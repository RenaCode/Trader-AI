"""Straże bitemporalne: co NAPRAWDĘ wykrywają, a czego nie mogą wykryć.

DLACZEGO TEN PLIK ISTNIEJE

„Nie znaleźliśmy przecieku" jest zdaniem o nas, nie o systemie, dopóki nie
pokażemy, że wykrywacz w ogóle potrafi zapalić się na znanej złej próbce.
Dlatego każdy test tutaj NAJPIERW wstrzykuje wiersz z przyszłości i sprawdza,
że strażnik go odrzuca, a dopiero potem — że wiersz poprawny przechodzi.

Dwie strażnice, dwie różne osłony:

  * `feat.as_of` (migracja 0004) — odczyt produkcyjny. Odrzuca wiersz, którego
    `ingested_at` jest PÓŹNIEJSZY niż chwila decyzji.
  * `train.load_panel` — odczyt treningowy. Odrzuca wiersz, który przyszedł
    do bazy później niż dwie świece po `event_time`.

TRZECI TEST JEST NAJWAŻNIEJSZY I MÓWI COŚ NIEPRZYJEMNEGO: potok cech sam
wypełnia `ingested_at` wartością `znane_od(event_time)`, więc KAŻDY wiersz,
który zapisze, przechodzi strażnika treningowego z definicji. Ta straż nie
chroni przed błędem w liczeniu cechy — chroni wyłącznie przed wierszami
wpisanymi inną drogą. Milczenie tego strażnika nie jest dowodem na nic.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.engine.horizons import H1H, H4H, H5M
from services.ingest.features import ROLES, znane_od

T = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)      # świeca 08:00–12:00

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO_TRENERA = (KORZEN / "services" / "engine" / "train.py").read_text()
ZRODLO_AS_OF = (KORZEN / "db" / "migrations"
                / "0004_features_and_signals.sql").read_text()


def test_warunki_przepisane_tu_stoja_NAPRAWDE_w_kodzie():
    """Bez tego cały plik sprawdzałby wyłącznie moje przepisanie warunku.

    Testy niżej odtwarzają predykaty SQL w Pythonie — inaczej wymagałyby bazy.
    Odtworzenie jest warte tyle, co dowód, że oryginał nadal tak wygląda:
    usunięcie strażnika z kodu musi wywrócić TEN plik, a nie przejść w nim cicho.
    """
    assert "fv.ingested_at <= fv.event_time + 2 * %(bar)s::interval" in ZRODLO_TRENERA,         "straż bitemporalna zniknęła z train.load_panel albo zmieniła kształt"
    assert "fv.ingested_at <= p_as_of" in ZRODLO_AS_OF,         "straż bitemporalna zniknęła z feat.as_of"
    assert "fv.event_time  >= p_as_of - p_max_staleness" in ZRODLO_AS_OF,         "as_of przestało odcinać cechy przeterminowane"


def przez_as_of(event_time, ingested_at, as_of, staleness_min):
    """Warunek z `feat.as_of` — przepisany z migracji 0004, wiersz po wierszu."""
    return (event_time <= as_of
            and ingested_at <= as_of
            and event_time >= as_of - timedelta(minutes=staleness_min))


def przez_load_panel(event_time, ingested_at, h):
    """Warunek z `train.load_panel`: fv.ingested_at <= fv.event_time + 2 * bar."""
    return ingested_at <= event_time + 2 * timedelta(minutes=h.bar_minutes)


# --- straż produkcyjna -------------------------------------------------------

def test_as_of_odrzuca_ceche_ktora_w_tamtej_chwili_jeszcze_nie_istniala():
    """ZŁA PRÓBKA: cecha świecy 08:00 zapisana dopiero o 12:00:03, czytana o 12:00:00.

    To jest dokładnie cecha liczona z ZAMKNIĘCIA świecy, o którą pytamy zanim
    świeca się domknęła. Bez tego warunku model uczyłby się na niej.
    """
    decyzja = T + timedelta(hours=4)                 # 12:00:00
    dostepna_od = znane_od(T, H4H)                   # 12:00:03
    assert not przez_as_of(T, dostepna_od, decyzja, H4H.max_staleness_min), \
        "as_of przepuściło cechę, której w chwili decyzji jeszcze nie policzono"


def test_as_of_przepuszcza_te_sama_ceche_trzy_sekundy_pozniej():
    """Kanarek odwrotny: bez niego poprzedni test zdałby przy strażniku,
    który odrzuca WSZYSTKO."""
    assert przez_as_of(T, znane_od(T, H4H), T + timedelta(hours=4, seconds=3),
                       H4H.max_staleness_min)


def test_as_of_odrzuca_ceche_o_ZNACZNIKU_z_przyszlosci():
    """ZŁA PRÓBKA drugiego rodzaju: `event_time` po chwili decyzji."""
    przyszla = T + timedelta(hours=4)
    assert not przez_as_of(przyszla, przyszla, T + timedelta(hours=1), 600)


# --- straż treningowa --------------------------------------------------------

@pytest.mark.parametrize("h", [H5M, H1H, H4H])
def test_load_panel_odrzuca_ceche_ktora_doszla_za_pozno(h):
    """ZŁA PRÓBKA: wartość świecy T wpisana trzy świece po niej.

    Tak wygląda cecha policzona z danych, których w chwili T nie było —
    na przykład sentyment z artykułu zescrapowanego z opóźnieniem.
    """
    spozniona = T + 3 * timedelta(minutes=h.bar_minutes)
    assert not przez_load_panel(T, spozniona, h), \
        f"strażnik {h.bar} przepuścił wartość spóźnioną o trzy świece"
    assert przez_load_panel(T, znane_od(T, h), h), \
        "ten sam strażnik odrzuca poprawne datowanie — test nie mierzy strażnika"


def test_stare_datowanie_bylo_by_zlapane_gdyby_wrocilo():
    """Regresja na błąd opisany w `znane_od`: `ingested_at = t + 3 sekundy`.

    Ten wariant przechodzi strażnika TRENINGOWEGO (jest wcześniejszy, nie
    późniejszy) i dlatego łapie go dopiero straż produkcyjna — cecha świecy
    08:00–12:00 byłaby wtedy „znana" o 08:00:03, czyli cztery godziny przed
    swoim własnym zamknięciem.
    """
    blednie = T + timedelta(seconds=3)
    assert przez_load_panel(T, blednie, H4H), \
        "test nie odtwarza starego, błędnego datowania"
    # I to jest cała szkoda: as_of oddałoby ją decyzji z 08:05.
    assert przez_as_of(T, blednie, T + timedelta(minutes=5), H4H.max_staleness_min)
    # Przy poprawnym datowaniu ta sama decyzja jej NIE dostaje.
    assert not przez_as_of(T, znane_od(T, H4H), T + timedelta(minutes=5),
                           H4H.max_staleness_min)


# --- czego ta straż NIE potrafi ---------------------------------------------

@pytest.mark.parametrize("h", [H5M, H1H, H4H])
def test_straz_treningowa_nie_moze_zapalic_sie_na_wlasnym_potoku(h):
    """KAŻDA rola zapisywana przez potok przechodzi strażnika Z DEFINICJI.

    `insert_values` wpisuje `ingested_at = znane_od(event_time)` dla wszystkich
    ról — także przy `ON CONFLICT DO UPDATE`, więc PRZELICZENIE cechy zawsze
    twierdzi, że nowa wartość była znana od zamknięcia świecy.

    Wniosek, który trzeba czytać razem z każdym „brak przecieku": warunek
    `ingested_at <= event_time + 2 * bar` nie sprawdza, czy cecha jest funkcją
    przeszłości. Sprawdza, czy ktoś wpisał wiersz Z POMINIĘCIEM potoku. Błąd
    w samym liczeniu cechy przejdzie tędy bez śladu i musi być złapany
    gdzie indziej — przy okienkach i przy `na_siatce`.
    """
    for rola in ROLES:
        assert przez_load_panel(T, znane_od(T, h), h), (
            f"rola {rola}: datowanie potoku nie mieści się w oknie strażnika — "
            f"to znaczy, że jedno z dwóch się zmieniło i rozjechały się")
    # Margines: potok datuje na +1 świecę, strażnik dopuszcza +2.
    luz = (T + 2 * timedelta(minutes=h.bar_minutes)) - znane_od(T, h)
    assert luz == timedelta(minutes=h.bar_minutes) - timedelta(seconds=3), (
        "zapas strażnika przestał wynosić jedną świecę minus opóźnienie "
        "obliczeniowe — sprawdź, czy nie zmieniono jednej strony bez drugiej")
