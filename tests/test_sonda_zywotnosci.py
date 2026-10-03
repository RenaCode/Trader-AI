"""Sonda zywotnosci workerow: plik z terminem, odswiezany KAZDYM obrotem petli.

Do 2026-10-03 workery nie mialy `livenessProbe` - swiadomie, z trzema powodami
w naglowku templates/workers.yaml. Sonda jest zbudowana tak, zeby zaden z nich
nie obowiazywal, i te testy pilnuja wlasnie tych warunkow:

  1. termin siedzi w PLIKU (liczy go proces z `--interval`), nie w charcie,
  2. plik odswieza takze cykl NIEUDANY - lezaca baza nie moze zrestartowac
     calego potoku naraz; sonda lapie proces, ktory STOI, nie ten, ktory PADA,
  3. brak pliku przepuszcza - chart wyprzedza obraz, a modul bez `puls.zyje`
     nie moze byc przez sonde zabijany,
  4. termin jest dluzszy niz prog czujki pulsu - czlowiek dowiaduje sie
     pierwszy, restart przychodzi potem.

Polecenie sondy jest URUCHAMIANE prawdziwym `sh` na prawdziwym pliku.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import yaml

from services.ingest import puls

KORZEN = Path(__file__).resolve().parents[1]
WORKERS = (KORZEN / "charts/trader/templates/workers.yaml").read_text(encoding="utf-8")
VALUES = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8"))


def _polecenie_sondy() -> list[str]:
    i = WORKERS.index("          livenessProbe:")
    blok = yaml.safe_load(WORKERS[i:WORKERS.index("          env:", i)])
    return blok["livenessProbe"]["exec"]["command"]


def _sonda(plik: Path) -> int:
    return subprocess.run(_polecenie_sondy(), env={"PLIK_ZYWOTNOSCI": str(plik),
                                                   "PATH": "/usr/bin:/bin"}).returncode


def test_sonda_przepuszcza_swiezy_termin_i_odrzuca_miniony(tmp_path):
    plik = tmp_path / "zywy"
    puls.zyje(60, plik=str(plik))
    assert _sonda(plik) == 0
    plik.write_text(str(int(time.time()) - 1))
    assert _sonda(plik) != 0, "termin minal, a sonda przepuscila"


def test_brak_pliku_przepuszcza(tmp_path):
    assert _sonda(tmp_path / "nie-ma") == 0, (
        "brak pliku zabija - chart wyprzedzajacy obraz zrestartowalby wszystkie workery")


def test_smieci_w_pliku_nie_przepuszczaja(tmp_path):
    plik = tmp_path / "zywy"
    plik.write_text("")
    assert _sonda(plik) != 0


def test_termin_dluzszy_niz_prog_czujki_pulsu():
    from services.engine import dozor
    from services.engine.dozorca import PODLOGA_ZYWOTNOSCI_S
    assert puls.termin_zywotnosci(60) > dozor.MIN_PROG_S
    assert PODLOGA_ZYWOTNOSCI_S >= dozor.MIN_PROG_S
    assert puls.termin_zywotnosci(1200) >= 4 * 1200, "dlugi interwal - termin z nim"


def test_sonda_na_kazdym_workerze_i_bez_kopii_interwalu():
    assert WORKERS.count("livenessProbe:") == 1, "sonda w range - jedna definicja"
    i = WORKERS.index("          livenessProbe:")
    blok = WORKERS[i:WORKERS.index("          env:", i)]
    for interwal in ("300", "900", "1200"):
        assert interwal not in blok, "interwal workera skopiowany do szablonu"


MODULY = {
    "market": "services/ingest/market_worker.py",
    "market-equity": "services/ingest/market_worker.py",
    "history": "services/ingest/history_worker.py",
    "history-equity": "services/ingest/history_worker.py",
    "features": "services/ingest/features.py",
    "features-equity": "services/ingest/features.py",
    "news": "services/ingest/news_worker.py",
    "dozorca": "services/engine/dozorca.py",
    "dozorca-equity": "services/engine/dozorca.py",
    "kapital-equity": "services/engine/kapital_gieldy.py",
    "engine": "services/engine/loop.py",
    "engine-equity": "services/engine/loop.py",
}


@pytest.mark.parametrize("worker", sorted(MODULY))
def test_modul_karmi_sonde_na_starcie_i_po_obrocie(worker):
    assert VALUES["workers"][worker]["enabled"]
    zrodlo = (KORZEN / MODULY[worker]).read_text(encoding="utf-8")
    assert zrodlo.count("puls.zyje(") >= 2, (
        f"{MODULY[worker]}: sonda bez karmienia na starcie i po kazdym obrocie")


def test_obrot_NIEUDANY_tez_odswieza_plik(monkeypatch, tmp_path):
    """Sedno: awaria bazy nie moze zatrzymac pliku, bo zatrzymalaby go
    w KAZDYM workerze naraz."""
    import asyncio
    import sys
    from services.ingest import market_worker

    plik = tmp_path / "zywy"
    monkeypatch.setenv("PLIK_ZYWOTNOSCI", str(plik))
    zapisy = []
    prawdziwe = puls.zyje
    monkeypatch.setattr(market_worker.puls, "zyje",
                        lambda *a, **k: (zapisy.append(1), prawdziwe(*a, **k)))

    class G:
        venue_code = "x"
        async def close(self): pass

    stan = {"n": 0}

    def connect(url, **kw):
        stan["n"] += 1
        if stan["n"] > 1:
            raise OSError("baza lezy")
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
        return C()

    async def nic(_s): pass
    monkeypatch.setattr(asyncio, "sleep", nic)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(market_worker.psycopg, "connect", connect)
    monkeypatch.setattr(market_worker, "adapter_danych", lambda c, e, **k: G())
    monkeypatch.setattr(sys, "argv", ["market_worker.py", "--interval", "60"])
    assert asyncio.run(market_worker.main()) == 1
    # start + po kazdym z czterech pierwszych nieudanych obrotow (piaty konczy proces)
    assert len(zapisy) == 1 + market_worker.POD_RZAD_MAX - 1
    assert plik.exists()
