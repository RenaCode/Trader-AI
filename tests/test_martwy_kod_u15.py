"""Martwy kod usunięty (audyt Fable U-15, vulture na engine+ingest+exchanges).

Każdy z tych symboli był zdefiniowany i nieużywany nigdzie w repozytorium
(kod, testy, chart). `ingest/instruments.py` był starszym duplikatem
`ingest/instrumenty.py` - tamten ma CronJob `trader-instrumenty`, ten nie miał
żadnego wywołania, a wyglądał na równie aktualny.
"""
from __future__ import annotations

from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]


def test_duplikat_odswiezania_instrumentow_usuniety():
    assert not (KORZEN / "services/ingest/instruments.py").exists()
    assert (KORZEN / "services/ingest/instrumenty.py").exists()


def test_martwe_symbole_usuniete():
    from services.engine import bramki, waluta
    from services.exchanges import base
    from services.ingest import llm

    assert not hasattr(base, "ExchangeAdapter")
    assert not hasattr(bramki.Bramki, "skalibrowane") if hasattr(bramki, "Bramki") else True
    assert not any(hasattr(c, "skalibrowane") for c in vars(bramki).values()
                   if isinstance(c, type))
    assert not any(hasattr(c, "_dla_triazu_zapasowy") for c in vars(llm).values()
                   if isinstance(c, type))
    assert "kurs_z" not in waluta.PunktOdniesienia.__dataclass_fields__ \
        if hasattr(waluta, "PunktOdniesienia") else True
    assert not any("kurs_z" in getattr(c, "__dataclass_fields__", {})
                   for c in vars(waluta).values() if isinstance(c, type))


def test_kody_wyjscia_instrumentow_sa_UZYWANE_a_nie_martwe():
    """`KOD_BRAK_REGUL`/`KOD_WYCOFANIE` nazywały kody, a `main` zwracał
    literały 3 i 4. Zamiast kasować nazwy - `main` ich używa."""
    zrodlo = (KORZEN / "services/ingest/instrumenty.py").read_text(encoding="utf-8")
    assert "return KOD_BRAK_REGUL" in zrodlo and "return KOD_WYCOFANIE" in zrodlo
    assert "return 3\n" not in zrodlo and "return 4\n" not in zrodlo
