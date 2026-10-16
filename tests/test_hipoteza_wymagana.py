"""Proces badawczy i promocji (audyt Fable T8: F-3.1, F-3.3, F-3.4, F-2.3).

  * `--sucho` (i `--panel`) wymaga `--hipoteza H-n` z wierszem w renacode-infra: docs/projekty/trader/hipotezy.md,
  * `status_walidacji` wymaga przewagi nad B2 w >= 60% okien i mowi, co blokuje,
  * dozor wysyla `learning.zmiana_struktury` raz na wersje-kandydata,
  * trener chodzi raz w tygodniu.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

from services.engine import dozor
from services.engine import train as tr

KORZEN = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------ --hipoteza

def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["train.py", *argv])
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as e:
        tr.main()
    return str(e.value)


def test_sucho_bez_hipotezy_odmawia(monkeypatch):
    assert "--hipoteza" in _main(monkeypatch, "--sucho")


def test_panel_bez_hipotezy_odmawia(monkeypatch):
    assert "--hipoteza" in _main(monkeypatch, "--panel", "nie-istnieje.json.gz")


# Rejestr hipotez zyje w prywatnym repo (renacode-infra:
# docs/projekty/trader/hipotezy.md), ktorego CI nie widzi - wiec test bramki
# rejestru dostaje wlasny, tymczasowy rejestr o tym samym ukladzie tabeli.
def test_hipoteza_spoza_rejestru_odmawia(tmp_path):
    rejestr = tmp_path / "hipotezy.md"
    rejestr.write_text("| H-1 | 2026-10-03 | opis |\n", encoding="utf-8")
    assert "brak wiersza" in tr.sprawdz_hipoteze("H-999", rejestr)
    assert tr.sprawdz_hipoteze("H-1", rejestr) is None


def test_hipoteza_z_rejestru_przechodzi_dalej(monkeypatch):
    # Dalej odmawia juz brak bazy - czyli bramka hipotezy przepuscila.
    assert "DATABASE_URL" in _main(monkeypatch, "--sucho", "--hipoteza", "H-1")


def test_przebieg_produkcyjny_NIE_wymaga_hipotezy(monkeypatch):
    assert "DATABASE_URL" in _main(monkeypatch)


def test_zly_format_numeru():
    assert "H-n" in tr.sprawdz_hipoteze("1")
    assert tr.sprawdz_hipoteze("H-B2") is None


def test_rejestr_poza_obrazem_sprawdza_tylko_format(tmp_path):
    assert tr.sprawdz_hipoteze("H-7", tmp_path / "brak.md") is None


def test_numer_hipotezy_trafia_do_podsumowania():
    sys.path.insert(0, str(KORZEN / "tests"))
    import contextlib
    import io

    from panel_syntetyczny import plik_syntetyczny

    from services.engine.horizons import get as get_horizon
    with contextlib.redirect_stdout(io.StringIO()):
        w = tr.train(None, "crypto", 300, 2, get_horizon("4h"), "spot_eu", sucho=True,
                     wybor="ic", panel=plik_syntetyczny(), hipoteza="H-1")
    assert w["hipoteza"] == "H-1"


def test_eksperyment_wymaga_hipotezy(monkeypatch):
    sys.path.insert(0, str(KORZEN / "scripts"))
    import eksperyment
    with pytest.raises(SystemExit):
        eksperyment.main(["--panel", "x"])


# ------------------------------------------------------------ B2 w bramce

DOBRE = dict(total=40, positive=30, median=0.117, bije_odniesienie=True, dsr_ok=True)


def test_bije_B2_w_60_procentach_okien_jest_warunkiem_validated():
    assert tr.status_walidacji(**DOBRE, udzial_b2=0.60) == "validated"
    assert tr.status_walidacji(**DOBRE, udzial_b2=0.59) == "learning"
    assert tr.status_walidacji(**DOBRE, udzial_b2=None) == "learning"


def test_kryteria_blokujace_nazywaja_KAZDY_niespelniony_warunek():
    blok = tr.kryteria_blokujace(40, 26, 0.03, False, False, 0.5)
    tekst = " | ".join(blok)
    for fraza in ("okna dodatnie 65%", "mediana", "kup-i-trzymaj", "B2 w 50%", "DSR"):
        assert fraza in tekst
    assert tr.kryteria_blokujace(**DOBRE, udzial_b2=0.7) == []


def test_powod_statusu_wypisuje_co_blokuje():
    z = tr.Przebieg()
    z.all_sharpes = [0.1] * 21 + [-0.1] * 9
    z.positive = 21
    z.bije_odn["B2"] = [10, 30]
    from services.engine.koszty import karta_rundy
    w = tr.werdykt(z, karta=karta_rundy("spot_eu", True, spread_bps=5.0), n=10,
                   trials=1, sd_prob=None, konfiguracji_z_oos=0)
    assert w.status == "learning"
    assert "Blokuje:" in w.reason and "bije B2 w 33% okien < 60%" in w.reason


# ------------------------------------------------- learning.zmiana_struktury

def test_dozor_zglasza_zmiane_raz_na_wersje():
    wiersz = {"id": 7, "version": 12, "zmiana_struktury": "+progi_na_wagach",
              "zgloszona": False}
    assert "wersja 12" in dozor.ocen_zmiane_struktury(wiersz)
    assert dozor.ocen_zmiane_struktury(wiersz | {"zgloszona": True}) is None
    assert dozor.ocen_zmiane_struktury(None) is None


def test_dozor_wysyla_typ_learning_zmiana_struktury():
    wyslane = []

    async def fetch_all(sql, params=None):
        if "zmiana_struktury" in sql:
            assert params == ("crypto_signal_weighting_4h",)
            return [{"id": 7, "version": 12, "zmiana_struktury": "próg range +25%",
                     "zgloszona": False}]
        return [{"id": 1}]

    async def notify(uid, kod, tytul, tresc, **kw):
        wyslane.append((kod, kw["data"]))

    asyncio.run(dozor.sprawdz_zmiane_struktury(fetch_all, notify, engine="crypto",
                                               bar="4h"))
    assert wyslane == [("learning.zmiana_struktury", {"model_version_id": "7", "bar": "4h"})]


def test_migracja_0090_dodaje_typ_kolumne_i_lock_timeout():
    sql = (KORZEN / "db/migrations/0090_zmiana_struktury_modelu.sql").read_text(encoding="utf-8")
    assert "SET LOCAL lock_timeout" in sql
    assert "'learning.zmiana_struktury'" in sql
    assert "ADD COLUMN IF NOT EXISTS zmiana_struktury text" in sql


# ------------------------------------------------------------ harmonogram

def test_trener_chodzi_raz_w_tygodniu_w_niedziele():
    values = (KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8")
    m = re.search(r'^trainerCron:.*?^  schedule: "([^"]+)"', values, re.S | re.M)
    assert m and m.group(1) == "40 4 * * 0"


def test_tor_equity_trenuje_sie():
    """R9 (wstrzymanie 2026-10-03) zdjete decyzja wlasciciela 2026-10-06:
    demo ma trenowac algorytm przed live, a bez toru equity silnik equity
    nie ma ani stanu nauki, ani kandydata."""
    values = (KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8")
    blok = values[values.index("\ntrainerCron:"):]
    blok = blok[blok.index("  equity:"):]
    assert re.match(r"  equity:\n    enabled: true\n", blok)
