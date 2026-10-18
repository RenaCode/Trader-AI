"""Tor zamkniety wynikiem hipotezy: crypto 4h `spot_eu` po H-9 (2026-10-18).

H-9 § STOP (renacode-infra: docs/projekty/trader/hipotezy.md): ensemble 4h na
`spot_eu` zamkniety NA STALE; trener liczy dalej (H-4, dryf), demo gra dalej
wylacznie jako pomiar egzekucji. Testy pilnuja, ze zamkniecie robi dokladnie
trzy rzeczy - odmowa live, odmowa promocji, cisza czujek, ktore staly sie
szumem - i ze NIE dotyka torow, ktorych H-9 nie rozstrzyga (perp z B-0 / H-12,
equity, crypto 1h), ani wejsc na demo.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from services.engine import dozor
from services.engine.horizons import get as get_horizon
from services.engine.tory import ZAMKNIETE, zamkniecie
from tests.test_dryf import AtrapaBazy, historia, uruchom

KORZEN = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- jedno zrodlo

def test_zamkniety_jest_wylacznie_tor_h9():
    assert list(ZAMKNIETE) == [("crypto", "4h", "spot_eu")]
    z = zamkniecie("crypto", "4h", "spot_eu")
    assert z and z.hipoteza == "H-9" and z.data == "2026-10-18"
    assert "H-9" in z.opis() and "2026-10-18" in z.opis()


@pytest.mark.parametrize("tor", [
    ("crypto", "4h", "perp"),      # B-0 / H-12 - czeka na decyzje do 25.10
    ("crypto", "1h", "spot_eu"),
    ("equity", "1h", "stock"),
    ("crypto", "4h", None),        # wolajacy bez cennika nie wycisza niczego
])
def test_inne_tory_otwarte(tor):
    assert zamkniecie(*tor) is None


# ------------------------------------------------------------- silnik

def _loop_main(monkeypatch, *argv):
    from services.engine import loop
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["loop.py", *argv])
    with pytest.raises(SystemExit) as e:
        asyncio.run(loop.main())
    return str(e.value.code)


def test_silnik_odmawia_live_na_torze_zamknietym(monkeypatch):
    kod = _loop_main(monkeypatch, "--bar", "4h", "--kind", "spot_eu", "--mode", "live")
    assert "H-9" in kod and "odmówiony" in kod


def test_silnik_demo_na_torze_zamknietym_startuje(monkeypatch):
    # Doszedl do sprawdzenia bazy, czyli za odmowe - demo gra dalej (H-10, B-0).
    kod = _loop_main(monkeypatch, "--bar", "4h", "--kind", "spot_eu", "--mode", "demo")
    assert "DATABASE_URL" in kod


def test_silnik_live_na_perp_nie_jest_odmawiany_przez_h9(monkeypatch):
    kod = _loop_main(monkeypatch, "--bar", "4h", "--kind", "perp", "--mode", "live")
    assert "DATABASE_URL" in kod


def test_run_once_odmawia_live_niezaleznie_od_parsera():
    from services.engine.loop import run_once
    with pytest.raises(ValueError, match="H-9"):
        asyncio.run(run_once(None, "crypto", mode="live", kind_kosztow="spot_eu",
                             h=get_horizon("4h")))


# ------------------------------------------------------------- trener

def _train_main(monkeypatch, *argv):
    from services.engine import train
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["train.py", *argv])
    with pytest.raises(SystemExit) as e:
        train.main()
    return e.value.code


def test_trener_odmawia_promocji_na_torze_zamknietym(monkeypatch, capsys):
    kod = _train_main(monkeypatch, "--bar", "4h", "--kind", "spot_eu",
                      "--zatwierdz-promocje")
    assert kod == 2
    assert "H-9" in capsys.readouterr().err


def test_trener_liczy_dalej_bez_promocji(monkeypatch, capsys):
    kod = _train_main(monkeypatch, "--bar", "4h", "--kind", "spot_eu")
    assert "DATABASE_URL" in str(kod)
    assert "TOR ZAMKNIĘTY" in capsys.readouterr().out


def test_trener_promuje_inne_tory(monkeypatch):
    kod = _train_main(monkeypatch, "--bar", "1h", "--kind", "spot_eu",
                      "--zatwierdz-promocje")
    assert "DATABASE_URL" in str(kod)


# ------------------------------------------------------------- dozor

def test_dryf_na_torze_zamknietym_bez_powiadomienia():
    a = AtrapaBazy(historia(0.695, 0.70, 0.68),
                   cechy=[{"feature": "rsi_14@1", "psi": 0.9}])
    kod, wyslane = uruchom(a, kind="spot_eu")
    assert (kod, wyslane) == (0, [])
    # Werdykt policzony do konca (rozpiska cech), tylko nie wyslany.
    assert any("drift_metric" in q for q in a.zapytania)


def test_dryf_bez_kind_i_na_perp_powiadamia():
    for kw in ({}, {"kind": "perp"}):
        a = AtrapaBazy(historia(0.695, 0.70, 0.68),
                       cechy=[{"feature": "rsi_14@1", "psi": 0.9}])
        kod, wyslane = uruchom(a, **kw)
        assert kod == 1 and wyslane, kw


def _zmiana_struktury(**kw):
    wyslane = []

    async def fetch_all(sql, params=None):
        if "zmiana_struktury" in sql:
            return [{"id": 7, "version": 12, "zmiana_struktury": "+strona",
                     "zgloszona": False}]
        return [{"id": 1}]

    async def notify(uid, kod, tytul, tresc, **reszta):
        wyslane.append(kod)

    kod = asyncio.run(dozor.sprawdz_zmiane_struktury(
        fetch_all, notify, engine="crypto", bar="4h", **kw))
    return kod, wyslane


def test_zmiana_struktury_na_torze_zamknietym_bez_powiadomienia():
    assert _zmiana_struktury(kind="spot_eu") == (0, [])
    assert _zmiana_struktury(kind="perp") == (1, ["learning.zmiana_struktury"])
    assert _zmiana_struktury() == (1, ["learning.zmiana_struktury"])


# ------------------------------------------------------------- chart

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")
def test_chart_podaje_dozorowi_cennik_toru_crypto():
    out = subprocess.run(["helm", "template", "t", str(KORZEN / "charts/trader")],
                         capture_output=True, text=True, check=True).stdout
    assert 'dozor.py --bar "4h" --kind "spot_eu" --kod-trenera $RC_CRYPTO' in out
    assert 'dozor.py --co dryf --bar "4h" --kind "spot_eu"' in out
    # Silnik demo nadal chodzi na `spot_eu` - zamkniecie nie zmienia jego komendy.
    assert "--kind spot_eu --limit --wymagaj-modelu --egzekucja gielda --mode demo" in out
