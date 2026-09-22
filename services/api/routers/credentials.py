"""Zarzadzanie kluczami API gield.

Zasady, ktore ten modul egzekwuje:
  - klucz i sekret sa szyfrowane AES-GCM przed zapisem; w bazie nie ma plaintextu
  - API NIGDY nie zwraca odszyfrowanej wartosci, tylko podpowiedz (4 ostatnie znaki)
  - klucz z prawem WYPLATY nie moze byc aktywny (wymuszone rowniez CHECK-iem w bazie)
  - test polaczenia uzywa endpointu informacyjnego, nie sklada zlecen
  - kazda operacja trafia do dziennika audytu
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import audit, current_user, require_csrf
from ..db import execute, fetch_all, fetch_one
from ..notifications import powiadom_o_bezpieczenstwie
from ..security import SecretBox

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


class CredentialBody(BaseModel):
    venue_code: str = Field(min_length=2, max_length=32)
    mode: str = Field(pattern="^(demo|live)$")
    label: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=8, max_length=512)
    api_secret: str = Field(min_length=8, max_length=512)
    passphrase: str | None = Field(default=None, max_length=512)


def _waznosc(created_at, dni: int | None) -> dict:
    """Termin waznosci klucza i ile dni zostalo. Pusty, gdy gielda go nie ogranicza."""
    if not dni or not created_at:
        return {}
    wygasa = created_at + timedelta(days=dni)
    zostalo = (wygasa - datetime.now(timezone.utc)).days
    return {"expires_at": wygasa.isoformat(), "days_left": zostalo}


def _hint(value: str) -> str:
    return "••••" + value[-4:] if len(value) >= 4 else "••••"


@router.get("")
async def list_credentials(user: dict = Depends(current_user)):
    rows = await fetch_all(
        """
        SELECT c.id, c.mode, c.label, c.key_hint, c.active, c.can_withdraw,
               c.permissions, c.created_at, c.last_used_at, c.last_error,
               v.code AS venue_code, v.name AS venue_name, v.engine,
               (v.meta->>'waznosc_klucza_dni')::int AS waznosc_dni
        FROM app.api_credential c
        JOIN ref.venue v ON v.id = c.venue_id
        WHERE c.user_id = %s
        ORDER BY v.engine, c.mode, c.label
        """,
        (user["id"],),
    )
    return [
        {
            "id": r["id"], "venue_code": r["venue_code"], "venue_name": r["venue_name"],
            "engine": r["engine"], "mode": r["mode"], "label": r["label"],
            "key_hint": r["key_hint"], "active": r["active"],
            "can_withdraw": r["can_withdraw"], "permissions": r["permissions"],
            # Klucz bez przypietego IP gasnie po trzech miesiacach, a wygasniecie
            # w polowie fazy demo nie objawia sie niczym widocznym: dane rynkowe
            # ida z hosta publicznego, wiec dashboard nadal wyglada zdrowo.
            # Patrz migracja 0019 - to zalozenie z regulaminu, nie odczyt z gieldy.
            **_waznosc(r["created_at"], r["waznosc_dni"]),
            "created_at": r["created_at"].isoformat(),
            "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            "last_error": r["last_error"],
        }
        for r in rows
    ]


@router.get("/venues")
async def venues(user: dict = Depends(current_user)):
    rows = await fetch_all(
        "SELECT code, name, engine, is_24_7, demo_host FROM ref.venue ORDER BY engine")
    return [dict(r) for r in rows]


@router.post("", dependencies=[Depends(require_csrf)])
async def add_credential(body: CredentialBody, request: Request,
                         user: dict = Depends(current_user)):
    venue = await fetch_one("SELECT id, name FROM ref.venue WHERE code = %s", (body.venue_code,))
    if not venue:
        raise HTTPException(404, f"Nieznana giełda: {body.venue_code}")

    # Tryb live wymaga swiadomej decyzji, a nie przypadkowego wyboru z listy.
    # Blokada jest tutaj celowa i zostaje do konca fazy demo.
    if body.mode == "live":
        raise HTTPException(
            400,
            "Tryb live jest zablokowany do zakończenia fazy demonstracyjnej. "
            "Odblokowanie wymaga spełnienia kryteriów opisanych w docs/00-research.md §9.2.",
        )

    box = SecretBox()
    try:
        await execute(
            """
            INSERT INTO app.api_credential
                (user_id, venue_id, mode, label, key_enc, secret_enc, passphrase_enc, key_hint)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (user["id"], venue["id"], body.mode, body.label.strip(),
             box.encrypt(body.api_key.strip()), box.encrypt(body.api_secret.strip()),
             box.encrypt(body.passphrase) if body.passphrase else None,
             _hint(body.api_key.strip())),
        )
    except Exception as e:                                     # noqa: BLE001
        if "api_credential_venue_id_mode_label_key" in str(e):
            raise HTTPException(409, "Klucz o tej etykiecie już istnieje dla tej giełdy i trybu")
        raise

    await audit("credential.create", user_id=user["id"], entity="api_credential",
                entity_id=body.label, request=request)
    await powiadom_o_bezpieczenstwie(
        user["id"], "credential.changed", "Dodano klucz API giełdy",
        f"Giełda: {venue['name']}, etykieta: {body.label.strip()}, "
        f"tryb: {body.mode}.\n\nJeśli to nie Ty — usuń klucz i zmień hasło.",
        severity="warning")
    return {"ok": True, "key_hint": _hint(body.api_key.strip())}


