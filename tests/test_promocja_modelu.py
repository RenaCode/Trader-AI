"""Bramka promocji modelu i wybór klucza API — dwie ciche podmiany.

DLACZEGO OBA TEMATY W JEDNYM PLIKU

Łączy je kształt błędu, nie moduł. W obu wypadkach system dalej działa,
raportuje zielono i nie zgłasza niczego — a podmienia się to, CZYM handluje
albo CZYM się uwierzytelnia. Awaria przychodzi później i wygląda na coś
innego, niż jest.

  * trener stawiał każdą nowo wytrenowaną wersję na `status='active'`
    (poza `degraded`), a `active_model` handluje aktywną wersją od razu.
    Liczby z docs/05-ochrona-kapitalu.md opisywały więc wagi, którymi system
    mógł nie handlować tydzień później;
  * `zbuduj_adapter` wybierał klucz API przez `ORDER BY c.id LIMIT 1`, czyli
    po rotacji NAJSTARSZY z aktywnych. Objaw pojawia się dopiero w dniu
    wygaśnięcia starego klucza i wygląda jak awaria uwierzytelnienia bez
    przyczyny — nowy klucz był w bazie poprawny przez cały czas.

Testy siedzą razem, bo powstały z jednego przeglądu; pozostałe pliki testowe
są w tym momencie w rękach równoległych zadań.
"""
import asyncio

import pytest

from services.engine.train import (
    status_nowej_wersji, tryb_handlu, wersja_produkcyjna, zapisz_promocje,
)


# ----------------------------------------------------------- werdykt bramki

def test_w_demo_nowa_wersja_wchodzi_sama():
    """Rachunek papierowy: pomyłka kosztuje jeden dzień, więc czekanie na
    człowieka opóźniałoby naukę bez powodu."""
    assert status_nowej_wersji("learning", "demo")[0] == "active"
    assert status_nowej_wersji("validated", "demo")[0] == "active"


def test_w_live_nowa_wersja_NIE_wchodzi_sama():
    """Tu jest cała zmiana: o 4:40 nad ranem nie zmienia się strategia,
    na której skalibrowano ochronę kapitału."""
    docelowy, powod = status_nowej_wersji("validated", "live")
    assert docelowy == "candidate"
    assert "live" in powod


def test_wynik_dobry_nie_jest_zgoda():
    """`validated` mówi o wyniku pomiaru, nie o decyzji człowieka. Gdyby sam
    otwierał bramkę, bramki by nie było — automat promowałby dokładnie te
    wersje, które i tak promował."""
    assert status_nowej_wersji("validated", "live", zgoda=False)[0] == "candidate"
    assert status_nowej_wersji("learning", "live", zgoda=False)[0] == "candidate"


def test_jawna_zgoda_czlowieka_promuje_takze_w_live():
    assert status_nowej_wersji("validated", "live", zgoda=True)[0] == "active"


def test_degraded_nie_wchodzi_NAWET_ze_zgoda():
    """Zgoda znosi wymóg trybu, a nie wymóg wyniku. Flaga podnoszona ręcznie
    o trzeciej w nocy nie może promować modelu bez przewagi."""
    for tryb in ("demo", "live"):
        for zgoda in (False, True):
            assert status_nowej_wersji("degraded", tryb, zgoda)[0] == "candidate"


def test_powod_wstrzymania_jest_zdaniem_dla_czlowieka():
    """Powód trafia do `reason`, czyli do treści powiadomienia. Pusty albo
    techniczny zostawiłby właściciela z kandydatem, o którym nikt nie wie."""
    _, powod = status_nowej_wersji("validated", "live")
    assert len(powod) > 20 and "zatwierdzenie" in powod


# ------------------------------------------------------- odczyt trybu z bazy

class KursorJednegoWiersza:
    """Kursor oddający jeden podłożony wiersz i zapamiętujący SQL."""

    def __init__(self, wiersz):
        self.wiersz, self.sql, self.parametry = wiersz, [], []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.parametry.append(params)
        return self

    def fetchone(self):
        return self.wiersz


def test_tryb_live_gdy_silnik_bije_w_trybie_live():
    kur = KursorJednegoWiersza({"zywe": True})
    assert tryb_handlu(kur, "crypto") == "live"


def test_tryb_demo_gdy_nic_nie_swiadczy_o_zywym_rachunku():
    kur = KursorJednegoWiersza({"zywe": False})
    assert tryb_handlu(kur, "crypto") == "demo"


def test_tryb_czytany_z_PULSU_I_ZE_ZLECEN():
    """Sam puls to jeden wiersz na silnik — po powrocie na demo mówi już tylko
    o teraźniejszości. Rachunek, na którym kupowano za prawdziwe pieniądze,
    pozostaje prawdziwy, więc zlecenia live są drugim świadectwem."""
    kur = KursorJednegoWiersza({"zywe": False})
    tryb_handlu(kur, "crypto")
    sql = " ".join(kur.sql)
    assert "risk.engine_heartbeat" in sql and "exec.order" in sql
    assert sql.count("'live'") == 2, "oba warunki muszą pytać o tryb live"


