"""Prymitywy bezpieczenstwa: hasla, sesje, szyfrowanie sekretow, TOTP.

Wydzielone do osobnego modulu, zeby kryptografia byla w jednym miejscu i dala sie
przejrzec bez czytania logiki aplikacji.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Parametry Argon2id. Wartosci powyzej domyslnych z argon2-cffi - to jest logowanie
# do systemu obracajacego pieniedzmi, kilkaset milisekund na probe jest akceptowalne,
# a mocno podnosi koszt ataku offline gdyby baza wyciekla.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)

SESSION_TTL = timedelta(hours=12)
SESSION_COOKIE = "trader_session"


# ----------------------------------------------------------------- hasla
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> tuple[bool, str | None]:
    """Zwraca (czy_poprawne, nowy_hash_do_zapisu_albo_None).

    Drugi element jest niepusty, gdy hash byl liczony starszymi parametrami -
    wtedy przy okazji udanego logowania podnosimy go do biezacych.
    """
    try:
        _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False, None
    except Exception:
        return False, None
    if _hasher.check_needs_rehash(stored_hash):
        return True, _hasher.hash(password)
    return True, None


# ----------------------------------------------------------------- sesje
def new_session_token() -> tuple[str, bytes]:
    """Zwraca (token_dla_ciasteczka, hash_do_bazy).

    W bazie ladauje wylacznie SHA-256 tokenu. Wyciek tabeli sesji nie pozwala
    wiec podszyc sie pod uzytkownika - tak samo jak przy hasłach.
    """
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).digest()


def session_token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + SESSION_TTL


# ----------------------------------------------------------------- kody zapasowe
def new_backup_codes(count: int = 10) -> list[tuple[str, str]]:
    """Zwraca liste (kod_do_pokazania_raz, hash_do_bazy)."""
    out = []
    for _ in range(count):
        raw = "-".join(secrets.token_hex(2) for _ in range(3))  # xxxx-xxxx-xxxx
        out.append((raw, hashlib.sha256(raw.encode()).hexdigest()))
    return out


def backup_code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()


# ----------------------------------------------------------------- szyfrowanie sekretow
class SecretBox:
    """AES-GCM na kluczu z env (SECRETS_KEY, 32 bajty base64).

    Uzywane do kluczy API gield, sekretow TOTP i klucza Anthropica. Nonce jest
    losowy per zapis i doklejany przed szyfrogramem, wiec ten sam plaintext daje
    rozne bajty.

    ROTACJA KLUCZA

    Klucz bez mozliwosci wymiany jest zobowiazaniem na zawsze: gdy wycieknie albo
    trzeba go zmienic z innego powodu, kazda zaszyfrowana wartosc staje sie
    nieodczytywalna - i to bez ostrzezenia, dopiero przy pierwszym odczycie.

    Dlatego `SECRETS_KEY_OLD` (opcjonalny) trzyma poprzedni klucz. Odszyfrowanie
    probuje najpierw biezacego, potem starego; zapis idzie ZAWSZE biezacym.
    Procedura wymiany jest wiec taka:

        1. SECRETS_KEY_OLD = dotychczasowy klucz, SECRETS_KEY = nowy
        2. wdrozenie - od tej chwili nowe zapisy ida nowym kluczem,
           a stare wartosci nadal sie odczytuja
        3. python services/rotate_secrets.py - przepisuje wszystko nowym kluczem
        4. usuniecie SECRETS_KEY_OLD

    Format szyfrogramu sie NIE zmienia, wiec nie ma migracji danych - jedyna
    roznica jest w tym, ze przy odszyfrowaniu probujemy dwoch kluczy.
    """

    def __init__(self, key_b64: str | None = None, old_key_b64: str | None = None):
        raw = key_b64 or os.environ.get("SECRETS_KEY", "")
        if not raw:
            raise RuntimeError(
                "SECRETS_KEY nie jest ustawiony - bez niego nie da sie bezpiecznie "
                "przechowac kluczy API gield ani sekretow TOTP."
            )
        self._aes = AESGCM(self._klucz(raw))

        stary = old_key_b64 if old_key_b64 is not None else os.environ.get("SECRETS_KEY_OLD", "")
        self._aes_stary = AESGCM(self._klucz(stary)) if stary else None

    @staticmethod
    def _klucz(raw: str) -> bytes:
        key = base64.b64decode(raw)
        if len(key) != 32:
            raise RuntimeError("SECRETS_KEY musi byc 32 bajtami zakodowanymi w base64")
        return key

    def encrypt(self, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, plaintext.encode(), None)

    def decrypt(self, blob: bytes) -> str:
        try:
            return self._aes.decrypt(blob[:12], blob[12:], None).decode()
        except Exception:
            if self._aes_stary is None:
                raise
            # Wartosc zapisana jeszcze poprzednim kluczem. Odczyt sie udaje,
            # ale nastepny zapis pojdzie juz kluczem biezacym.
            return self._aes_stary.decrypt(blob[:12], blob[12:], None).decode()

    def zaszyfrowany_biezacym(self, blob: bytes) -> bool:
        """Czy wartosc jest juz zaszyfrowana AKTUALNYM kluczem.

        Uzywane przez rotate_secrets.py, zeby nie przepisywac tego, co nie
        wymaga przepisania - i zeby raport z rotacji mowil prawde.
        """
        try:
            self._aes.decrypt(blob[:12], blob[12:], None)
            return True
        except Exception:
            return False


def constant_time_eq(a: str, b: str) -> bool:
    """Porownanie odporne na pomiar czasu.

    Uzywane do kodow TOTP. Zwykle `==` konczy sie na pierwszej roznicy, wiec
    czas odpowiedzi niesie informacje o tym, ile znakow bylo poprawnych.
    """
    return hmac.compare_digest(a, b)


# ----------------------------------------------------------------- TOTP
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, login: str, issuer: str = "Trader AI") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=login, issuer_name=issuer)


TOTP_STEP = 30
TOTP_WINDOW = 1        # tolerancja rozjazdu zegara: jeden krok w kazda strone


def verify_totp(secret: str, code: str, last_step: int | None = None) -> int | None:
    """Zwraca numer zaakceptowanego kroku czasowego albo None.

    OCHRONA PRZED POWTORZENIEM. Wczesniejsza wersja zwracala bool i nie
    interesowala sie tym, ktory krok pasowal. Kod byl przez to wazny przez cale
    okno tolerancji (~90 s) i dzialal w nim WIELOKROTNIE - przechwycony raz,
    dawal sie uzyc ponownie. RFC 6238 §5.2 wymaga wprost, zeby weryfikator
    odrzucil drugie uzycie tego samego kroku.

    Zwracany numer kroku wolajacy zapisuje w app.totp_secret.last_step
    i podaje przy nastepnym sprawdzeniu jako `last_step`. Kod z krokiem nie
    wiekszym niz zapisany jest odrzucany, choc sam w sobie jest poprawny.

    Porownanie idzie przez compare_digest, a nie przez `==`. Roznica czasu przy
    zwyklym porownaniu lancuchow jest tu mala, ale kod TOTP ma szesc cyfr
    i darmowa ochrona jest tanszym wyborem niz rozwazanie, czy akurat wystarczy.
    """
    code = code.strip().replace(" ", "").replace("-", "")
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    biezacy = int(time.time()) // TOTP_STEP
    for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        step = biezacy + offset
        if constant_time_eq(totp.at(step * TOTP_STEP), code):
            if last_step is not None and step <= last_step:
                return None          # kod juz zuzyty
            return step
    return None


