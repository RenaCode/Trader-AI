"""Etykieta „reżim dominujący w oknie" nie może zależeć od PYTHONHASHSEED.

Do 2026-10-02 `train()` liczył ją jako `max(set(rezimy), key=rezimy.count)`.
Przy remisie wygrywał pierwszy element w kolejności iteracji ZBIORU NAPISÓW,
a ta zależy od ziarna haszowania, losowanego przy każdym starcie interpretera
(CronJob trenera go nie ustawia). Ten sam przebieg na tych samych danych
opisywał okno raz jako `trend_up`, raz jako `range`.

Test uruchamia interpreter KILKA RAZY z różnym ziarnem - to jedyny sposób,
żeby zobaczyć tę wadę: wewnątrz jednego procesu ziarno jest stałe i każda
asercja przechodzi.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from services.engine.signals import REGIMES
from services.engine.train import rezim_dominujacy

KORZEN = Path(__file__).resolve().parents[1]

# Remis WSZYSTKICH reżimów - najgorszy przypadek dla kolejności zbioru.
REMIS = list(REGIMES) * 3

SKRYPT = (
    "from services.engine.signals import REGIMES\n"
    "from services.engine.train import rezim_dominujacy\n"
    "print(rezim_dominujacy(list(REGIMES) * 3))\n"
)

STARY = (
    "from services.engine.signals import REGIMES\n"
    "r = list(REGIMES) * 3\n"
    "print(max(set(r), key=r.count))\n"
)


def _uruchom(kod: str, ziarno: int) -> str:
    env = os.environ | {"PYTHONHASHSEED": str(ziarno)}
    wynik = subprocess.run([sys.executable, "-B", "-c", kod], cwd=KORZEN, env=env,
                           capture_output=True, text=True, check=True)
    return wynik.stdout.strip()


ZIARNA = (0, 1, 2, 3, 7, 42, 1234, 99999)


def test_ta_sama_etykieta_przy_KAZDYM_ziarnie_haszowania():
    etykiety = {_uruchom(SKRYPT, z) for z in ZIARNA}
    assert etykiety == {REGIMES[0]}, (
        f"etykieta okna zależy od PYTHONHASHSEED: {sorted(etykiety)}")


def test_kanarek_stary_wzor_NAPRAWDE_zalezal_od_ziarna():
    """Bez tego test wyżej mógłby przechodzić dlatego, że remis nie zachodzi
    albo że interpreter przestał losować ziarno - a nie dzięki poprawce."""
    assert len({_uruchom(STARY, z) for z in ZIARNA}) > 1


def test_wiekszosc_wygrywa_remis_rozstrzyga_kolejnosc_REGIMES():
    assert rezim_dominujacy(["range", "trend_up", "range"]) == "range"
    assert rezim_dominujacy(["range", "trend_up"]) == "trend_up"
    assert rezim_dominujacy(["high_vol", "range", "range", "high_vol"]) == "range"
    assert rezim_dominujacy([]) is None
    # Spoza REGIMES - na końcu, ale nadal deterministycznie.
    assert rezim_dominujacy(["zzz", "aaa"]) == "aaa"
    assert rezim_dominujacy(["zzz", "high_vol"]) == "high_vol"


def test_trener_uzywa_funkcji_a_nie_wzoru_na_zbiorze():
    zrodlo = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    assert "dominant = rezim_dominujacy(regimes)" in zrodlo
    # Wzorzec z `key=` - w docstringu funkcji stoi opis starego wzoru.
    assert "max(set(regimes)" not in zrodlo
