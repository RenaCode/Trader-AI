"""`loop.py --once` po nieudanym cyklu musi kończyć się kodem różnym od zera.

Do 02.10.2026 pojedynczy przebieg zawsze zwracał 0: wyjątek w cyklu był
wypisywany, licznik `pod_rzad` rósł do jedynki, a potem `if args.once:
return 0`. Ręczne sprawdzenie albo zadanie wołające pętlę z `--once` widziało
sukces cyklu, który się wywrócił.
"""
from __future__ import annotations

import asyncio
import sys

from services.engine import loop


def _uruchom(monkeypatch, polacz) -> int:
    monkeypatch.setattr(sys, "argv", ["loop.py", "--once", "--quiet"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(loop.psycopg, "connect", polacz)
    return asyncio.run(loop.main())


def test_nieudany_cykl_z_once_daje_kod_bledu(monkeypatch):
    def padnij(*a, **k):
        raise RuntimeError("baza niedostępna")

    assert _uruchom(monkeypatch, padnij) != 0, (
        "przebieg przerwany wyjątkiem zakończył `--once` kodem 0")


def test_udany_cykl_z_once_daje_zero(monkeypatch):
    """Kanarek: poprawka nie może zapalać kodu błędu na zdrowym cyklu."""
    class _Pol:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return None

    async def sesja(*a, **k):
        class S:
            otwarta = True

            def opis(self):
                return "otwarta"
        return S()

    async def cykl(*a, **k):
        return {"rozwazone": 0, "wejscia": 0, "wyjscia": 0, "wstrzymane": 0,
                "zablokowane": 0, "poza_swieca": 0}

    monkeypatch.setattr(loop, "stan_sesji", sesja)
    monkeypatch.setattr(loop, "run_once", cykl)
    assert _uruchom(monkeypatch, lambda *a, **k: _Pol()) == 0