@router.delete("/{credential_id}", dependencies=[Depends(require_csrf)])
async def delete_credential(credential_id: int, request: Request,
                            user: dict = Depends(current_user)):
    row = await fetch_one(
        "SELECT label FROM app.api_credential WHERE id = %s AND user_id = %s",
        (credential_id, user["id"]),
    )
    if not row:
        raise HTTPException(404, "Nie ma takiego klucza")
    await execute("DELETE FROM app.api_credential WHERE id = %s", (credential_id,))
    await audit("credential.delete", user_id=user["id"], entity="api_credential",
                entity_id=row["label"], request=request)
    return {"ok": True}


@router.post("/{credential_id}/test", dependencies=[Depends(require_csrf)])
async def test_credential(credential_id: int, request: Request,
                          user: dict = Depends(current_user)):
    row = await fetch_one(
        """
        SELECT c.id, c.mode, c.key_enc, c.secret_enc, v.code AS venue_code,
               v.demo_host, v.live_host
        FROM app.api_credential c JOIN ref.venue v ON v.id = c.venue_id
        WHERE c.id = %s AND c.user_id = %s
        """,
        (credential_id, user["id"]),
    )
    if not row:
        raise HTTPException(404, "Nie ma takiego klucza")

    box = SecretBox()
    api_key = box.decrypt(bytes(row["key_enc"]))
    api_secret = box.decrypt(bytes(row["secret_enc"]))

    from services.exchanges.fabryka import zbuduj_adapter_gieldy
    try:
        adapter = zbuduj_adapter_gieldy(row["venue_code"], api_key, api_secret,
                                        mode=row["mode"], demo_host=row["demo_host"],
                                        live_host=row["live_host"])
    except KeyError:
        raise HTTPException(501, f"Adapter dla {row['venue_code']} nie jest jeszcze gotowy")

    try:
        check = await adapter.check_connection()
    finally:
        await adapter.close()

    await execute(
        "UPDATE app.api_credential SET last_used_at = now(), last_error = %s,"
        " can_withdraw = COALESCE(%s, can_withdraw),"
        " permissions = %s, active = (%s AND NOT COALESCE(%s, false))"
        " WHERE id = %s",
        (None if check.ok else check.message, check.can_withdraw,
         __import__("json").dumps({"lista": check.permissions}),
         check.ok, check.can_withdraw, credential_id),
    )
    await audit("credential.test", user_id=user["id"], entity="api_credential",
                entity_id=str(credential_id),
                after={"ok": check.ok, "can_withdraw": check.can_withdraw}, request=request)

    return {
        "ok": check.ok,
        "message": check.message,
        "account_type": check.account_type,
        "permissions": check.permissions,
        "can_withdraw": check.can_withdraw,
        "latency_ms": check.latency_ms,
        "balances": [
            {"currency": b.currency, "total": str(b.total), "available": str(b.available)}
            for b in check.balances
        ],
    }