def test_tryb_jest_ODCZYTANY_a_zgoda_PODANA():
    """Podział na te dwie rzeczy jest istotą bramki.

    Tryb to fakt o działającym silniku — gdyby trener miał własne `--mode`,
    powstałaby druga deklaracja tego samego faktu, a jej rozjazd z chartem
    dawałby otwartą bramkę przy zamkniętych drzwiach (values.yaml ostrzega
    przed tym samym przy `--kind` i `--limit`). Zgoda odwrotnie: musi być
    flagą przebiegu, bo zadanie cykliczne ma jej nie umieć podnieść.
    """
    import inspect

    import services.engine.train as t

    assert list(inspect.signature(tryb_handlu).parameters) == ["cur", "engine"]
    zrodlo_main = inspect.getsource(t.main)
    assert '"--zatwierdz-promocje"' in zrodlo_main
    assert '"--mode"' not in zrodlo_main, \
        "tryb handlu czytamy z bazy, nie z drugiej deklaracji w chart"


# --------------------------------------------------------------- zapis stanu

class KursorZapisu:
    """Kursor, który tylko zbiera instrukcje — sprawdzamy, czego NIE wysłano."""

    def __init__(self, wiersz=None):
        self.wiersz, self.instrukcje = wiersz, []

    def execute(self, sql, params=None):
        self.instrukcje.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self.wiersz

    def sql(self):
        return [s for s, _ in self.instrukcje]


def test_promocja_wycofuje_poprzednia_wersje_przed_nadaniem_nowej():
    """Odwrotna kolejność łamie unikalny indeks „jedna aktywna wersja na
    model" — cały zapis wywraca się wtedy w transakcji na końcu treningu."""
    kur = KursorZapisu()
    zapisz_promocje(kur, model_id=7, mv_id=42, docelowy="active")
    sql = kur.sql()
    assert len(sql) == 2
    assert "status='retired'" in sql[0]
    assert "status='active'" in sql[1]


def test_kandydat_NIE_WYCOFUJE_wersji_produkcyjnej():
    """Najgroźniejszy wariant tej zmiany. Wycofanie zostawione bezwarunkowo
    byłoby gorsze niż brak bramki: `active_model` nie znalazłby żadnej
    aktywnej wersji i cofnąłby produkcję do założeń początkowych — bramka
    przeciw cichej podmianie strategii wywoływałaby ją co noc sama."""
    kur = KursorZapisu()
    zapisz_promocje(kur, model_id=7, mv_id=42, docelowy="candidate")
    assert not any("retired" in s for s in kur.sql()), \
        "wersja, którą handluje produkcja, nie może zostać wycofana"
    assert len(kur.sql()) == 1 and "status='candidate'" in kur.sql()[0]


def test_kandydat_nie_dostaje_daty_promocji():
    """`promoted_at` wpisane „na wszelki wypadek" kłamie w jedynym miejscu,
    gdzie potem szuka się odpowiedzi, kiedy strategia się zmieniła."""
    kur = KursorZapisu()
    zapisz_promocje(kur, model_id=7, mv_id=42, docelowy="candidate")
    assert "promoted_at" not in kur.sql()[0]


def test_wersja_produkcyjna_pomija_wersje_wlasnie_wytrenowana():
    """Bez `id <> mv_id` komunikat powiedziałby, że produkcja handluje
    wersją, która właśnie została kandydatem."""
    kur = KursorZapisu({"version": 11})
    assert wersja_produkcyjna(kur, model_id=7, mv_id=42) == 11
    sql, params = kur.instrukcje[0]
    assert "id<>%s" in sql.replace(" ", "") and params == (7, 42)
    assert "status='active'" in sql


def test_brak_aktywnej_wersji_to_None_a_nie_wywrotka():
    assert wersja_produkcyjna(KursorZapisu(None), model_id=7, mv_id=42) is None


# ------------------------------------------------- wybór klucza API po rotacji

class PolaczenieZKluczami:
    """Atrapa `psycopg.connect` oddająca podłożoną listę kluczy.

    Atrapa NIE sortuje: kolejność wierszy jest tym, co zwróciłaby baza dla
    zapytania w kodzie. Dlatego osobno sprawdzamy treść `ORDER BY` — bez tego
    test przechodziłby też dla zapytania, które oddaje najstarszy klucz.
    """

    def __init__(self, klucze):
        self.klucze, self.sql = klucze, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        return self

    def fetchall(self):
        return self.klucze


