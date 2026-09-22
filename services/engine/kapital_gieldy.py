"""Krzywa kapitalu silnika Z KSIAG GIELDY - dla silnika, ktory nie liczy jej sam.

DLACZEGO TO ISTNIEJE

`exec.equity_point` dla krypto pisze petla silnika z wlasnych wypelnien
i wyceny (`loop.py`). Silnik equity jeszcze nie decyduje, wiec nie pisze
niczego - a wykres "Kapital laczny obu platform" mial przez to jedna linie.
Konto paper Alpaki jest natomiast PRAWDZIWYM rachunkiem z prawdziwa wycena:
equity, gotowka i wartosc pozycji sa tam liczone przez gielde, nie przez nas.
Ten proces przepisuje je do tej samej tabeli, ta sama miara co krypto
(jednostka uczestnictwa z migracji 0031), wiec obie linie znacza to samo.

CZEGO NIE ROBI: nie decyduje, nie sklada zlecen, nie dotyka pozycji. Odczyt
i jeden INSERT na cykl, `ON CONFLICT DO NOTHING` na siatce minutowej.

JEDNOSTKI. Bez przeplywow w `exec.cash_flow` przyjmujemy start 100 000 jako
wplate (tak samo jak `loop.seed_equity` dla demo) - konto paper Alpaki startuje
dokladnie z tej kwoty. `realized_cum` = equity - wplaty - unrealized, czyli to,
co wynika z ksiag gieldy, a nie z naszego dziennika; `unrealized` = suma
`unrealized_pl` z pozycji.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from services.engine.loop import START_EQUITY, przeplywy  # noqa: E402
from services.exchanges.fabryka import adapter_danych  # noqa: E402
from services.ingest import puls  # noqa: E402

POD_RZAD_MAX = 5
PULS = "kapital"


def zapisz_punkt(cur, engine: str, mode: str, stan: dict, unrealized: Decimal,
                 as_of: datetime | None = None) -> dict:
    """Jeden punkt krzywej z odczytu konta. CZYSTA arytmetyka + INSERT."""
    as_of = (as_of or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    wplaty, units = przeplywy(cur, engine, mode)
    if wplaty == 0:
        if mode != "demo":
            raise RuntimeError(f"tryb {mode} bez przeplywu w exec.cash_flow - nie zgaduje")
        wplaty, units = START_EQUITY, START_EQUITY
    equity = Decimal(stan["equity"])
    realized = equity - wplaty - unrealized
    nav = (equity / units) if units > 0 else Decimal(1)
    cur.execute(
        "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash, unrealized,"
        " realized_cum, exposure, nav, units, wplaty_netto)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (engine, mode, event_time) DO NOTHING",
        (engine, mode, as_of, equity, Decimal(stan["cash"]), unrealized, realized,
         Decimal(stan["ekspozycja"]), nav, units, wplaty))
    return {"equity": equity, "nav": nav, "realized": realized, "unrealized": unrealized,
            "ekspozycja": Decimal(stan["ekspozycja"]),
            "zapisany": getattr(cur, "rowcount", 1) > 0}


async def cykl(url: str, engine: str, mode: str) -> dict:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        adapter = adapter_danych(conn, engine, mode)
        try:
            stan = await adapter.stan_konta()
            unrealized = Decimal(0)
            if hasattr(adapter, "pozycje_szczegoly"):
                unrealized = sum((p["unrealized_pl"] for p in await adapter.pozycje_szczegoly()),
                                 Decimal(0))
        finally:
            await adapter.close()
        cur = conn.cursor(row_factory=dict_row)
        wynik = zapisz_punkt(cur, engine, mode, stan, unrealized)
        conn.commit()
        return wynik


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="equity")
    ap.add_argument("--mode", default="demo", choices=("demo", "live"))
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")
    nazwa = f"{PULS}-{args.engine}"
    pod_rzad = 0
    while True:
        t0 = datetime.now(timezone.utc)
        try:
            w = await cykl(url, args.engine, args.mode)
            print(f"[{t0:%H:%M:%S}] {args.engine}/{args.mode}: kapitał {w['equity']:.2f}, "
                  f"nav {w['nav']:.4f}, ekspozycja {w['ekspozycja']:.2f}"
                  f"{'' if w['zapisany'] else ' (punkt tej minuty już był)'}")
            with psycopg.connect(url, row_factory=dict_row) as conn:
                puls.bij(conn, nazwa, args.interval, {"equity": float(w["equity"])})
            pod_rzad = 0
        except Exception as e:                                # noqa: BLE001
            pod_rzad += 1
            print(f"[{t0:%H:%M:%S}] cykl nieudany ({pod_rzad}/{POD_RZAD_MAX}): "
                  f"{type(e).__name__}: {e}")
            try:
                with psycopg.connect(url, row_factory=dict_row) as conn:
                    puls.zglos_awarie(conn, nazwa, args.interval, pod_rzad,
                                      f"{type(e).__name__}: {e}")
            except Exception:                                 # noqa: BLE001
                pass
            if pod_rzad >= POD_RZAD_MAX:
                return 1
        if args.once:
            return 0 if pod_rzad == 0 else 1
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
