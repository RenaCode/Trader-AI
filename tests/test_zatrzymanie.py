"""Łagodne zatrzymanie silnika i dozorcy (P-T4, audyt 04.10 T-9).

Do 2026-10-05 SIGTERM kończył `loop.py` i `dozorca.py` natychmiast, w środku
cyklu. Testy pilnują trzech rzeczy: zdarzenie przerywa czekanie między
cyklami, prawdziwy SIGTERM kończy proces kodem 0 (nie 143), a cykl, który
już trwa, dobiega końca.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from services.engine.zatrzymanie import Zatrzymanie

KORZEN = Path(__file__).resolve().parents[1]


def test_czekaj_wraca_natychmiast_po_zatrzymaniu():
    async def przebieg():
        z = Zatrzymanie()
        asyncio.get_running_loop().call_later(0.05, z.ustaw)
        t0 = time.monotonic()
        koniec = await z.czekaj(5)
        return koniec, time.monotonic() - t0

    koniec, czas = asyncio.run(przebieg())
    assert koniec is True
    assert czas < 1, "czekanie nie zostało przerwane zatrzymaniem"


def test_czekaj_bez_zatrzymania_spi_i_zwraca_false():
    async def przebieg():
        return await Zatrzymanie().czekaj(0.01)

    assert asyncio.run(przebieg()) is False


SKRYPT = textwrap.dedent("""
    import asyncio, sys
    from services.engine.zatrzymanie import Zatrzymanie

    async def main():
        z = Zatrzymanie().zarejestruj()
        print("gotowy", flush=True)
        while True:
            if z.ustawione:
                return 0
            # "cykl": SIGTERM w trakcie NIE moze go przerwac
            await asyncio.sleep(0.3)
            print("cykl domkniety", flush=True)
            if await z.czekaj(30):
                return 0

    sys.exit(asyncio.run(main()))
""")


def test_SIGTERM_konczy_petle_kodem_0_po_domknieciu_cyklu():
    p = subprocess.Popen([sys.executable, "-c", SKRYPT], cwd=KORZEN,
                         stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "PYTHONPATH": str(KORZEN)})
    assert p.stdout.readline().strip() == "gotowy"
    p.send_signal(signal.SIGTERM)   # w środku pierwszego "cyklu"
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0, f"kod {p.returncode} - proces ucięty zamiast zakończony"
    assert "SIGTERM — kończę między cyklami" in out
    assert "cykl domkniety" in out, "SIGTERM przerwał cykl w toku"
