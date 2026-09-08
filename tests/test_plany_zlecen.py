"""Zlecenie z limitem to PLAN, nie fakt.

DLACZEGO TO JEST OSOBNY PLIK

Do 2026-09-06 pętla decyzyjna zapisywała wypełnienie natychmiast, przyjmując,
że limit się wypełnił. Symulacja walk-forward modelowała to od początku
(`portfolio._wypelnione`), produkcja nie — czyli krzywa kapitału i wynik
kalibracji opisywały dwie różne strategie.

Błąd był systematyczny w jedną stronę: zlecenia, które się NIE wypełniają, to
zwykle te, przy których rynek uciekł w naszą stronę — a więc najbardziej
zyskowne. Optymizm nie rozkładał się losowo.
"""
import re
from pathlib import Path

import pytest

from services.engine.portfolio import _wypelnione

ZRODLO = Path("services/engine/loop.py").read_text()


def test_petla_uzywa_reguly_symulatora_a_nie_wlasnej_kopii():
    """Kanarek. Dwie kopie tej samej reguły rozjadą się przy pierwszej zmianie,
    a różnica będzie niewidoczna — obie strony wyglądają poprawnie z osobna."""
    assert "from services.engine.portfolio import _wypelnione" in ZRODLO, \
        "loop.py nie importuje reguły wypełnienia z symulatora"
    kod = "\n".join(l for l in ZRODLO.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"^\s*def\s+_?wypelnione", kod, re.M), \
        "loop.py ma własną kopię reguły wypełnienia"


def test_petla_nie_zapisuje_wypelnienia_z_limitu_od_razu():
    """Przy `limit` decyzja ma iść przez `zaplanuj`, nie przez `execute_paper`."""
    assert "zaplanuj(cur, did," in ZRODLO, "brak ścieżki planowania"
    assert "rozstrzygnij_plany" in ZRODLO, "brak rozstrzygania planów"


# --- semantyka reguly, wspolna dla symulacji i produkcji --------------------
def test_kupno_wypelnia_sie_gdy_rynek_ZSZEDL_do_limitu():
    assert _wypelnione(100.0, {"low": 99.0, "high": 105.0}, kupno=True)
    assert not _wypelnione(100.0, {"low": 100.5, "high": 108.0}, kupno=True)


def test_sprzedaz_wypelnia_sie_gdy_rynek_DOSZEDL_do_limitu():
    assert _wypelnione(100.0, {"low": 95.0, "high": 100.5}, kupno=False)
    assert not _wypelnione(100.0, {"low": 92.0, "high": 99.5}, kupno=False)


def test_dotkniecie_limitu_co_do_grosza_wypelnia():
    """Granica jest domknięta z obu stron — inaczej model różniłby się od
    symulacji o przypadki brzegowe, których nikt by nie zauważył."""
    assert _wypelnione(100.0, {"low": 100.0, "high": 101.0}, kupno=True)
    assert _wypelnione(100.0, {"low": 99.0, "high": 100.0}, kupno=False)


def test_ruch_ktory_ucieka_NIE_wypelnia_sie():
    """Sedno selekcji negatywnej: gwałtowny ruch w naszą stronę zostawia nas
    bez pozycji. To nie jest wada modelu, to jego treść."""
    # Kupujemy po 100, rynek od razu wystrzeliwuje w gore i nie wraca.
    assert not _wypelnione(100.0, {"low": 100.4, "high": 112.0}, kupno=True)


def test_plan_wazny_jest_przez_jedna_nastepna_swiece():
    """`valid_until` = początek świecy + DWIE świece, czyli koniec świecy T+1.

    Krótszy termin kasowałby plan, zanim świeca, w której ma się wypełnić,
    zdąży się domknąć; dłuższy wypełniałby go na świecy, której decyzja
    nigdy nie dotyczyła.
    """
    assert "2 * h.bar_minutes" in ZRODLO, \
        "termin ważności planu nie odpowiada jednej następnej świecy"


def test_swieca_trwajaca_nie_moze_byc_oceniana():
    """Kanarek na lukę znalezioną przy weryfikacji na produkcji.

    `zakres_swiecy` zwracała dane dla świecy, która się jeszcze nie domknęła —
    bo w środku były już świece źródłowe. Chronił przed tym wyłącznie warunek
    u wywołującego, a ten przestaje wystarczać po przestoju silnika, gdy minie
    termin ważności planu. Zakres trwającej świecy jeszcze rośnie, więc
    orzekanie na niej wypełnienia byłoby zgadywaniem.
    """
    assert "if koniec > datetime.now(timezone.utc):" in ZRODLO, \
        "zakres_swiecy nie odmawia oceny świecy, która się nie domknęła"


