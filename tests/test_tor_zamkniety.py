"""Tory zamkniete wynikiem hipotezy: crypto 4h `spot_eu` (H-9) i `perp` (B-0).

H-9 § STOP i B-0 = STOP (2026-10-18, renacode-infra: docs/projekty/trader/
hipotezy.md): caly tor crypto zamkniety jako wynik negatywny; trener liczy
dalej (H-4, dryf), silnik demo chodzi dalej, ale nie otwiera nowych pozycji.
Testy pilnuja, ze zamkniecie robi dokladnie cztery rzeczy - odmowa live,
odmowa promocji, cisza czujek, ktore staly sie szumem, blokada NOWYCH WEJSC
na demo (wyjscia ida dalej) - i ze NIE dotyka torow otwartych (equity,
crypto 1h, wolajacy bez cennika).
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
from tests.test_run_once import (H1, INSTRUMENTY, POZYCJA, SESJA_ALPACA, cykl,
                                 plany, puls, swiat)  # noqa: F401  (fikstura)

KORZEN = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- jedno zrodlo

def test_zamkniete_sa_wylacznie_tory_h9_i_b0():
    assert list(ZAMKNIETE) == [("crypto", "4h", "spot_eu"), ("crypto", "4h", "perp")]
    z = zamkniecie("crypto", "4h", "spot_eu")
    assert z and z.hipoteza == "H-9" and z.data == "2026-10-18"
    assert "H-9" in z.opis() and "2026-10-18" in z.opis()
    p = zamkniecie("crypto", "4h", "perp")
    assert p and p.hipoteza == "B-0" and p.data == "2026-10-18"
    assert "B-0" in p.opis() and "2026-10-18" in p.opis()


@pytest.mark.parametrize("tor", [
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
    # Doszedl do sprawdzenia bazy, czyli za odmowe - demo chodzi dalej (puls,
    # wyjscia), tylko bez nowych wejsc (testy blokady nizej).
    kod = _loop_main(monkeypatch, "--bar", "4h", "--kind", "spot_eu", "--mode", "demo")
    assert "DATABASE_URL" in kod


def test_silnik_live_na_perp_odmawiany_po_b0(monkeypatch):
    kod = _loop_main(monkeypatch, "--bar", "4h", "--kind", "perp", "--mode", "live")
    assert "B-0" in kod and "odmówiony" in kod


def test_silnik_live_bez_kind_odmawiany(monkeypatch, capsys):
    # Cichy domysl `spot` omijal zamkniecie toru `spot_eu` i liczyl 0,20%.
    kod = _loop_main(monkeypatch, "--bar", "4h", "--mode", "live")
    assert kod == "2"
    assert "wymaga jawnego --kind" in capsys.readouterr().err


def test_silnik_demo_bez_kind_dalej_spot(monkeypatch):
    kod = _loop_main(monkeypatch, "--bar", "4h", "--mode", "demo")
    assert "DATABASE_URL" in kod


def test_run_once_odmawia_live_niezaleznie_od_parsera():
    from services.engine.loop import run_once
    with pytest.raises(ValueError, match="H-9"):
        asyncio.run(run_once(None, "crypto", mode="live", kind_kosztow="spot_eu",
                             h=get_horizon("4h")))


# ------------------------------------------------------------- demo: blokada wejsc
#
# `run_once` WYKONANY na atrapie z `test_run_once` (kursor znajacy CHECK-i
# `journal.planned_order`, prawdziwe `zaplanuj`, `nowe_stats`, puls).

def test_demo_na_torze_zamknietym_WEJSCIE_to_hold_a_WYJSCIE_idzie(swiat):  # noqa: F811
    swiat.wyniki = {1: 0.6, 2: -0.4}
    swiat.pozycje = {2: POZYCJA}
    stats, cur = cykl(swiat, kind_kosztow="spot_eu")

    decyzje = {d["iid"]: d for d in swiat.decyzje}
    assert decyzje[1]["action"] == "hold"
    assert decyzje[1]["powod_holdu"] == "tor_zamkniety"
    assert decyzje[1]["verdict"] == "allow"
    assert decyzje[2]["action"] == "exit"
    # Jedyny plan to WYJSCIE - otwarta pozycja domyka sie sama.
    assert [(p[1], p[2]) for p in plany(cur)] == [(2, "sell")]
    assert stats["tor_zamkniety"] == 1
    assert stats["wejscia"] == 0 and stats["wyjscia"] == 1
    # Puls bije dalej i niesie licznik; log cyklu nazywa powod.
    assert '"tor_zamkniety": 1' in puls(cur)
    assert "tor_zamkniety 1" in loop_opis(stats)


def test_demo_na_perp_blokuje_TAKZE_short(swiat):  # noqa: F811
    swiat.wyniki = {1: -0.6, 2: 0.6}
    perpy = [{**i, "kind": "perp"} for i in INSTRUMENTY]
    stats, cur = cykl(swiat, instrumenty=perpy, kind_kosztow="perp")

    assert {d["powod_holdu"] for d in swiat.decyzje} == {"tor_zamkniety"}
    assert {d["action"] for d in swiat.decyzje} == {"hold"}
    assert stats["tor_zamkniety"] == 2 and stats["short_niewykonalny"] == 0
    assert plany(cur) == []


def test_demo_na_torze_zamknietym_KILLSWITCH_ma_pierwszenstwo(swiat):  # noqa: F811
    # Proba pozarowa rozpoznaje ogniwo 3 po `wstrzymanie_ryzykiem` - blokada
    # toru nie moze go przykryc.
    swiat.killswitch = {"id": 23, "reason": "obsunięcie"}
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, _ = cykl(swiat, kind_kosztow="spot_eu")

    assert swiat.decyzje[0]["powod_holdu"] == "wstrzymanie_ryzykiem"
    assert stats["tor_zamkniety"] == 0


def test_demo_na_torze_OTWARTYM_wchodzi_jak_dotad(swiat):  # noqa: F811
    # Kanarek: ten sam cykl na `spot` (tor niezamkniety) daje wejscie.
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, cur = cykl(swiat, kind_kosztow="spot")

    assert swiat.decyzje[0]["action"] == "enter_long"
    assert stats["tor_zamkniety"] == 0 and stats["wejscia"] == 1
    assert [p[2] for p in plany(cur)] == ["buy"]


def test_equity_wchodzi_jak_dotad(swiat):  # noqa: F811
    from datetime import datetime, timezone
    swiat.teraz = datetime(2026, 10, 16, 14, 5, tzinfo=timezone.utc)
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, _ = cykl(swiat, engine="equity", h=H1, sesja=SESJA_ALPACA,
                    kind_kosztow="stock")

    assert stats["tor_zamkniety"] == 0 and stats["wejscia"] == 1


def loop_opis(stats):
    from services.engine.loop import opis_zatrzymanych_wejsc
    return opis_zatrzymanych_wejsc(stats)


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


def test_trener_promocja_bez_kind_odmawiana(monkeypatch, capsys):
    kod = _train_main(monkeypatch, "--bar", "4h", "--zatwierdz-promocje")
    assert kod == 2
    assert "wymaga jawnego --kind" in capsys.readouterr().err


def test_trener_bez_kind_i_bez_promocji_dalej_spot(monkeypatch, capsys):
    kod = _train_main(monkeypatch, "--bar", "4h")
    assert "DATABASE_URL" in str(kod)
    assert "TOR ZAMKNIĘTY" not in capsys.readouterr().out


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


def test_dryf_na_perp_bez_powiadomienia_a_bez_kind_powiadamia():
    a = AtrapaBazy(historia(0.695, 0.70, 0.68),
                   cechy=[{"feature": "rsi_14@1", "psi": 0.9}])
    assert uruchom(a, kind="perp") == (0, [])
    a = AtrapaBazy(historia(0.695, 0.70, 0.68),
                   cechy=[{"feature": "rsi_14@1", "psi": 0.9}])
    kod, wyslane = uruchom(a)
    assert kod == 1 and wyslane


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
    assert _zmiana_struktury(kind="perp") == (0, [])
    assert _zmiana_struktury() == (1, ["learning.zmiana_struktury"])


# ------------------------------------------------------------- chart

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")
def test_chart_podaje_dozorowi_cennik_toru_crypto():
    out = subprocess.run(["helm", "template", "t", str(KORZEN / "charts/trader")],
                         capture_output=True, text=True, check=True).stdout
    assert 'dozor.py --bar "4h" --kind "spot_eu" --kod-trenera $RC_CRYPTO' in out
    assert 'dozor.py --co dryf --bar "4h" --kind "spot_eu"' in out
    # Trener i dozor ten sam cennik toru - bez rozjazdu domyslow w szablonie.
    szablon = (KORZEN / "charts/trader/templates/trainer-cron.yaml").read_text(encoding="utf-8")
    assert 'trainerCron.kind | default "spot" ' not in szablon
    assert szablon.count('trainerCron.kind | default "spot_eu"') == 4
    # Silnik demo nadal chodzi na `spot_eu` - zamkniecie nie zmienia jego komendy.
    assert "--kind spot_eu --limit --wymagaj-modelu --egzekucja gielda --mode demo" in out
