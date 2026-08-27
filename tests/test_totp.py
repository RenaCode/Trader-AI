"""Testy weryfikacji TOTP - ze szczegolnym naciskiem na ochrone przed powtorzeniem."""
import time

import pyotp
import pytest

from services.api.security import TOTP_STEP, verify_totp

SEKRET = pyotp.random_base32()


def kod_dla(offset_krokow: int = 0) -> tuple[str, int]:
    krok = int(time.time()) // TOTP_STEP + offset_krokow
    return pyotp.TOTP(SEKRET).at(krok * TOTP_STEP), krok


def test_biezacy_kod_przechodzi():
    kod, krok = kod_dla(0)
    assert verify_totp(SEKRET, kod) == krok


def test_kod_z_poprzedniego_kroku_przechodzi():
    """Tolerancja rozjazdu zegara o jeden krok w kazda strone."""
    kod, krok = kod_dla(-1)
    assert verify_totp(SEKRET, kod) == krok


def test_kod_sprzed_dwoch_krokow_odpada():
    kod, _ = kod_dla(-2)
    assert verify_totp(SEKRET, kod) is None


def test_bledny_kod_odpada():
    # 000000 moze teoretycznie byc poprawnym kodem; bierzemy kod na pewno inny
    # od biezacego, zeby test nie mrugal raz na milion uruchomien.
    biezacy, _ = kod_dla(0)
    falszywy = "111111" if biezacy != "111111" else "222222"
    assert verify_totp(SEKRET, falszywy) is None


def test_kod_nie_bedacy_cyframi_odpada():
    assert verify_totp(SEKRET, "abcdef") is None


def test_ten_sam_kod_nie_przechodzi_dwa_razy():
    """SEDNO ochrony. Kod przechwycony raz nie moze zadzialac ponownie."""
    kod, krok = kod_dla(0)
    assert verify_totp(SEKRET, kod, last_step=None) == krok
    assert verify_totp(SEKRET, kod, last_step=krok) is None, \
        "ten sam krok czasowy musi zostac odrzucony za drugim razem"


def test_starszy_kod_odpada_po_uzyciu_nowszego():
    """Kod z poprzedniego kroku jest sam w sobie poprawny, ale juz nieaktualny."""
    kod_stary, krok_stary = kod_dla(-1)
    _, krok_biezacy = kod_dla(0)
    assert verify_totp(SEKRET, kod_stary, last_step=krok_biezacy) is None


def test_nastepny_kod_przechodzi_mimo_zapisanego_kroku():
    """Kanarek: ochrona nie moze blokowac PRAWIDLOWYCH kolejnych logowan."""
    kod, krok = kod_dla(0)
    assert verify_totp(SEKRET, kod, last_step=krok - 1) == krok


def test_kod_z_innego_sekretu_odpada():
    inny = pyotp.random_base32()
    kod = pyotp.TOTP(inny).now()
    assert verify_totp(SEKRET, kod) is None
