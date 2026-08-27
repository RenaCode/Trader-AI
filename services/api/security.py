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

    Uzywane do kluczy API gield i sekretow TOTP. Nonce jest losowy per zapis
    i doklejany przed szyfrogramem, wiec ten sam plaintext daje rozne bytes.
    """

    def __init__(self, key_b64: str | None = None):
        raw = key_b64 or os.environ.get("SECRETS_KEY", "")
        if not raw:
            raise RuntimeError(
                "SECRETS_KEY nie jest ustawiony - bez niego nie da sie bezpiecznie "
                "przechowac kluczy API gield ani sekretow TOTP."
            )
        key = base64.b64decode(raw)
        if len(key) != 32:
            raise RuntimeError("SECRETS_KEY musi byc 32 bajtami zakodowanymi w base64")
        self._aes = AESGCM(key)

    def encrypt(self, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, plaintext.encode(), None)

    def decrypt(self, blob: bytes) -> str:
        return self._aes.decrypt(blob[:12], blob[12:], None).decode()


# ----------------------------------------------------------------- TOTP
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, login: str, issuer: str = "Trader AI") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=login, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    # valid_window=1 toleruje rozjazd zegara o jeden krok (30 s) w kazda strone.
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
