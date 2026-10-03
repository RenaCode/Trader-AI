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

Od migracji 0070 POPRAWKA wartości ma własną datę (`revised_at`) i trener oraz
kontrola startowa silnika liczą rewizje po czasie decyzji osobno — to pilnuje
`tests/test_rewizja_cechy.py`. Tutejsza straż `ingested_at` dalej ich nie widzi
i nie ma widzieć: rewizja nie jest wartością znaną za późno, tylko inną.
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
#
# SPARAMETRYZOWANE PO HORYZONTACH 2026-09-25. Do tego dnia oba testy niżej
# stały na `H4H` wpisanym na sztywno, tak samo jak regresja starego datowania,
# choć produkcja liczy cechy na DWÓCH horyzontach krypto (`4h` i `1h`), a tor
# equity WYŁĄCZNIE na `1h` (`trainerCron.equity.horyzonty`).
#
# ZMIERZONE MUTACYJNIE na zestawie sprzed tej zmiany: wada w datowaniu wywracała
# 8 testów dla wszystkich horyzontów razem, a dla samej świecy 1h — JEDEN.
# Horyzont, którym ma handlować equity, był więc pilnowany osiem razy słabiej
# niż ten, którym handluje krypto, i różnica wychodziła wyłącznie z tego, że
# `H4H` przypadkiem trafiło do tych trzech testów.
#
# WSZYSTKIE PRZESUNIĘCIA CZASU LICZĄ SIĘ TERAZ Z `h.bar_minutes`, a nie z liczby
# godzin: `timedelta(hours=4)` dla świecy 1h opisywałoby czwartą świecę po tej
# mierzonej i test przeszedłby z innego powodu niż mierzony.


@pytest.mark.parametrize("h", [H5M, H1H, H4H])
def test_as_of_odrzuca_ceche_ktora_w_tamtej_chwili_jeszcze_nie_istniala(h):
    """ZŁA PRÓBKA: cecha świecy T zapisana o `zamknięcie + 3 s`, czytana
    DOKŁADNIE w chwili zamknięcia.

    To jest cecha liczona z ZAMKNIĘCIA świecy, o którą pytamy, zanim świeca się
    domknęła. Bez tego warunku model uczyłby się na niej — i to na każdym
    horyzoncie, bo `znane_od` dokłada `bar_minutes` niezależnie od świecy.
    """
    decyzja = T + timedelta(minutes=h.bar_minutes)    # chwila zamknięcia świecy
    dostepna_od = znane_od(T, h)                      # zamknięcie + 3 s
    assert not przez_as_of(T, dostepna_od, decyzja, h.max_staleness_min), \
        f"as_of ({h.bar}) przepuściło cechę, której w chwili decyzji nie policzono"


@pytest.mark.parametrize("h", [H5M, H1H, H4H])
def test_as_of_przepuszcza_te_sama_ceche_trzy_sekundy_pozniej(h):
    """Kanarek odwrotny: bez niego poprzedni test zdałby przy strażniku,
    który odrzuca WSZYSTKO."""
    assert przez_as_of(T, znane_od(T, h), znane_od(T, h), h.max_staleness_min), (
        f"strażnik {h.bar} odrzuca cechę czytaną w chwili, w której stała się "
        f"znana — mierzyłby wtedy coś innego niż przeciek przyszłości")


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


@pytest.mark.parametrize("h", [H5M, H1H, H4H])
def test_stare_datowanie_bylo_by_zlapane_gdyby_wrocilo(h):
    """Regresja na błąd opisany w `znane_od`: `ingested_at = t + 3 sekundy`.

    Ten wariant przechodzi strażnika TRENINGOWEGO (jest wcześniejszy, nie
    późniejszy) i dlatego łapie go dopiero straż produkcyjna — cecha świecy
    byłaby wtedy „znana" trzy sekundy po swoim OTWARCIU, czyli cały czas trwania
    świecy przed jej własnym zamknięciem.

    Szkoda rośnie z długością świecy (przy 4h to cztery godziny wiedzy
    z przyszłości), ale ISTNIEJE NA KAŻDYM horyzoncie — także na `1h`, którym
    ma handlować tor equity.
    """
    blednie = T + timedelta(seconds=3)
    assert przez_load_panel(T, blednie, h), \
        f"test nie odtwarza starego, błędnego datowania dla {h.bar}"
    # I to jest cała szkoda: as_of oddałoby ją decyzji podjętej W TRAKCIE świecy.
    # Chwila liczona z `bar_minutes`, a nie stałe 5 minut: dla świecy 5m stałe
    # 5 minut wypadałoby DOKŁADNIE na jej zamknięciu i test mierzyłby granicę,
    # a nie wnętrze świecy.
    w_trakcie = T + timedelta(minutes=h.bar_minutes) / 2
    assert przez_as_of(T, blednie, w_trakcie, h.max_staleness_min), (
        f"stare datowanie ({h.bar}) nie przeszłoby przez as_of — test nie "
        f"odtwarza wady, którą ma pilnować")
    # Przy poprawnym datowaniu ta sama decyzja jej NIE dostaje.
    assert not przez_as_of(T, znane_od(T, h), w_trakcie, h.max_staleness_min), (
        f"poprawne datowanie ({h.bar}) też przechodzi — strażnik nie odróżnia "
        f"tych dwóch przypadków, czyli nie pilnuje niczego")


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