# --- cena decyzyjna --------------------------------------------------------
def test_cena_decyzyjna_to_zamkniecie_swiecy_a_nie_ostatnia_minuta():
    """Symulacja stawia limit po zamknięciu świecy; pętla musi tak samo.

    Pętla budzi się co 300 s, więc `mark_price(now)` oddaje cenę nawet pięć
    minut późniejszą. Zmierzone na dziesięciu instrumentach uniwersum: mediana
    dryfu po pięciu minutach to 0,0808%, czyli 40% całej rundy kosztowej
    (0,200%), a dziewiąty decyl 0,3195% — półtora budżetu.

    Cena limitu wprost decyduje o tym, czy zlecenie się wypełni, więc rozjazd
    tej wielkości zmienia mierzoną strategię, a nie tylko jej dokładność.
    """
    assert "swieca_dec = zakres_swiecy(cur, iid, h, bar_start)" in ZRODLO, \
        "pętla nie bierze ceny decyzyjnej z zamknięcia świecy"
    # mark_price zostaje wyłącznie jako ścieżka awaryjna i do wyceny pozycji
    assert "stats[\"bez_swiecy\"]" in ZRODLO, \
        "użycie ceny zastępczej nie jest zliczane — byłaby to cicha zamiana źródła"


# ============================ zero w regule to NIE jest komplet regul

from decimal import Decimal as D  # noqa: E402

def test_ZEROWY_krok_nie_jest_kompletem_regul():
    """Znalezione w audycie, poprawione 2026-09-08.

    `kompletne` sprawdzało wyłącznie „nie None", więc zero przechodziło jako
    wartość ZNANA: instrument był dopuszczany do egzekucji, a dopiero głęboko
    w zaokrąglaniu leciał goły `ValueError("krok musi byc dodatni")`. To nie
    jest błąd giełdy, więc nie łapie go obsługa błędów egzekucji — jeden
    instrument z zepsutą regułą wywracał CAŁY cykl decyzyjny.

    Zero nie jest hipotezą: `tick_size` to NUMERIC bez ograniczenia
    dodatniości, a wartości pochodzą wprost z odpowiedzi giełdy.
    """
    from services.engine.zlecenia import Filtry

    pelne = Filtry(krok_ilosci=D("0.001"), krok_ceny=D("0.01"),
                   min_wartosc=D("5"))
    assert pelne.kompletne

    for pole in ("krok_ilosci", "krok_ceny", "min_wartosc"):
        zepsute = Filtry(krok_ilosci=D("0.001"), krok_ceny=D("0.01"),
                         min_wartosc=D("5"))
        object.__setattr__(zepsute, pole, D("0"))
        assert not zepsute.kompletne, f"zero w {pole} nie może uchodzić za regułę"


def test_UJEMNY_krok_tez_nie_jest_kompletem():
    """Ujemna wartość jest jeszcze gorsza od zera: nie wywraca zaokrąglania
    wyjątkiem, tylko liczy w drugą stronę."""
    from services.engine.zlecenia import Filtry

    f = Filtry(krok_ilosci=D("0.001"), krok_ceny=D("-0.01"), min_wartosc=D("5"))
    assert not f.kompletne


def test_bramka_egzekucji_i_FILTRY_uzywaja_TEJ_SAMEJ_reguly():
    """Wcześniej stały tu dwie kopie i obie miały ten sam błąd. Rozjazd tej
    akurat reguły znaczy „dopuszczony do handlu instrument, którym nie da się
    handlować" — bramka mówi, że komplet reguł jest, a egzekucja się wywraca.
    """
    from services.ingest.instrumenty import niekompletne

    wiersze = [
        {"symbol": "OKUSDC", "tradeable": True, "lot_size": D("0.001"),
         "tick_size": D("0.01"), "min_notional": D("5")},
        {"symbol": "ZEROUSDC", "tradeable": True, "lot_size": D("0.001"),
         "tick_size": D("0"), "min_notional": D("5")},
        {"symbol": "BRAKUSDC", "tradeable": True, "lot_size": None,
         "tick_size": D("0.01"), "min_notional": D("5")},
        {"symbol": "NIEHANDLOWANY", "tradeable": False, "lot_size": None,
         "tick_size": None, "min_notional": None},
    ]
    assert niekompletne(wiersze) == ["BRAKUSDC", "ZEROUSDC"]
