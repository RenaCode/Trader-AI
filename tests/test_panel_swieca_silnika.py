"""Panel "Stopien nauki" szuka stanu na swiecy, ktora silnik handluje.

Do 2026-10-06 `learning.py` i `overview.py` filtrowaly `learn.learning_state`
po `DEFAULT_BAR` (4h) dla KAZDEGO silnika, a equity trenuje sie wylacznie
na 1h - panel equity mowil "Brak stanu dla tego silnika" nawet po treningu.
"""
import re
from pathlib import Path

import yaml

from services.engine.horizons import bar_silnika

KORZEN = Path(__file__).resolve().parents[1]
VALUES = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8"))


def _bar_petli(worker: str) -> str:
    """`--bar` z wywolania petli (komenda to `sh -c "... loop.py ... --bar X ..."`)."""
    m = re.findall(r"loop\.py[^&]*?--bar (\S+)", " ".join(VALUES["workers"][worker]["command"]))
    assert m, f"{worker}: nie znalazlem `loop.py --bar`"
    return m[-1]


def test_swieca_panelu_to_swieca_petli_decyzyjnej():
    assert bar_silnika("crypto") == _bar_petli("engine")
    assert bar_silnika("equity") == _bar_petli("engine-equity")


def test_equity_trenuje_te_swiece_ktora_pokazuje_panel():
    assert bar_silnika("equity") in VALUES["trainerCron"]["equity"]["horyzonty"]


def test_api_nie_filtruje_juz_po_stalej():
    for f in ("services/api/routers/learning.py", "services/api/routers/overview.py"):
        assert "DEFAULT_BAR" not in (KORZEN / f).read_text(encoding="utf-8"), f
