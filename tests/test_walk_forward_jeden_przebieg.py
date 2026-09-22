"""Wykres walk-forward pokazuje JEDEN przebieg JEDNEGO horyzontu.

CO BYLO ZLE

`/learning/walk-forward` filtrowalo okna wylacznie po `m.engine`. Tymczasem:

  * `learn.wf_window` numeruje `idx` od zera W KAZDYM przebiegu treningowym
    (UNIQUE (training_run_id, idx), migracja 0006), a `train.py` zaklada NOWY
    przebieg przy kazdym treningu — wiec po czterech nocach w bazie sa cztery
    okna o `idx = 39`, z roznymi datami;
  * od migracji 0025 swieca 4h i swieca 1h sa trenowane ROWNOLEGLE, jako dwa
    osobne modele tego samego silnika. Swieca 1h ma 87–88 okien, swieca 4h
    39–40 — a silnik handluje wylacznie horyzontem domyslnym.

Zapytanie konczylo sie `ORDER BY w.idx DESC LIMIT 40`. Przy obecnych danych
czterdziesci najwyzszych indeksow to indeksy 87..48, czyli okna WYLACZNIE
z horyzontu, ktorym silnik nie handluje — pod naglowkiem opisanym metrykami
z horyzontu domyslnego. Gdy 1h chwilowo nie ma, wynikiem bylo czterdziesci
slupkow z czterech roznych przebiegow, po kilka o tym samym numerze, w
kolejnosci, ktora nie byla chronologiczna.

`/learning/state` bronil sie przed tym jawnym `AND ls.bar = %s`. Ten endpoint
nie mial zadnej obrony i dlatego dwie liczby opisujace ten sam wykres —
odsetek dodatnich okien na kaflu i slupki pod nim — mogly pochodzic z dwoch
roznych modeli.

CZEGO PILNUJE ATRAPA

`BazaNauki` nie oddaje gotowej odpowiedzi: trzyma OKNA DWOCH HORYZONTOW
i stosuje do nich to, co zapytanie faktycznie mowi — filtr po przebiegu (albo
jego brak), sortowanie i limit. Stare zapytanie dostaje wiec dokladnie to, co
dostawalo od Postgresa, i test na nim pada z tego samego powodu, z ktorego
wykres klamal na produkcji.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from services.engine.horizons import DEFAULT_BAR
from tests.test_dane_bez_zer import sprawdz_kolumny

# Wersja modelu, ktora `learn.learning_state` wskazuje dla horyzontu domyslnego.
MV_DOMYSLNA = 77
# Przebiegi swiecy domyslnej: cztery kolejne noce, najnowszy ostatni.
PRZEBIEGI_4H = {498: 74, 499: 75, 500: 76, 501: MV_DOMYSLNA}
# Przebieg swiecy porownawczej (1h) - inny model tego samego silnika.
PRZEBIEG_1H = 502

POCZATEK_4H = datetime(2022, 9, 1, tzinfo=timezone.utc)
# Okna 1h sa KROTSZE i NOWSZE, i to celowo: dzieki temu sam rok w `test_from`
# wystarcza, zeby powiedziec, z ktorego horyzontu pochodzi slupek.
POCZATEK_1H = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Okno:
    run: int
    idx: int
    test_from: datetime
    dni: int

    def wiersz(self) -> dict:
        """Dokladnie te kolumny, ktore pobiera zapytanie — ani jednej wiecej."""
        return {
            "idx": self.idx,
            "train_from": self.test_from - timedelta(days=180),
            "train_to": self.test_from,
            "test_from": self.test_from,
            "test_to": self.test_from + timedelta(days=self.dni),
            "n_trades": 12 + self.idx,
            "dominant_regime": "range",
            "metrics": {"sharpe": round(0.01 * self.idx, 4)},
        }


def okna_przebiegu(run: int, ile: int, poczatek: datetime, dni: int) -> list[Okno]:
    return [Okno(run, i, poczatek + timedelta(days=dni * i), dni) for i in range(ile)]


class BazaNauki:
    """Atrapa trzech zapytan `/learning/walk-forward`.

    Nie jest emulatorem Postgresa: rozumie wylacznie te klauzule, ktorymi
    roznia sie zapytanie stare i nowe — filtr po przebiegu, sortowanie i limit.
    Klauzula, ktorej nie rozumie, konczy sie bledem, zeby test nie przeszedl
    z powodu cichego pominiecia warunku.
    """

    def __init__(self, okna: list[Okno], *, stan: list[dict] | None = None,
                 przebiegi: dict[int, int] | None = None):
        self.okna = list(okna)
        self.stan = [{"model_version_id": MV_DOMYSLNA}] if stan is None else stan
        # przebieg -> wersja modelu
        self.przebiegi = dict(przebiegi if przebiegi is not None
                              else {**PRZEBIEGI_4H, PRZEBIEG_1H: 78})
        self.zapytania: list[tuple[str, object]] = []

    async def fetch_one(self, sql, params=None):
        w = self._odpowiedz(sql, params)
        return w[0] if w else None

    async def fetch_all(self, sql, params=None):
        return self._odpowiedz(sql, params)

    def zapytanie_z(self, fragment: str) -> tuple[str, object]:
        for sql, params in self.zapytania:
            if fragment in sql:
                return sql, params
        raise AssertionError(f"zadne zapytanie nie zawiera {fragment!r}")

    def _odpowiedz(self, sql, params):
        plaski = " ".join(sql.split())
        self.zapytania.append((plaski, params))
        if "learn.wf_window" in plaski:
            wiersze = self._okna(plaski, params)
        elif "learn.learning_state" in plaski:
            wiersze = [dict(w) for w in self.stan]
        elif "learn.training_run" in plaski:
            mv = params[0]
            pasujace = sorted(r for r, v in self.przebiegi.items() if v == mv)
            wiersze = [{"id": pasujace[-1]}] if pasujace else []
        else:
            raise AssertionError("zapytanie spoza tego endpointu: " + plaski)
        sprawdz_kolumny(plaski, wiersze)
        return wiersze

    def _okna(self, sql: str, params) -> list[dict]:
        if "w.training_run_id = %s" in sql:
            wybrane = [o for o in self.okna if o.run == params[0]]
        elif "m.engine" in sql:
            # Stare zapytanie. Oba horyzonty naleza do tego samego silnika,
            # wiec ten filtr nie odrzucal niczego - i o to w tej wadzie chodzi.
            wybrane = list(self.okna)
        else:
            raise AssertionError("zapytanie o okna bez zadnego filtru: " + sql)

        if "ORDER BY w.test_from DESC" in sql:
            wybrane.sort(key=lambda o: o.test_from, reverse=True)
        elif "ORDER BY w.idx DESC" in sql:
            wybrane.sort(key=lambda o: o.idx, reverse=True)
        else:
            raise AssertionError("nieznane sortowanie okien: " + sql)

        return [o.wiersz() for o in wybrane[:params[-1]]]


def podlacz(monkeypatch, baza: BazaNauki) -> BazaNauki:
    from services.api.routers import learning as modul

    monkeypatch.setattr(modul, "fetch_one", baza.fetch_one)
    monkeypatch.setattr(modul, "fetch_all", baza.fetch_all)
    return baza


def _cztery_noce_4h() -> list[Okno]:
    """Cztery kolejne nocne przebiegi swiecy domyslnej, po 40 okien kazdy."""
    okna: list[Okno] = []
    for run in PRZEBIEGI_4H:
        okna += okna_przebiegu(run, 40, POCZATEK_4H, dni=30)
    return okna


def _rownolegle_horyzonty() -> list[Okno]:
    """Stan z produkcji: cztery nocne przebiegi swiecy 4h i jeden swiecy 1h."""
    return _cztery_noce_4h() + okna_przebiegu(PRZEBIEG_1H, 88, POCZATEK_1H, dni=14)


# --------------------------------------------------------------------------

def test_wykres_NIE_pokazuje_okien_horyzontu_ktorym_silnik_nie_handluje(monkeypatch):
    """Najgrozniejsza postac tej wady.

    Swieca 1h siega `idx` 87, swieca 4h tylko 39. `ORDER BY w.idx DESC
    LIMIT 40` zwracalo wiec czterdziesci okien o indeksach 87..48 — czyli
    WYLACZNIE okna toru porownawczego, ktorym silnik nie handluje. Naglowek nad
    wykresem opisywal je metrykami horyzontu domyslnego.

    Okna 1h leza tu w roku 2026, okna 4h w latach 2022–2025, wiec sam rok
    rozstrzyga, skad pochodzi slupek."""
    b = podlacz(monkeypatch, BazaNauki(_rownolegle_horyzonty()))

    okna = asyncio.run(_walk_forward())

    assert okna, "endpoint nie oddal ani jednego okna"
    obce = [o for o in okna if o["test_from"] >= POCZATEK_1H.isoformat()]
    assert not obce, (
        f"{len(obce)} z {len(okna)} okien pochodzi z horyzontu porownawczego "
        f"(1h), ktorym silnik nie handluje — np. {obce[0]['test_from']}")

    # Skoro okna sa z przebiegu 4h, a ten ma ich 40, to tyle ma byc na wykresie.
    assert len(okna) == 40
    assert b.zapytanie_z("w.training_run_id = %s")[1][0] == 501, (
        "wybrany zostal inny przebieg niz ten, ktory wskazuje stan nauki")


def test_kazdy_numer_okna_wystepuje_RAZ(monkeypatch):
    """Druga postac tej samej wady, widoczna nawet bez drugiego horyzontu.

    `idx` numeruje sie od zera w KAZDYM przebiegu, wiec po czterech nocach
    zapytanie bez filtru po przebiegu zwracalo dziesiec ostatnich indeksow razy
    cztery — cztery slupki podpisane "okno 39", kazdy z inna data, w kolejnosci,
    ktora nie byla chronologiczna."""
    podlacz(monkeypatch, BazaNauki(_cztery_noce_4h()))

    okna = asyncio.run(_walk_forward())
    numery = [o["idx"] for o in okna]

    assert len(numery) == len(set(numery)), (
        "ten sam numer okna wystepuje kilka razy — slupki pochodza z roznych "
        f"przebiegow treningowych: {sorted(n for n in numery if numery.count(n) > 1)[:6]}")
    assert set(numery) == set(range(40))


def test_okna_ida_chronologicznie_najstarsze_po_lewej(monkeypatch):
    """Naglowek wykresu obiecuje "najstarsze po lewej". `ORDER BY w.idx`
    dotrzymywal tego tylko wtedy, gdy wszystkie okna pochodzily z jednego
    przebiegu — czyli dokladnie w przypadku, ktorego ta wada nie dotyczyla.
    Przy czterech przebiegach sortowanie po numerze okna mieszalo daty."""
    podlacz(monkeypatch, BazaNauki(_cztery_noce_4h()))

    daty = [o["test_from"] for o in asyncio.run(_walk_forward())]

    assert daty == sorted(daty), "okna nie ida chronologicznie"
    assert len(set(daty)) == len(daty), "dwa okna o tej samej dacie startu testu"


def test_horyzont_wybierany_TA_SAMA_regula_co_w_stanie_nauki(monkeypatch):
    """Druga regula wyboru horyzontu rozjechalaby sie z pierwsza po cichu:
    kafel "Okna dodatnie" i wykres pod nim opisywalyby dwa rozne modele,
    a nic na ekranie by tego nie zdradzalo. Test porownuje wiec parametry
    obu zapytan, zamiast sprawdzac kazde z osobna."""
    from services.api.routers import learning as modul

    b = podlacz(monkeypatch, BazaNauki(_rownolegle_horyzonty()))
    asyncio.run(_walk_forward())
    sql_wf, params_wf = b.zapytanie_z("learn.learning_state")

    b2 = podlacz(monkeypatch, BazaNauki([], stan=[{
        "status": "validated", "windows_total": 40, "windows_positive": 28,
        "median_oos_sharpe": 0.13, "positive_ci_low": 0.55, "positive_ci_high": 0.83,
        "max_drawdown": -12.5, "calibration_error": 0.04, "max_psi": 0.31,
        "trials_run": 120, "reason": "ok",
        "updated_at": datetime(2026, 9, 22, 3, 20, tzinfo=timezone.utc),
        "model_name": "crypto_signal_weighting_4h", "model_version": 7,
        "trained_from": POCZATEK_4H, "trained_to": POCZATEK_1H,
    }]))
    asyncio.run(modul.state("crypto"))
    sql_st, params_st = b2.zapytanie_z("learn.learning_state")

    assert "ls.bar = %s" in sql_wf, "walk-forward nie filtruje po horyzoncie"
    assert params_wf == params_st, (
        f"walk-forward pyta o stan nauki inaczej niz /state: "
        f"{params_wf!r} vs {params_st!r}")
    assert DEFAULT_BAR in params_wf


def test_brak_stanu_nauki_daje_PUSTO_a_nie_cudze_okna(monkeypatch):
    """Silnik bez wiersza stanu nauki nie ma wykresu walk-forward. Poprzednio
    dostawal okna dowolnego przebiegu, jaki akurat byl w bazie — z horyzontu,
    ktorego nikt nie wybieral."""
    podlacz(monkeypatch, BazaNauki(_rownolegle_horyzonty(), stan=[]))
    assert asyncio.run(_walk_forward()) == []

    podlacz(monkeypatch, BazaNauki(_rownolegle_horyzonty(),
                                   stan=[{"model_version_id": None}]))
    assert asyncio.run(_walk_forward()) == []


def test_limit_obcina_NAJNOWSZE_okna(monkeypatch):
    """Limit ma zostawiac koniec osi czasu, a nie najwyzsze numery — to dwie
    rozne rzeczy, gdy w bazie jest wiecej niz jeden przebieg."""
    podlacz(monkeypatch, BazaNauki(_rownolegle_horyzonty()))

    okna = asyncio.run(_walk_forward(limit=5))

    assert [o["idx"] for o in okna] == [35, 36, 37, 38, 39]
    assert okna[-1]["test_from"] == (POCZATEK_4H + timedelta(days=30 * 39)).isoformat()


async def _walk_forward(limit: int = 40):
    from services.api.routers import learning as modul

    return await modul.walk_forward("crypto", limit)
