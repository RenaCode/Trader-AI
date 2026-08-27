"""Testy prymitywow bezpieczenstwa: hasla, sesje, szyfrowanie sekretow.

Modul obraca kluczami do gieldy i sekretami 2FA, wiec kazda funkcja ma tu test
sprawdzajacy nie tylko "dziala", ale takze "nie da sie tego obejsc".
"""
import base64
import os

import pytest

from services.api.security import (
    SecretBox, backup_code_hash, constant_time_eq, hash_password,
    new_backup_codes, new_session_token, session_token_hash, verify_password,
)

KLUCZ = base64.b64encode(os.urandom(32)).decode()


# ------------------------------------------------------------------ hasla
def test_poprawne_haslo_przechodzi():
    h = hash_password("poprawne-haslo-123")
    ok, _ = verify_password(h, "poprawne-haslo-123")
    assert ok


def test_bledne_haslo_odpada():
    h = hash_password("poprawne-haslo-123")
    ok, _ = verify_password(h, "poprawne-haslo-124")
    assert not ok


def test_ten_sam_tekst_daje_rozne_hashe():
    """Sol per hash. Bez tego wyciek bazy pokazywalby, ktorzy uzytkownicy
    maja identyczne haslo."""
    assert hash_password("takie-samo") != hash_password("takie-samo")


def test_hash_uzywa_argon2id_z_zadanymi_parametrami():
    """Kanarek na ciche oslabienie parametrow.

    64 MiB pamieci i t=3 to znaczaco powyzej minimum OWASP (19 MiB, t=2).
    Obnizenie ich nie zepsuje zadnego testu funkcjonalnego - zepsuje ten.
    """
    h = hash_password("x")
    assert h.startswith("$argon2id$")
    assert "m=65536" in h, f"oczekiwano 64 MiB pamieci, jest: {h.split('$')[3]}"
    assert "t=3" in h
    assert "p=4" in h


def test_uszkodzony_hash_nie_wywraca_weryfikacji():
    ok, _ = verify_password("to-nie-jest-hash", "cokolwiek")
    assert not ok


# ------------------------------------------------------------------ sesje
def test_token_sesji_jest_losowy_i_dlugi():
    tokeny = {new_session_token()[0] for _ in range(50)}
    assert len(tokeny) == 50, "tokeny musza byc unikalne"
    assert all(len(t) >= 40 for t in tokeny)


def test_w_bazie_ladauje_hash_a_nie_token():
    """Wyciek tabeli sesji nie moze pozwalac na podszycie sie."""
    token, hash_do_bazy = new_session_token()
    assert token.encode() not in hash_do_bazy
    assert hash_do_bazy == session_token_hash(token)
    assert len(hash_do_bazy) == 32          # SHA-256


def test_hash_tokenu_jest_deterministyczny():
    token, _ = new_session_token()
    assert session_token_hash(token) == session_token_hash(token)


# ------------------------------------------------------------------ kody zapasowe
def test_kody_zapasowe_sa_unikalne_i_zahaszowane():
    kody = new_backup_codes(10)
    assert len(kody) == 10
    surowe = {k for k, _ in kody}
    assert len(surowe) == 10
    for surowy, h in kody:
        assert h == backup_code_hash(surowy)
        assert surowy not in h


def test_kod_zapasowy_odporny_na_wielkosc_liter_i_spacje():
    surowy, h = new_backup_codes(1)[0]
    assert backup_code_hash(f"  {surowy.upper()}  ") == h


# ------------------------------------------------------------------ szyfrowanie
def test_szyfrowanie_i_odszyfrowanie():
    box = SecretBox(KLUCZ)
    assert box.decrypt(box.encrypt("klucz-api-gieldy")) == "klucz-api-gieldy"


def test_ten_sam_tekst_daje_rozny_szyfrogram():
    """Losowy nonce per zapis. Bez tego widac, ktore rekordy maja te sama wartosc."""
    box = SecretBox(KLUCZ)
    assert box.encrypt("takie-samo") != box.encrypt("takie-samo")


def test_obcy_klucz_nie_odszyfruje():
    a = SecretBox(KLUCZ)
    b = SecretBox(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(Exception):
        b.decrypt(a.encrypt("sekret"))


def test_manipulacja_szyfrogramem_jest_wykrywana():
    """AES-GCM jest uwierzytelniony - podmiana bajtu ma zostac wykryta,
    a nie oddac przekrecony tekst."""
    box = SecretBox(KLUCZ)
    blob = bytearray(box.encrypt("sekret"))
    blob[-1] ^= 0x01
    with pytest.raises(Exception):
        box.decrypt(bytes(blob))


def test_brak_klucza_konczy_sie_bledem_a_nie_cichym_dzialaniem():
    stary = os.environ.pop("SECRETS_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="SECRETS_KEY"):
            SecretBox()
    finally:
        if stary is not None:
            os.environ["SECRETS_KEY"] = stary


def test_klucz_o_zlej_dlugosci_jest_odrzucany():
    with pytest.raises(RuntimeError, match="32 bajtami"):
        SecretBox(base64.b64encode(os.urandom(16)).decode())


# ------------------------------------------------------------------ porownanie
def test_constant_time_eq():
    assert constant_time_eq("abc", "abc")
    assert not constant_time_eq("abc", "abd")
    assert not constant_time_eq("abc", "abcd")


# ------------------------------------------------------------------ rotacja klucza
KLUCZ_STARY = base64.b64encode(os.urandom(32)).decode()
KLUCZ_NOWY = base64.b64encode(os.urandom(32)).decode()


def test_nowy_klucz_odczytuje_wartosci_zapisane_starym():
    """Sedno rotacji: po zmianie klucza stare wartosci nadal sie odczytuja."""
    stary_box = SecretBox(KLUCZ_STARY)
    blob = stary_box.encrypt("klucz-api-gieldy")
    nowy_box = SecretBox(KLUCZ_NOWY, old_key_b64=KLUCZ_STARY)
    assert nowy_box.decrypt(blob) == "klucz-api-gieldy"


def test_zapis_idzie_zawsze_biezacym_kluczem():
    nowy_box = SecretBox(KLUCZ_NOWY, old_key_b64=KLUCZ_STARY)
    blob = nowy_box.encrypt("sekret")
    assert nowy_box.zaszyfrowany_biezacym(blob)
    # Sam stary klucz nie moze tego odczytac.
    with pytest.raises(Exception):
        SecretBox(KLUCZ_STARY).decrypt(blob)


def test_rozpoznaje_ktore_wartosci_wymagaja_przepisania():
    """Bez tego raport z rotacji klamalby o tym, co zostalo zrobione."""
    nowy_box = SecretBox(KLUCZ_NOWY, old_key_b64=KLUCZ_STARY)
    stare = SecretBox(KLUCZ_STARY).encrypt("x")
    nowe = nowy_box.encrypt("x")
    assert not nowy_box.zaszyfrowany_biezacym(stare)
    assert nowy_box.zaszyfrowany_biezacym(nowe)


def test_bez_starego_klucza_stare_wartosci_sa_nieodczytywalne():
    """Kanarek do procedury rotacji.

    Pokazuje, dlaczego kroku z przepisaniem nie wolno pominac: po usunieciu
    SECRETS_KEY_OLD wartosc zostaje bezpowrotnie nieczytelna.
    """
    blob = SecretBox(KLUCZ_STARY).encrypt("klucz-gieldy")
    bez_starego = SecretBox(KLUCZ_NOWY)
    with pytest.raises(Exception):
        bez_starego.decrypt(blob)
