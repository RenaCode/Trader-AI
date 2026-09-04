"""Termin waznosci klucza API.

DLACZEGO OSOBNY TEST

Wygasniecie klucza w polowie fazy demonstracyjnej nie objawia sie niczym
widocznym: dane rynkowe ida z hosta publicznego i nie wymagaja klucza, wiec
dashboard nadal wyglada zdrowo, a egzekucja po prostu przestaje dzialac.
Jedynym ostrzezeniem jest ta liczba - jesli sklamie, nie sklamie nic innego.
"""
from datetime import datetime, timedelta, timezone

from services.api.routers.credentials import _waznosc

TERAZ = datetime.now(timezone.utc)


def test_gielda_bez_terminu_nie_dostaje_zadnych_pol():
    """Brak reguly to brak pola, a nie termin "nigdy" - UI ma nic nie pokazac."""
    assert _waznosc(TERAZ, None) == {}
    assert _waznosc(TERAZ, 0) == {}


def test_brak_daty_utworzenia_nie_wywraca():
    assert _waznosc(None, 91) == {}


def test_swiezy_klucz_ma_prawie_pelny_termin():
    zostalo = _waznosc(TERAZ, 91)["days_left"]
    # Zaokraglenie w dol jest celowe: lepiej ostrzec dzien za wczesnie niz za pozno.
    assert zostalo in (90, 91)


def test_klucz_blisko_konca_wchodzi_w_prog_ostrzezenia():
    zostalo = _waznosc(TERAZ - timedelta(days=80), 91)["days_left"]
    assert zostalo <= 21, "UI ostrzega ponizej 21 dni - ten klucz musi sie tam miescic"


def test_klucz_po_terminie_ma_wartosc_UJEMNA_a_nie_zero():
    """Zero znaczyloby "wygasa dzis". Po terminie liczba musi byc ujemna,
    inaczej UI pokaze odliczanie zamiast komunikatu o wygasnieciu."""
    assert _waznosc(TERAZ - timedelta(days=100), 91)["days_left"] < 0


def test_termin_liczy_sie_od_utworzenia_klucza():
    utworzony = datetime(2026, 9, 4, 7, 20, 39, tzinfo=timezone.utc)
    wygasa = datetime.fromisoformat(_waznosc(utworzony, 91)["expires_at"])
    assert wygasa == utworzony + timedelta(days=91)
    assert wygasa.tzinfo is not None, "data bez strefy rozjedzie sie o godziny"
