"""BRAK POMIARU NIE JEST POMIAREM ROWNYM ZERU.

Cztery wady w routerach API mialy jedna wspolna przyczyne: brak wiersza w bazie
byl zamieniany na `0.0` albo na odniesienie zastepcze, a front nie mial juz
z czego odroznic jednego od drugiego. Skutek za kazdym razem szedl w te sama,
najgorsza strone - system BEZ DANYCH wygladal na system ZMIERZONY:

  * rachunek bez ani jednego punktu kapitalu pokazywal 0,00 i wzrost "+0,00%"
    w tonie zysku;
  * limit ryzyka, ktorego nikt nie zmierzyl, rysowal sie jak limit wykorzystany
    w zerowym stopniu - wiec smierc dozorcy malowala zakladke "Ryzyko"
    na lepiej niz przed awaria;
  * kafel "Zmiana 24h" bez punktu sprzed doby pokazywal zmiane OD STARTU,
    czyli prawdziwa liczbe podpisana nieprawdziwym okresem;
  * "Razem" na wykresie kapitalu dodawalo USDC do USD i podawalo wynik jako
    jedna liczbe;
  * kolumna "Trafnosc" liczyla decyzje NIEROZLICZONE jako nietrafienia.

CZEGO PILNUJE `Dyspozytor` I DLACZEGO NIE WYSTARCZA ZWYKLA ATRAPA

`tests/atrapa_bazy.py` oddaje wiersze z kolejki, nie patrzac na zapytanie.
Test napisany na takiej atrapie przechodzi rowniez wtedy, gdy SELECT wcale nie
pobiera kolumny, ktora atrapa podlozyla - czyli przechodzi Z BLEDNEGO POWODU
i utrwala zapytanie, ktorego prawdziwa baza by nie wykonala.

`Dyspozytor` zamyka obie te dziury:

  * DOBIERA odpowiedz po fragmencie SQL, wiec endpoint wysylajacy inne
    zapytanie niz to poprawione nie dostaje podlozonego wiersza, tylko blad;
  * SPRAWDZA, ze KAZDA kolumna podlozonego wiersza wystepuje w zapytaniu,
    ktore ten wiersz dostaje.

Drugi punkt jest tu tym, co czyni test zaleznym od poprawki, a nie od atrapy.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest


# --------------------------------------------------------------- atrapa bazy

def sprawdz_kolumny(sql: str, wiersze: list[dict]) -> None:
    """Kazda kolumna podlozonego wiersza MUSI wystepowac w tym zapytaniu.

    Bez tego sprawdzenia test przechodzi, gdy atrapa poda kolumne, ktorej
    SELECT nie pobiera - a wtedy mierzy wylacznie sama siebie.
    """
    for wiersz in wiersze:
        for kolumna in wiersz:
            if not re.search(rf"\b{re.escape(kolumna)}\b", sql):
                raise AssertionError(
                    f"podlozony wiersz ma kolumne {kolumna!r}, ktorej to "
                    f"zapytanie NIE POBIERA — prawdziwa baza oddalaby wiersz "
                    f"bez niej:\n{sql}")


class Dyspozytor:
    """Atrapa bazy dobierajaca odpowiedz po tresci zapytania.

    Reguly sa parami (fragment albo krotka fragmentow, wiersze). Wygrywa
    PIERWSZA regula, ktorej wszystkie fragmenty wystepuja w zapytaniu - wiec
    reguly bardziej szczegolowe podaje sie wczesniej.

    Zapytanie, do ktorego nie pasuje zadna regula, konczy sie bledem. Ciche
    oddanie pustej listy zamienialoby "endpoint pyta o cos innego, niz myslimy"
    w "endpoint nic nie znalazl", czyli w wynik nie do odroznienia od poprawnego.
    """

    def __init__(self, *reguly):
        self.reguly = [((f,) if isinstance(f, str) else tuple(f), list(w))
                       for f, w in reguly]
        self.zapytania: list[tuple[str, object]] = []

    async def fetch_one(self, sql, params=None):
        wiersze = self._odpowiedz(sql, params)
        return wiersze[0] if wiersze else None

    async def fetch_all(self, sql, params=None):
        return self._odpowiedz(sql, params)

    def zapytanie_z(self, *fragmenty: str) -> tuple[str, object]:
        """Pierwsze wyslane zapytanie zawierajace wszystkie fragmenty."""
        for sql, params in self.zapytania:
            if all(f in sql for f in fragmenty):
                return sql, params
        raise AssertionError(
            f"zadne zapytanie nie zawiera {fragmenty!r}; wyslano:\n"
            + "\n".join(s[:160] for s, _ in self.zapytania))

    def _odpowiedz(self, sql, params):
        plaski = " ".join(sql.split())
        self.zapytania.append((plaski, params))
        for fragmenty, wiersze in self.reguly:
            if all(f in plaski for f in fragmenty):
                sprawdz_kolumny(plaski, wiersze)
                return [dict(w) for w in wiersze]
        raise AssertionError(
            "zapytanie bez podlozonej odpowiedzi — atrapa nie zgaduje:\n" + plaski)


def podlacz(monkeypatch, modul, dyspozytor: Dyspozytor) -> Dyspozytor:
    """Podmienia `fetch_one`/`fetch_all` W MODULE ROUTERA.

    Routery importuja te funkcje do wlasnej przestrzeni nazw (`from ..db import
    fetch_all`), wiec podmiana w `services.api.db` nie mialaby na nie wplywu.

    Nie kazdy router importuje obie — `kapital_laczny` czyta wylacznie
    `fetch_all`. Podmieniamy wiec te, ktore modul naprawde ma, i sprawdzamy,
    ze podmieniona zostala przynajmniej jedna: cichy brak podmiany oznaczalby
    test rozmawiajacy z prawdziwa pula polaczen.
    """
    podmienione = 0
    for nazwa in ("fetch_one", "fetch_all"):
        if hasattr(modul, nazwa):
            monkeypatch.setattr(modul, nazwa, getattr(dyspozytor, nazwa))
            podmienione += 1
    assert podmienione, f"{modul.__name__} nie ma czego podmienic"
    return dyspozytor


# Fragmenty rozrozniajace trzy zapytania `overview` do tej samej tabeli.
_DOBA_TEMU = ("exec.equity_point", "interval '24 hours'")
_PIERWSZY = ("exec.equity_point", "ORDER BY event_time ASC")
_OSTATNI = ("exec.equity_point",)

PUNKT_KAPITALU = {
    "equity": Decimal("106000"), "cash": Decimal("20000"),
    "unrealized": Decimal("1500"), "realized_cum": Decimal("4500"),
    "exposure": Decimal("86000"),
    "event_time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
}


# ------------------------------------------------- 1. kapital bez ani jednego punktu

def test_rachunek_bez_pomiaru_NIE_jest_rachunkiem_wyzerowanym(monkeypatch):
    """Silnik, ktory nie zapisal ani jednego punktu kapitalu, pokazywal
    kapital 0,00, gotowke 0,00, ekspozycje 0,00 i wzrost "+0,00%" na zielono.

    To jest gorsze niz pusty ekran: pusty ekran nie oszukuje. Ten pokazywal
    stan rachunku, ktorego nikt nie zmierzyl, i to stan skrajny."""
    from services.api.routers import overview as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        (_DOBA_TEMU, []),
        (_PIERWSZY, []),
        (_OSTATNI, []),
        ("risk.engine_heartbeat", []),
        ("learn.learning_state", []),
    ))

    o = asyncio.run(modul.overview("crypto", "demo"))

    for pole in ("equity", "cash", "unrealized", "realized_cum", "exposure",
                 "change_24h_pct", "change_total_pct", "as_of"):
        assert o[pole] is None, f"{pole} = {o[pole]!r} — brak pomiaru podany jako pomiar"


# ------------------------------------------------- 2. "Zmiana 24h" to zmiana 24-godzinna

def test_zmiana_24h_bez_punktu_sprzed_doby_NIE_jest_zmiana_od_startu(monkeypatch):
    """`prev = ... if day_ago else start` robilo z braku punktu sprzed doby
    odniesienie do pierwszego punktu w ogole.

    Silnik dzialajacy szesc godzin pokazywal wiec w kaflu "Zmiana 24h" zmiane
    od startu — liczbe prawdziwa, podpisana nieprawdziwym okresem. Na zakladce
    kapitalu lacznego te dwie liczby stoja obok siebie, wiec ten sam wynik
    wystepowal dwa razy pod dwiema etykietami i wygladal na potwierdzenie."""
    from services.api.routers import overview as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        (_DOBA_TEMU, []),                                   # nic starszego niz doba
        (_PIERWSZY, [{"equity": Decimal("100000")}]),
        (_OSTATNI, [PUNKT_KAPITALU]),
        ("risk.engine_heartbeat", []),
        ("learn.learning_state", []),
    ))

    o = asyncio.run(modul.overview("crypto", "demo"))

    assert o["change_total_pct"] == pytest.approx(6.0), "zmiana od startu ma dalej dzialac"
    assert o["change_24h_pct"] is None, (
        f"zmiana 24h = {o['change_24h_pct']!r} bez punktu sprzed doby — "
        f"to jest zmiana od startu pod cudza etykieta")


def test_zmiana_24h_liczy_sie_z_punktu_sprzed_doby(monkeypatch):
    """Kontrola dla testu wyzej: gdy punkt sprzed doby JEST, liczba ma byc
    policzona wlasnie z niego, a nie z pierwszego punktu serii."""
    from services.api.routers import overview as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        (_DOBA_TEMU, [{"equity": Decimal("100000")}]),
        (_PIERWSZY, [{"equity": Decimal("50000")}]),
        (_OSTATNI, [PUNKT_KAPITALU]),
        ("risk.engine_heartbeat", []),
        ("learn.learning_state", []),
    ))

    o = asyncio.run(modul.overview("crypto", "demo"))
    assert o["change_24h_pct"] == pytest.approx(6.0)
    assert o["change_total_pct"] == pytest.approx(112.0)


# ------------------------------------------------- 3. limity ryzyka

def test_limit_NIEZMIERZONY_jest_odrozialny_od_zmierzonego_na_zero(monkeypatch):
    """To jest cala tresc tej wady, w jednym zdaniu: `used ... else 0.0`
    i `utilization or 0.0` sprawialy, ze limit BEZ ANI JEDNEGO POMIARU
    wygladal identycznie jak limit wykorzystany w zerowym stopniu.

    Kierunek pomylki jest tu najgorszy z mozliwych. Gdy dozorca przestanie
    pisac do `risk.limit_usage`, wszystkie paski zjezdzaja do zera i zakladka
    "Ryzyko" wyglada LEPIEJ niz przed awaria — awaria nadzoru maluje sie
    na ekranie jako wzorowa dyscyplina."""
    from services.api.routers import risk as modul

    wspolne = {"scope": "portfel", "unit": "pct", "action": "block",
               "enabled": True, "wznowienie": "reczne", "max_wznowien": 0,
               "symbol": None}
    podlacz(monkeypatch, modul, Dyspozytor(
        ("risk.limit_def", [
            {"id": 1, "name": "nigdy niemierzony", "value": Decimal("5"),
             "used": None, "utilization": None, "event_time": None, **wspolne},
            {"id": 2, "name": "zmierzony na zero", "value": Decimal("5"),
             "used": Decimal("0"), "utilization": 0.0,
             "event_time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
             **wspolne},
        ]),
    ))

    niemierzony, zero = asyncio.run(modul.limits("crypto", "demo"))

    assert niemierzony["used"] is None and niemierzony["utilization"] is None, (
        "limit bez pomiaru oddany jako pomiar rowny zeru")
    assert niemierzony["as_of"] is None

    # Pomiar rowny zeru ma dalej byc ZEREM, a nie brakiem — inaczej poprawka
    # zamienilaby jedna nierozroznialnosc na druga.
    assert zero["used"] == 0.0 and zero["utilization"] == 0.0
    assert zero["as_of"] is not None

    # Pola, na ktorych front juz polega, maja zostac.
    for l in (niemierzony, zero):
        assert l["enabled"] is True
        assert l["threshold"] == 5.0
        assert "as_of" in l


# ------------------------------------------------- 4. stan nauki bez wiersza

def test_stan_nauki_bez_wiersza_oddaje_KOMPLET_kluczy(monkeypatch):
    """Bez wiersza `learn.learning_state` endpoint oddawal obiekt o dwoch
    polach. W JS brakujacy klucz i klucz o wartosci `undefined` sa
    nieodroznialne, wiec karta wypisywala doslownie "undefined/undefined".

    Test porownuje ZBIORY KLUCZY obu sciezek, zamiast wyliczac je recznie:
    wyliczona lista rozjechalaby sie przy pierwszym nowym polu i przestalaby
    cokolwiek pilnowac."""
    from services.api.routers import learning as modul

    pelny = {
        "status": "validated", "windows_total": 40, "windows_positive": 28,
        "median_oos_sharpe": 0.13, "positive_ci_low": 0.55, "positive_ci_high": 0.83,
        "max_drawdown": -12.5, "calibration_error": 0.04, "max_psi": 0.31,
        "trials_run": 120, "reason": "ok",
        "updated_at": datetime(2026, 9, 22, 3, 20, tzinfo=timezone.utc),
        "model_name": "crypto_signal_weighting_4h", "model_version": 7,
        "trained_from": datetime(2022, 8, 1, tzinfo=timezone.utc),
        "trained_to": datetime(2026, 9, 22, tzinfo=timezone.utc),
    }

    # Drugie zapytanie tego endpointu od 2026-09-27: N BRAMKUJACE, czyli
    # `count(DISTINCT params)` po `learn.model_version`. Atrapa slusznie odmawia
    # zgadywania, wiec regula musi tu byc - inaczej test mowilby o rozjezdzie
    # kluczy, a padal na braku odpowiedzi.
    podlacz(monkeypatch, modul, Dyspozytor(
        ("learn.learning_state", [pelny]),
        ("count(DISTINCT v.params)", [{"n": 58}])))
    z_wierszem = asyncio.run(modul.state("crypto"))

    podlacz(monkeypatch, modul, Dyspozytor(("learn.learning_state", [])))
    bez_wiersza = asyncio.run(modul.state("crypto"))

    assert set(bez_wiersza) == set(z_wierszem), (
        "brakujace klucze: " + ", ".join(sorted(set(z_wierszem) - set(bez_wiersza))))
    for klucz in ("windows_total", "windows_positive", "windows_positive_pct"):
        assert bez_wiersza[klucz] is None, f"{klucz} = {bez_wiersza[klucz]!r}"
    assert bez_wiersza["status"] == "warmup"
    assert bez_wiersza["reason"]


# ------------------------------------------------- 5. trafnosc per sygnal

SYGNALY = [
    # Sygnal swiezo dodany: uzyty 40 razy, ani jedna pozycja jeszcze nie zamknieta.
    {"name": "sila_wzgledna", "family": "przekroj", "used_count": 40,
     "avg_contribution": 0.21, "closed_count": 0, "hit_rate": None, "total_pnl": None},
    # Sygnal rozliczony czesciowo.
    {"name": "rsi", "family": "momentum", "used_count": 10,
     "avg_contribution": -0.05, "closed_count": 4, "hit_rate": 0.75,
     "total_pnl": Decimal("312.50")},
]


def test_trafnosc_liczy_sie_WYLACZNIE_po_zamknietych_pozycjach(monkeypatch):
    """`avg(CASE WHEN p.realized_pnl > 0 THEN 1.0 ELSE 0.0 END)` po LEFT JOIN
    wrzucal do `ELSE 0.0` kazdy wiersz BEZ dopasowanej zamknietej pozycji —
    czyli liczyl decyzje nierozliczona jako NIETRAFIENIE.

    Zanizenie nie bylo rownomierne: uderzalo najmocniej w sygnaly najmlodsze,
    bo te maja najwiecej decyzji jeszcze wiszacych. Kolumna "Trafnosc"
    porzadkowala wiec hipotezy po WIEKU, udajac, ze porzadkuje je po jakosci.

    Podlozone wiersze maja kolumne `closed_count`, ktorej stare zapytanie
    nie pobieralo — `Dyspozytor` odrzuca je wiec, zanim dojdzie do asercji."""
    from services.api.routers import learning as modul

    d = podlacz(monkeypatch, modul, Dyspozytor(
        ("journal.decision_contribution", SYGNALY)))

    swiezy, rsi = asyncio.run(modul.signal_performance("crypto"))

    # Brak rozliczen to BRAK TRAFNOSCI, a nie trafnosc zerowa.
    assert swiezy["hit_rate"] is None, f"hit_rate = {swiezy['hit_rate']!r}"
    assert swiezy["total_pnl"] is None, "brak zamknietej pozycji oddany jako 0,00 zysku"
    assert swiezy["closed_count"] == 0
    assert swiezy["unsettled_count"] == 40, (
        "liczba decyzji nierozliczonych ma byc widoczna osobno — bez niej "
        "trafnosc jest liczba bez mianownika")

    assert rsi["hit_rate"] == pytest.approx(0.75)
    assert rsi["closed_count"] == 4
    assert rsi["unsettled_count"] == 6

    # Straznik na samym zapytaniu: atrapa nie wykonuje SQL, wiec to jedyne
    # miejsce, w ktorym da sie sprawdzic, ze srednia trafnosci pomija wiersze
    # bez rozliczenia, zamiast wliczac je jako zero.
    sql, _ = d.zapytanie_z("journal.decision_contribution")
    m = re.search(r"avg\(CASE WHEN.*?END\)(.*?)AS hit_rate", sql)
    assert m, "nie znalazlem wyrazenia liczacego hit_rate"
    assert "FILTER" in m.group(1) and "IS NOT NULL" in m.group(1), (
        "srednia trafnosci nie jest ograniczona do rozliczonych decyzji: "
        + m.group(0))


# ------------------------------------------------- 6. kanaly newsow

def test_wylaczony_kanal_NIE_ma_statusu_ok(monkeypatch):
    """Worker newsow pyta wylacznie kanaly `WHERE enabled`, wiec kanal
    wylaczony trzyma zamrozone `last_status = 'ok'` z chwili sprzed wylaczenia.

    Stary sukces nie jest dowodem pracy. Stan wylaczonego kanalu rozstrzyga
    teraz API — jednym polem, a nie regula powtorzona w komponencie."""
    from services.api.routers import sources as modul

    wspolne = {"url": "https://x/rss", "engine": "crypto", "poll_interval_s": 600,
               "last_error": None, "articles_seen": 300, "articles_kept": 12,
               "last_24h": 0}
    trzy_dni_temu = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)

    podlacz(monkeypatch, modul, Dyspozytor(("news.feed", [
        {"id": 1, "name": "wylaczony", "enabled": False, "last_status": "ok",
         "last_polled_at": trzy_dni_temu, **wspolne},
        {"id": 2, "name": "pracujacy", "enabled": True, "last_status": "ok",
         "last_polled_at": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
         **wspolne},
        {"id": 3, "name": "nigdy nieodpytany", "enabled": True, "last_status": None,
         "last_polled_at": None, **wspolne},
    ])))

    wylaczony, pracujacy, nowy = asyncio.run(modul.feeds("crypto"))

    assert wylaczony["stan"] == "wylaczony"
    assert pracujacy["stan"] == "ok"
    assert nowy["stan"] == "nigdy_nieodpytany"

    # Surowy ostatni odczyt ZOSTAJE: to jedyny slad tego, jak kanal konczyl
    # prace, i po wylaczeniu jest jedyna diagnostyka, jaka zostala.
    assert wylaczony["last_status"] == "ok"
    assert wylaczony["last_polled_at"] == trzy_dni_temu.isoformat()


# ------------------------------------------------- 7. kapital laczny: waluty i doba

GODZINA = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
# Ostatnim punktem siatki jest `now()`, a NIE pelna godzina — patrz UNION
# w zapytaniu `kapital_laczny`. Ta niepelna godzina jest cala trescia wady
# odniesienia "24 h", wiec atrapa musi ja odtwarzac.
TERAZ = GODZINA + timedelta(minutes=17)


def _siatka(silniki: dict[str, float], godzin: int = 30) -> list[dict]:
    """Wiersze takie, jakie oddaje zapytanie siatki: (t, engine, equity).

    Kapital rosnie o 1 na godzine, zeby kazda chwila miala wlasna, rozpoznawalna
    liczbe — inaczej pomylka o godzine byla by w tescie niewidoczna.
    """
    czasy = [GODZINA - timedelta(hours=k) for k in range(godzin, -1, -1)] + [TERAZ]
    rows = []
    for i, t in enumerate(czasy):
        for engine, baza in silniki.items():
            rows.append({"t": t, "engine": engine, "equity": Decimal(baza + i)})
    return rows


def _waluty(**per_engine: str) -> tuple:
    return ("ref.instrument", [
        {"engine": e, "handlowe": [w], "wszystkie": [w]} for e, w in per_engine.items()])


def test_kafel_24h_cofa_sie_o_dobe_ZEGAROWA_a_nie_o_24_kroki(monkeypatch):
    """`wart[-25]` cofalo o 24 kroki siatki godzinowej, ale ostatnim punktem
    siatki jest `now()`, a nie pelna godzina. Realny odstep wynosil wiec
    23 godziny plus to, co uplynelo z biezacej godziny — i nikt tego nie
    widzial, bo etykieta mowila "24 h".

    Kapital rosnie tu o 1 na godzine, wiec pomylka o jedna godzine jest
    pomylka o dokladnie 1 w wyniku."""
    from services.api.routers import kapital_laczny as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        ("exec.equity_point q", _siatka({"crypto": 1000}, godzin=30)),
        _waluty(crypto="USDC"),
    ))

    d = asyncio.run(modul.laczny(days=45, mode="demo"))
    z = d["zmiany"]["crypto"]

    # Siatka: 31 pelnych godzin (1000..1030) + punkt `now()` o wartosci 1031.
    # Doba wstecz od `now()` wypada miedzy GODZINA-24h (1006) a GODZINA-23h
    # (1007); wolno wziac wylacznie punkt NIE POZNIEJSZY niz granica.
    assert z["teraz"] == 1031
    assert z["doba"] == 25, (
        f"doba = {z['doba']} — 24 zna czy odniesienie sprzed 23 godzin, "
        f"czyli kafel opisuje krotszy okres, niz mowi jego etykieta")
    assert z["od_startu"] == 31


def test_brak_punktu_sprzed_doby_daje_null_a_nie_zmiane_od_startu(monkeypatch):
    """Silnik dzialajacy szesc godzin nie ma czego porownac z doba wstecz.
    Poprzednio odniesieniem stawal sie wtedy PIERWSZY punkt serii, wiec pola
    "24 h" i "okno" pokazywaly te sama liczbe obok siebie, bez adnotacji."""
    from services.api.routers import kapital_laczny as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        ("exec.equity_point q", _siatka({"crypto": 1000}, godzin=6)),
        _waluty(crypto="USDC"),
    ))

    z = asyncio.run(modul.laczny(days=45, mode="demo"))["zmiany"]["crypto"]

    assert z["doba"] is None and z["doba_pct"] is None, (
        f"doba = {z['doba']} — to jest zmiana od startu pod etykieta 24 h")
    assert z["od_startu"] == 7, "zmiana od startu ma dalej byc policzona"


def test_RAZEM_nie_sumuje_dwoch_roznych_walut(monkeypatch):
    """Krypto jest kwotowane w USDC, Alpaca w USD, a `exec.equity_point` nie ma
    kolumny waluty. `sum(wart)` dodawalo wiec dolary do USDC i podawalo wynik
    jako jedna liczbe — gruba linia "Razem", czyli najbardziej rzucajaca sie
    w oczy krzywa na tej zakladce.

    Kursu miedzy tymi walutami NIE MA w `market.fx_rate` (tabela trzyma tylko
    kurs waluty pomiaru do waluty odniesienia wlasciciela), wiec przeliczyc nie
    ma czym. Podstawienie 1:1 byloby tym samym bledem co zero za brak pomiaru:
    liczba wygladajaca na przeliczona."""
    from services.api.routers import kapital_laczny as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        ("exec.equity_point q", _siatka({"crypto": 1000, "equity": 5000}, godzin=30)),
        _waluty(crypto="USDC", equity="USD"),
    ))

    d = asyncio.run(modul.laczny(days=45, mode="demo"))

    assert d["waluty"] == {"crypto": "USDC", "equity": "USD"}
    assert all(p["razem"] is None for p in d["punkty"]), (
        "suma dwoch walut podana jako jedna liczba")
    assert d["zmiany"]["razem"]["teraz"] is None

    powod = d["razem_powod"] or ""
    assert "USDC" in powod and "USD" in powod, (
        f"powod braku sumy nie nazywa walut: {powod!r}")

    # Kazda seria z osobna ma byc dalej policzona — brak sumy nie moze kasowac
    # dwoch prawidlowych krzywych.
    assert d["zmiany"]["crypto"]["teraz"] == 1031
    assert d["zmiany"]["equity"]["teraz"] == 5031


def test_RAZEM_sumuje_gdy_waluta_jest_jedna(monkeypatch):
    """Kontrola dla testu wyzej: sama wielosc silnikow nie jest powodem, zeby
    nie sumowac. Powodem jest rozjazd jednostek."""
    from services.api.routers import kapital_laczny as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        ("exec.equity_point q", _siatka({"crypto": 1000, "equity": 5000}, godzin=30)),
        _waluty(crypto="USDC", equity="USDC"),
    ))

    d = asyncio.run(modul.laczny(days=45, mode="demo"))

    assert d["razem_powod"] is None
    assert d["punkty"][-1]["razem"] == 1031 + 5031
    assert d["zmiany"]["razem"]["teraz"] == 1031 + 5031


def test_nieznana_waluta_silnika_tez_wstrzymuje_sume(monkeypatch):
    """Silnik, ktorego waluty nie da sie ustalic jednoznacznie, nie moze wejsc
    do sumy "na oko". Jednostka zgadnieta jest gorsza niz jej brak: liczba
    wyglada wtedy na porownywalna."""
    from services.api.routers import kapital_laczny as modul

    podlacz(monkeypatch, modul, Dyspozytor(
        ("exec.equity_point q", _siatka({"crypto": 1000, "equity": 5000}, godzin=30)),
        ("ref.instrument", [
            {"engine": "crypto", "handlowe": ["USDC"], "wszystkie": ["USDC", "USDT"]},
            {"engine": "equity", "handlowe": None, "wszystkie": ["USD", "EUR"]},
        ]),
    ))

    d = asyncio.run(modul.laczny(days=45, mode="demo"))

    assert d["waluty"] == {"crypto": "USDC", "equity": None}
    assert d["razem_powod"] and "equity" in d["razem_powod"]
    assert all(p["razem"] is None for p in d["punkty"])
