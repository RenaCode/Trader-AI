"""Pętla silnika: jeden cykl (engine, mode) naraz i klucz giełdy czytany co cykl.

DWIE WADY Z AUDYTU 2026-10-03:

  G1. W bazie nie było nic, co powstrzymałoby DRUGĄ pętlę tego samego silnika
      (ręczne `loop.py --once --limit` obok działającego poda, pod zawieszony
      w Terminating). Dwie pętle to dwie decyzje, dwa plany o różnych id
      i dwa zlecenia — podwójna pozycja.
  G3. Adapter giełdy powstawał raz na proces. Klucz wymieniony w UI nie
      docierał do silnika aż do restartu poda.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from services.engine import loop

TEST_DB_URL = os.environ.get("TEST_DB_URL")


class Wynik:
    def __init__(self, wiersz):
        self._w = wiersz

    def fetchone(self):
        return self._w


class Polaczenie:
    """Atrapa połączenia na jeden cykl; odpowiedzi z listy wspólnej dla cykli."""

    def __init__(self, stan):
        self._stan = stan

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return None

    def execute(self, sql, params=None):
        if "pg_try_advisory_lock" in sql:
            self._stan["klucze_blokady"].append(params[0])
            return Wynik({"wolne": self._stan["wolne"]})
        if "FROM app.api_credential" in sql:
            kl = self._stan["klucze_api"].pop(0)
            return Wynik(None if kl is None else {"id": kl})
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")


async def _sesja(*a, **k):
    class S:
        otwarta = True

        def opis(self):
            return "otwarta"
    return S()


def _uruchom(monkeypatch, stan, argv):
    cykle = []

    async def cykl(conn, *a, **k):
        cykle.append(k.get("gielda"))
        return {"rozwazone": 0, "wejscia": 0, "wyjscia": 0, "wstrzymane": 0,
                "zablokowane": 0, "poza_swieca": 0, "wypelnione": 0,
                "niewypelnione": 0, "czekaja": 0}

    monkeypatch.setattr(sys, "argv", ["loop.py", "--quiet", *argv])
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(loop.psycopg, "connect", lambda *a, **k: Polaczenie(stan))
    monkeypatch.setattr(loop, "stan_sesji", _sesja)
    monkeypatch.setattr(loop, "run_once", cykl)
    return asyncio.run(loop.main()), cykle


# ====================================================================== G1

def test_G1_zajety_cykl_NIE_jest_wykonywany_i_daje_kod_bledu(monkeypatch):
    stan = {"wolne": False, "klucze_blokady": [], "klucze_api": []}
    kod, cykle = _uruchom(monkeypatch, stan, ["--once", "--engine", "crypto"])
    assert kod != 0
    assert cykle == [], "druga pętla wykonała cykl mimo zajętej blokady"
    assert stan["klucze_blokady"] == ["engine:crypto:demo"]


def test_G1_wolny_cykl_jest_wykonywany(monkeypatch):
    """Kanarek: blokada nie może zatrzymać jedynej pętli."""
    stan = {"wolne": True, "klucze_blokady": [], "klucze_api": []}
    kod, cykle = _uruchom(monkeypatch, stan, ["--once"])
    assert kod == 0 and len(cykle) == 1


def test_G1_klucz_rozroznia_silnik_i_tryb():
    assert len({loop.klucz_cyklu(e, m) for e in ("crypto", "equity")
                for m in ("demo", "live")}) == 4


@pytest.mark.skipif(not TEST_DB_URL,
                    reason="ustaw TEST_DB_URL na jednorazową bazę z migracjami")
def test_G1_drugie_polaczenie_dostaje_odmowe_na_prawdziwej_bazie():
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as a, \
            psycopg.connect(TEST_DB_URL, row_factory=dict_row) as b:
        loop.zajmij_cykl(a, "crypto", "demo")
        with pytest.raises(loop.DrugaPetla):
            loop.zajmij_cykl(b, "crypto", "demo")
        loop.zajmij_cykl(b, "equity", "demo")       # inny silnik - wolny
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as c:
        loop.zajmij_cykl(c, "crypto", "demo")       # zamknięte połączenie zwolniło


# ====================================================================== G3

class Adapter:
    def __init__(self, nr):
        self.nr = nr
        self.zamkniety = False

    async def close(self):
        self.zamkniety = True


def _z_gielda(monkeypatch):
    zbudowane = []

    async def zbuduj(url, mode, engine="crypto"):
        a = Adapter(len(zbudowane) + 1)
        zbudowane.append(a)
        return a

    monkeypatch.setattr(loop, "zbuduj_adapter", zbuduj)
    monkeypatch.setattr(loop, "zamknij_pozycje_papierowe", lambda *a, **k: [])
    return zbudowane


def test_G3_zmieniony_klucz_przebudowuje_adapter_bez_restartu(monkeypatch):
    zbudowane = _z_gielda(monkeypatch)
    # start: klucz 1; cykl: aktywny jest już klucz 2
    stan = {"wolne": True, "klucze_blokady": [], "klucze_api": [1, 2]}
    kod, cykle = _uruchom(monkeypatch, stan, ["--once", "--egzekucja", "gielda"])
    assert kod == 0
    assert len(zbudowane) == 2, "adapter nie został przebudowany po zmianie klucza"
    assert zbudowane[0].zamkniety, "stary adapter został otwarty"
    assert cykle == [zbudowane[1]], "cykl poszedł starym adapterem"


def test_G3_ten_sam_klucz_NIE_przebudowuje(monkeypatch):
    zbudowane = _z_gielda(monkeypatch)
    stan = {"wolne": True, "klucze_blokady": [], "klucze_api": [7, 7]}
    kod, cykle = _uruchom(monkeypatch, stan, ["--once", "--egzekucja", "gielda"])
    assert kod == 0 and len(zbudowane) == 1 and cykle == [zbudowane[0]]


def test_G3_klucz_usuniety_w_trakcie_to_awaria_cyklu(monkeypatch):
    zbudowane = _z_gielda(monkeypatch)
    stan = {"wolne": True, "klucze_blokady": [], "klucze_api": [7, None]}
    kod, cykle = _uruchom(monkeypatch, stan, ["--once", "--egzekucja", "gielda"])
    assert kod != 0 and cykle == []
