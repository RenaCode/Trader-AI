"""Odcisk uniwersum — tożsamość cechy przekrojowej.

`rel_strength` udaje własność instrumentu, a jest własnością PARY
(instrument, uniwersum). Percentyl BTC wśród dziesięciu par to inna liczba niż
wśród dwudziestu czterech — ta sama nazwa, dwa różne znaczenia.

Dopóki nazwa nie mówi, dla jakiego zbioru wartość policzono, zmiana uniwersum
zostawia w bazie wiersze, które WYGLĄDAJĄ na aktualne: nie ma awarii, nie ma
pustego pola, jest liczba — tyle że opisująca inny świat.
"""
from pathlib import Path

import pytest

from services.engine.horizons import Horizon
from services.engine.uniwersum import nazwa_przekrojowa, odcisk

DZIESIATKA = ["BTCUSDC", "ETHUSDC", "XRPUSDC", "SOLUSDC", "DOGEUSDC",
              "ADAUSDC", "LINKUSDC", "ARBUSDC", "SUIUSDC", "NEARUSDC"]


def test_odcisk_nie_zalezy_od_kolejnosci_ani_wielkosci_liter():
    """Wymaganie, nie wygoda: silnik i trener czytają uniwersum osobnymi
    zapytaniami. Gdyby odcisk zależał od kolejności wierszy, rozjazd sortowania
    po stronie bazy zamieniłby cechę w martwą — cicho, bo brak wartości wygląda
    tak samo jak brak sygnału."""
    assert odcisk(DZIESIATKA) == odcisk(reversed(DZIESIATKA))
    assert odcisk(DZIESIATKA) == odcisk([s.lower() for s in DZIESIATKA])
    assert odcisk(DZIESIATKA) == odcisk(DZIESIATKA + [" BTCUSDC "])


def test_inny_zbior_daje_inny_odcisk():
    """Kanarek do poprzedniego. Odcisk odporny na wszystko byłby stałą."""
    assert odcisk(DZIESIATKA) != odcisk(DZIESIATKA[:-1])
    assert odcisk(DZIESIATKA) != odcisk(DZIESIATKA + ["BNBUSDT"])


def test_puste_uniwersum_jest_bledem_a_nie_odciskiem():
    """Odcisk pustego zbioru byłby poprawną nazwą cechy, pod którą nikt nigdy
    nic nie zapisze — czyli cichą śmiercią sygnału."""
    with pytest.raises(ValueError):
        odcisk([])


def test_odcisk_w_nazwie_jest_PRZEZROCZYSTY_dla_mapowania_rol():
    """Sygnały dostają rolę, nie nazwę. Gdyby odcisk psuł `role_of`, każda cecha
    przekrojowa trafiałaby do słownika pod kluczem, którego żaden sygnał nie
    czyta — i byłaby martwa mimo poprawnie policzonych wartości."""
    nazwa = nazwa_przekrojowa("rel_strength__4h", DZIESIATKA)
    assert Horizon.role_of(nazwa) == "rel_strength"
    # Także z sufiksem wersji, który dokleja feat.as_of().
    assert Horizon.role_of(f"{nazwa}@3") == "rel_strength"


def test_panel_trenera_filtruje_po_odcisku():
    """NAJWAŻNIEJSZY test tego pliku.

    Trener agreguje cechy po ROLI (`jsonb_object_agg`). Dwa warianty cechy
    przekrojowej, policzone dla różnych zbiorów, zderzyłyby się w jednym kluczu
    i wygrałby przypadkowy — więc pierwszy eksperyment na innym uniwersum po
    cichu zmieniałby wynik przebiegu produkcyjnego.
    """
    zrodlo = Path("services/engine/train.py").read_text(encoding="utf-8")
    assert "fd.params->>'uniwersum'" in zrodlo, "panel nie filtruje po odcisku"
    assert '"fp": fp' in zrodlo


def test_silnik_filtruje_po_odcisku_i_zna_swoje_uniwersum():
    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    assert "params->>'uniwersum'" in zrodlo
    assert "horizon_feature_ids(cur, h, [i[\"symbol\"] for i in instrumenty])" in zrodlo, (
        "silnik nie przekazuje własnego uniwersum do wyboru cech")


def test_cechy_per_instrument_przechodza_ZAWSZE():
    """Kanarek do dwóch powyższych. Gdyby filtr obejmował wszystkie cechy,
    a nie tylko przekrojowe, RSI i ATR znikłyby razem z nimi — i model
    zostałby bez wejść."""
    for plik in ("services/engine/train.py", "services/engine/loop.py"):
        zrodlo = Path(plik).read_text(encoding="utf-8")
        assert "'uniwersum' IS NULL" in zrodlo, (
            f"{plik}: brak przepustki dla cech bez odcisku")


def test_trener_MOWI_gdy_cechy_przekrojowej_brak():
    """Poprawne zachowanie przy braku to martwy sygnał, a nie wartość z innego
    zbioru. Ale cichy brak wygląda jak sygnał bez treści — musi być powiedziany
    wprost, razem z komendą, która to naprawia."""
    zrodlo = Path("services/engine/train.py").read_text(encoding="utf-8")
    assert "brak cech przekrojowych dla tego uniwersum" in zrodlo
    assert "--tylko-przekroj" in zrodlo, "komunikat nie podaje lekarstwa"


def test_prog_MIN_UNIVERSE_dotyczy_zbioru_rankingu():
    """Bez --instrumenty pętla per instrument idzie po WSZYSTKICH aktywnych (34),
    a ranking po uniwersum decyzyjnym (10). Sprawdzanie tej pierwszej liczby
    przepuszczałoby ranking po trzech parach, dopóki w bazie leży historia
    trzydziestu czterech — czyli dokładnie wtedy, gdy próg jest potrzebny."""
    zrodlo = Path("services/ingest/features.py").read_text(encoding="utf-8")
    assert "if len(uniwersum) < MIN_UNIVERSE:" in zrodlo
    assert "if len(instruments) < MIN_UNIVERSE:" not in zrodlo