def _klucz(nr):
    return {"id": nr, "key_hint": f"••••{nr:04d}", "key_enc": f"k{nr}".encode(),
            "secret_enc": f"s{nr}".encode(), "venue_code": "bybit",
            "demo_host": "https://demo", "live_host": "https://live"}


@pytest.fixture
def adapter_na_atrapie(monkeypatch):
    """Buduje adapter na podłożonych kluczach, oddaje (adapter, połączenie)."""
    import services.api.security as security
    import services.engine.loop as loop
    import services.exchanges.bybit as bybit

    class FalszywySecretBox:
        def decrypt(self, dane):
            return dane.decode()

    class FalszywyAdapter:
        # Fabryka (services.exchanges.fabryka) wola adapter POZYCYJNIE
        # (klucz, sekret) i nazwanymi (mode, host) - atrapa przyjmuje oba.
        def __init__(self, api_key=None, api_secret=None, **kw):
            self.kw = dict(kw, api_key=api_key, api_secret=api_secret)

    monkeypatch.setattr(security, "SecretBox", FalszywySecretBox)
    monkeypatch.setattr(bybit, "BybitAdapter", FalszywyAdapter)

    def zbuduj(klucze, mode="demo"):
        polaczenie = PolaczenieZKluczami(klucze)
        monkeypatch.setattr(loop.psycopg, "connect",
                            lambda *a, **k: polaczenie)
        adapter = asyncio.run(loop.zbuduj_adapter("postgresql:///x", mode))
        return adapter, polaczenie

    return zbuduj


def test_po_rotacji_wygrywa_NAJNOWSZY_klucz(adapter_na_atrapie):
    """Stary klucz zostawiony jako `active` wygrywał po cichu przy
    `ORDER BY c.id`. System uwierzytelniał się kluczem, który właśnie
    zastąpiono, i nikt się o tym nie dowiadywał aż do jego wygaśnięcia."""
    adapter, _ = adapter_na_atrapie([_klucz(9), _klucz(1)])
    assert adapter.kw["api_key"] == "k9"


def test_zapytanie_sortuje_po_dacie_utworzenia_MALEJACO(adapter_na_atrapie):
    """Atrapa nie wykonuje `ORDER BY`, więc kolejność wierszy niczego nie
    dowodzi — dowodzi jej treść zapytania."""
    _, polaczenie = adapter_na_atrapie([_klucz(9)])
    sql = polaczenie.sql[0]
    assert "ORDER BY c.created_at DESC, c.id DESC" in sql
    assert "ORDER BY c.id LIMIT 1" not in sql


def test_dwa_aktywne_klucze_naraz_sa_SLYSZALNE(adapter_na_atrapie, capsys):
    """Niedokończona rotacja to stan, którego nikt nie zaplanował: albo
    zapomniano dezaktywować starego klucza, albo wgrano klucz do złego trybu.
    Cisza w tym miejscu była połową poprzedniego błędu."""
    adapter_na_atrapie([_klucz(9), _klucz(1)])
    wyjscie = capsys.readouterr().out
    assert "2 aktywnych kluczy" in wyjscie and "••••0009" in wyjscie


def test_jeden_klucz_nie_halasuje(adapter_na_atrapie, capsys):
    """Ostrzeżenie przy normalnym stanie nauczyłoby je ignorować."""
    adapter_na_atrapie([_klucz(9)])
    assert capsys.readouterr().out == ""


def test_brak_klucza_konczy_proces_zamiast_handlowac_dalej(adapter_na_atrapie):
    """Silnik udający, że handluje, jest gorszy niż silnik, który nie wstał."""
    with pytest.raises(SystemExit):
        adapter_na_atrapie([])


# ------------------------------------- bramka struktury (audyt Fable F-3.1, T8)
#
# 28.09 progi per rezim weszly do demo deployem kodu: kazda wersja
# nie-`degraded` byla `active`. Teraz zmiana struktury -> `candidate`.

import json  # noqa: E402
import os  # noqa: E402

from services.engine.train import zmiana_struktury  # noqa: E402

AKTYWNE = {"bar": "4h", "wybor": "ic", "progi": "per_rezim", "kind": "spot_eu",
           "weights": {"range": {"a": 0.5}}, "enter_threshold": 0.28,
           "enter_threshold_per_regime": {"range": 0.28, "high_vol": 0.28},
           "enter_threshold_windows": {"range": 39, "high_vol": 39}}


def test_te_same_klucze_i_progi_w_tolerancji_to_NIE_zmiana():
    nowe = json.loads(json.dumps(AKTYWNE))
    nowe["enter_threshold_per_regime"] = {"range": 0.30, "high_vol": 0.26}
    nowe["weights"] = {"range": {"a": 0.9}}
    nowe["enter_threshold_windows"] = {"range": 40, "high_vol": 40}
    assert zmiana_struktury(AKTYWNE, nowe) is None


