"""Czujka świec odłożonych musi szukać decyzji w OKNIE TEJ ŚWIECY, nie poprzedniej.

Do 02.10.2026 `sprawdz_odlozone_decyzje` pytało o decyzję w [B, B + świeca).
Pętla rozstrzyga świecę B dopiero po jej zamknięciu, w [B + świeca,
B + 2·świeca) — `loop.okno_decyzji`. W oknie [B, B + świeca) leżą decyzje dla
świecy B−1, więc każda z nich „gasiła" brak decyzji dla B: czujka milczała,
dopóki nie zabrakło dwóch świec z rzędu.

Atrapa w test_odlozone_bez_decyzji.py oddaje gotowe braki i nie wykonuje SQL,
więc tamten zestaw nie mógł tego zauważyć. Ten test podstawia parametry do
zapytania i rozstrzyga okno tak, jak zrobiłaby to baza.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from services.engine.dozor import sprawdz_odlozone_decyzje
from services.engine.horizons import H4H
from services.engine.loop import okno_decyzji

TERAZ = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)
MINUT = 240


class _Stend:
    def __init__(self):
        self.zapytanie: tuple[str, tuple] | None = None

    async def fetch_all(self, sql, params=None):
        plaski = " ".join(sql.split())
        if "SELECT DISTINCT bar_start FROM journal.pominiecie" in plaski:
            return [{"bar_start": TERAZ - timedelta(minutes=MINUT * i)}
                    for i in range(6, 0, -1)]
        if "journal.pominiecie" in plaski and "NOT EXISTS" in plaski:
            self.zapytanie = (plaski, tuple(params))
        return []

    async def notify(self, *a, **k):
        return 1


def _okno_w_zapytaniu() -> tuple[int, int]:
    """(od, do) w minutach względem `p.bar_start`, jak policzyłaby je baza."""
    stend = _Stend()
    asyncio.run(sprawdz_odlozone_decyzje(stend.fetch_all, stend.notify))
    assert stend.zapytanie, "czujka nie zadała zapytania o świece bez decyzji"
    sql, params = stend.zapytanie
    kawalki = sql.split("%s")
    assert len(kawalki) - 1 == len(params), "liczba parametrów nie pasuje do SQL"
    podstawione = "".join(k + (repr(params[i]) if i < len(params) else "")
                          for i, k in enumerate(kawalki))
    wnetrze = podstawione[podstawione.index("NOT EXISTS"):]

    def granica(op: str) -> int:
        m = re.search(rf"d\.decided_at {op} p\.bar_start"
                      rf"(?: \+ make_interval\(mins => (\d+)\))?", wnetrze)
        assert m, f"brak granicy `{op}` w oknie decyzji"
        return int(m.group(1) or 0)

    return granica(">="), granica("<")


def test_okno_czujki_to_okno_decyzji_petli():
    od, do = _okno_w_zapytaniu()
    b = TERAZ - timedelta(hours=12)
    assert b + timedelta(minutes=od) == okno_decyzji(b, H4H), (
        f"czujka szuka decyzji od B+{od} min, a pętla rozstrzyga świecę B "
        f"od {okno_decyzji(b, H4H) - b} po jej otwarciu")
    assert do - od == MINUT


def test_decyzja_dla_POPRZEDNIEJ_swiecy_nie_gasi_braku():
    od, do = _okno_w_zapytaniu()
    # Decyzja 30 min po otwarciu B to rozstrzygnięcie świecy B−1.
    assert not (od <= 30 < do), (
        "decyzja dla świecy poprzedniej liczy się jako decyzja dla B — "
        "czujka milczy przy pojedynczej nierozstrzygniętej świecy")
    # Decyzja 30 min po ZAMKNIĘCIU B jest tą właściwą.
    assert od <= MINUT + 30 < do
