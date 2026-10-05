"""przebieg_zadania czeka na baze przed poleceniem (2026-10-04: trener padl
na `Connection refused`, bo kube-router wpuszcza swiezy pod z opoznieniem)."""
from __future__ import annotations

from services.engine import przebieg_zadania as pz


class _Gniazdo:
    def close(self):
        pass


def test_ponawia_az_baza_przyjmie(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://trader@trader-db:5432/trader")
    proby = []

    def polacz(adres, timeout):
        proby.append(adres)
        if len(proby) < 3:
            raise ConnectionRefusedError("refused")
        return _Gniazdo()

    monkeypatch.setattr(pz.time, "sleep", lambda s: None)
    assert pz.czekaj_na_baze(limit_s=60, odstep_s=0, polacz=polacz) is True
    assert proby == [("trader-db", 5432)] * 3


def test_po_limicie_rusza_mimo_to(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://trader@trader-db/trader")

    def polacz(adres, timeout):
        raise ConnectionRefusedError("refused")

    assert pz.czekaj_na_baze(limit_s=0, odstep_s=0, polacz=polacz) is False


def test_bez_url_nic_nie_sprawdza(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def polacz(adres, timeout):
        raise AssertionError("nie powinno laczyc")

    assert pz.czekaj_na_baze(polacz=polacz) is True


def test_uruchom_czeka_przed_poleceniem(monkeypatch):
    kolejnosc = []
    monkeypatch.setattr(pz, "czekaj_na_baze", lambda: kolejnosc.append("baza"))

    class Proc:
        def __init__(self, polecenie):
            kolejnosc.append("polecenie")

        def wait(self):
            return 0

    monkeypatch.setattr(pz.subprocess, "Popen", Proc)
    monkeypatch.setattr(pz, "zapisz", lambda *a, **_: True)
    assert pz.uruchom("trainer", ["true"]) == 0
    assert kolejnosc == ["baza", "polecenie"]