def test_zmiana_z_28_09_progi_o_25_procent_JEST_zmiana():
    nowe = json.loads(json.dumps(AKTYWNE))
    nowe["enter_threshold_per_regime"] = {"range": 0.35, "high_vol": 0.21}
    opis = zmiana_struktury(AKTYWNE, nowe)
    assert opis and "range" in opis and "+25%" in opis and "high_vol" in opis


def test_nowy_klucz_params_JEST_zmiana():
    nowe = AKTYWNE | {"progi_na_wagach": "ic"}
    assert "+progi_na_wagach" in zmiana_struktury(AKTYWNE, nowe)
    assert "−progi" in zmiana_struktury(AKTYWNE, {k: v for k, v in AKTYWNE.items()
                                                  if k != "progi"})


def test_inna_wartosc_konfiguracji_JEST_zmiana():
    assert "kind" in zmiana_struktury(AKTYWNE, AKTYWNE | {"kind": "spot"})


def test_rezim_ktory_stracil_prog_JEST_zmiana():
    nowe = AKTYWNE | {"enter_threshold_per_regime": {"range": 0.28}}
    assert "reżimy z progiem" in zmiana_struktury(AKTYWNE, nowe)


def test_bez_aktywnej_wersji_nie_ma_z_czym_porownac():
    assert zmiana_struktury(None, AKTYWNE) is None


def test_zmiana_struktury_zatrzymuje_promocje_TAKZE_w_demo():
    docelowy, powod = status_nowej_wersji("validated", "demo", zmiana="próg range +25%")
    assert docelowy == "candidate"
    assert "zmiana struktury" in powod and "docs/hipotezy.md" in powod


def test_zgoda_czlowieka_przepuszcza_zmiane_struktury():
    assert status_nowej_wersji("learning", "demo", zgoda=True,
                               zmiana="x")[0] == "active"
    assert status_nowej_wersji("degraded", "demo", zgoda=True,
                               zmiana="x")[0] == "candidate"


TEST_DB_URL = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not TEST_DB_URL, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_PRAWDZIWA_BAZA_zapisz_wynik_zostawia_kandydata_i_opis_zmiany():
    """Cala sciezka zapisu: aktywna wersja zostaje, nowa jest `candidate`,
    kolumna 0090 ma opis, `reason` mowi, dlaczego."""
    import psycopg
    from psycopg.rows import dict_row

    from services.engine import train as tr
    from services.engine.horizons import get as get_horizon

    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO learn.model (name, engine, kind, description)"
                    " VALUES ('test_struktura_t8', 'crypto', 'signal_weighting', 't')"
                    " RETURNING id")
        model_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO learn.model_version (model_id, version, status, params)"
                    " VALUES (%s, 1, 'active', %s) RETURNING id",
                    (model_id, json.dumps(AKTYWNE)))
        stara = cur.fetchone()["id"]
        bez = {k: v for k, v in AKTYWNE.items() if k not in tr.KLUCZE_WYNIKOWE}
        cur.execute("INSERT INTO learn.model_version (model_id, version, status, params)"
                    " VALUES (%s, 2, 'training', %s) RETURNING id",
                    (model_id, json.dumps(bez | {"progi_na_wagach": "ic"})))
        nowa = cur.fetchone()["id"]
        cur.execute("INSERT INTO learn.training_run (model_version_id, train_from,"
                    " train_to, status) VALUES (%s, now(), now(), 'running') RETURNING id",
                    (nowa,))
        run = cur.fetchone()["id"]
        w = tr.Status("validated", "Kryteria spełnione.", "", 40, 30, 75.0, 0.1,
                      0.6, 0.85, 0.55, 0.8, 0.97)
        prod = tr.Produkcja({"range": 0.28, "high_vol": 0.28}, {"range": 39, "high_vol": 39},
                            0.28, {"range": {"a": 0.5}}, [], [])

        class Pol:
            def commit(self):
                pass

            def rollback(self):
                conn.rollback()

        reason = tr.zapisz_wynik(cur, Pol(), False, "crypto", get_horizon("4h"),
                                 model_id, nowa, run, w, prod, worst_dd=-1.0, ece=None,
                                 max_psi=0.0, trials=1, zgoda_na_promocje=False)
        cur.execute("SELECT id, status, zmiana_struktury FROM learn.model_version"
                    " WHERE model_id=%s ORDER BY id", (model_id,))
        wiersze = {r["id"]: r for r in cur.fetchall()}
        conn.rollback()
    assert wiersze[stara]["status"] == "active"
    assert wiersze[nowa]["status"] == "candidate"
    assert "+progi_na_wagach" in wiersze[nowa]["zmiana_struktury"]
    assert "PROMOCJA WSTRZYMANA (zmiana struktury" in reason
