"""Czy Alpaca odpowiada NASZYM kluczem — cztery odczyty, zero zapisów.

Uruchamiane raz po wpisaniu klucza w interfejsie (Ustawienia -> Giełdy ->
Alpaca Markets, tryb demo) — z poda `trader-api`, bo tam jest `SECRETS_KEY`
i baza:

    kubectl exec -n default deploy/trader-api -- python services/exchanges/sprawdz_alpaca.py

Albo z kluczem ze środowiska (`ALPACA_KEY_ID`, `ALPACA_SECRET`) — bez bazy.

Każdy odczyt jest osobno nazwany, bo każdy pada z innego powodu: konto
(zły klucz / zły host), zegar (klucz działa, ale to nie jest to środowisko),
świece (Alpaca Data ma OSOBNE uprawnienia i feed), pozycje (uzgodnienie).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.exchanges.alpaca import AlpacaAdapter  # noqa: E402
from services.exchanges.base import ExchangeError  # noqa: E402


def _z_bazy(mode: str) -> tuple[str, str, str | None]:
    import psycopg
    from psycopg.rows import dict_row

    from services.api.security import SecretBox

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("brak ALPACA_KEY_ID/ALPACA_SECRET w środowisku i brak DATABASE_URL")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT c.key_enc, c.secret_enc, v.demo_host, v.live_host"
            "  FROM app.api_credential c JOIN ref.venue v ON v.id = c.venue_id"
            " WHERE v.code = 'alpaca' AND c.mode::text = %s AND c.active"
            " ORDER BY c.created_at DESC, c.id DESC LIMIT 1", (mode,)).fetchone()
    if not row:
        sys.exit(f"brak aktywnego klucza Alpaca dla trybu {mode} w app.api_credential")
    box = SecretBox()
    return (box.decrypt(bytes(row["key_enc"])), box.decrypt(bytes(row["secret_enc"])),
            row["demo_host"] if mode == "demo" else row["live_host"])


async def main() -> int:
    mode = os.environ.get("ALPACA_MODE", "demo")
    k, s = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET")
    host = None
    if not (k and s):
        k, s, host = _z_bazy(mode)
    a = AlpacaAdapter(k, s, mode=mode, host=host)
    kod = 0
    try:
        for nazwa, wywolanie in (
            ("konto", a.check_connection()),
            ("zegar sesji", a.zegar()),
            ("świece AAPL 1h (ostatnie 3 dni)",
             a.fetch_candles("AAPL", "1h", limit=5,
                             start=datetime.now(timezone.utc) - timedelta(days=3))),
            ("pozycje", a.pozycje()),
        ):
            try:
                wynik = await wywolanie
            except ExchangeError as e:
                print(f"  [BŁĄD] {nazwa}: {e}")
                kod = 1
                continue
            if isinstance(wynik, list):
                print(f"  [ok  ] {nazwa}: {len(wynik)} pozycji"
                      + (f", ostatnia {wynik[-1]}" if wynik else ""))
            else:
                print(f"  [ok  ] {nazwa}: {wynik}")
    finally:
        await a.close()
    return kod


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
