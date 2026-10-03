"""Zwrot naprzód 24/48 h: wejścia wypełnione kontra niewypełnione.

CO BYŁO ZŁE (audyt Fable F-4.5, 2026-10-03)

`dozor --co poslizg` liczył wypełnialność, ale nie to, CO tracimy przez
niewypełnienie. Zlecenie PostOnly pod rynkiem wypełnia się wtedy, gdy rynek do
niego zejdzie — czyli częściej w złych wejściach niż w dobrych (selekcja
negatywna). Analiza z 03.10 (§3.4) dała +407 pb / 48 h dla niewypełnionych
wobec wypełnionych, CI [+70; +773] — liczbę policzoną ręcznie, raz.

Teraz raport liczy ją przy każdym przebiegu: średni zwrot naprzód od ceny
zlecenia dla obu grup i ich różnicę, z przedziałem z bootstrapu (bez założenia
normalności — próba ma kilkanaście obserwacji i grube ogony).
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest

from services.engine.poslizg import (
    HORYZONTY_NAPRZOD_H, bootstrap_przedzial, bootstrap_roznicy, opis_selekcji,
    zwrot_naprzod)


def _w(status, strona, cena, c24, c48):
    return {"status": status, "strona": strona, "cena": Decimal(str(cena)),
            "cena_24h": None if c24 is None else Decimal(str(c24)),
            "cena_48h": None if c48 is None else Decimal(str(c48))}


def test_horyzonty():
    assert HORYZONTY_NAPRZOD_H == (24, 48)


def test_zwrot_liczony_od_ceny_zlecenia_ze_znakiem_strony():
    z = zwrot_naprzod([_w("filled", "buy", 100, 101, 103),
                       _w("cancelled", "buy", 100, 104, 106),
                       _w("rejected", "sell", 100, 98, 97)])
    assert z[24]["wypelnione"]["n"] == 1
    assert z[24]["wypelnione"]["srednia"] == pytest.approx(0.01)
    # sprzedaż: spadek ceny to zysk pozycji krótkiej
    assert z[24]["niewypelnione"]["n"] == 2
    assert z[24]["niewypelnione"]["srednia"] == pytest.approx((0.04 + 0.02) / 2)
    assert z[48]["niewypelnione"]["srednia"] == pytest.approx((0.06 + 0.03) / 2)


def test_czesciowe_to_wypelnione_a_w_locie_poza_proba():
    z = zwrot_naprzod([_w("partial", "buy", 100, 101, 101),
                       _w("new", "buy", 100, 150, 150),
                       _w("unknown", "buy", 100, 150, 150)])
    assert z[24]["wypelnione"]["n"] == 1
    assert z[24]["niewypelnione"]["n"] == 0


def test_brak_ceny_naprzod_wypada_tylko_z_tego_horyzontu():
    """Zlecenie sprzed 30 h ma już cenę po 24 h, a po 48 h jeszcze nie."""
    z = zwrot_naprzod([_w("filled", "buy", 100, 101, None)])
    assert z[24]["wypelnione"]["n"] == 1
    assert z[48]["wypelnione"]["n"] == 0


def test_roznica_niewypelnione_minus_wypelnione():
    wiersze = ([_w("filled", "buy", 100, 99 + k * 0.1, 99) for k in range(10)]
               + [_w("cancelled", "buy", 100, 104 + k * 0.1, 106) for k in range(10)])
    z = zwrot_naprzod(wiersze)
    r = z[24]["roznica"]
    assert r["srednia"] == pytest.approx(0.05, abs=1e-9)
    lo, hi = r["przedzial"]
    assert 0 < lo < 0.05 < hi, "wyraźna selekcja negatywna: CI różnicy nad zerem"


def test_bootstrap_deterministyczny_i_bez_przedzialu_przy_jednej_obserwacji():
    xs = [0.01, -0.02, 0.03, 0.0, 0.05]
    assert bootstrap_przedzial(xs) == bootstrap_przedzial(xs)
    assert bootstrap_przedzial([0.01]) is None
    assert bootstrap_roznicy([0.01], [0.02, 0.03]) is None
    lo, hi = bootstrap_przedzial(xs)
    assert lo < sum(xs) / len(xs) < hi


def test_opis_mowi_NIE_ZMIERZONO_przy_pustej_probie():
    linie = opis_selekcji(zwrot_naprzod([]))
    assert any("NIE ZMIERZONO" in w for w in linie)


def test_opis_podaje_n_srednia_i_przedzial():
    wiersze = ([_w("filled", "buy", 100, 101, 102), _w("filled", "buy", 100, 99, 98)]
               + [_w("cancelled", "buy", 100, 103, 105),
                  _w("expired", "buy", 100, 104, 107)])
    tekst = "\n".join(opis_selekcji(zwrot_naprzod(wiersze)))
    assert "24 h" in tekst and "48 h" in tekst
    assert "n=2" in tekst
    assert "różnica" in tekst and "CI" in tekst


def test_dozor_poslizg_wypisuje_selekcje(capsys):
    from services.engine.dozor import sprawdz_poslizg

    async def fetch_all(sql, params=None):
        if "cena_24h" in sql:
            return [_w("filled", "buy", 100, 101, 102),
                    _w("cancelled", "buy", 100, 104, 106)]
        return []

    assert asyncio.run(sprawdz_poslizg(fetch_all)) == 0
    assert "selekcja negatywna" in capsys.readouterr().out


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_zapytanie_przechodzi_przez_postgresa(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    from services.api import db as api_db
    from services.engine.poslizg import pobierz_zwrot_naprzod

    async def przebieg():
        await api_db.open_pool()
        try:
            return await pobierz_zwrot_naprzod(api_db.fetch_all)
        finally:
            await api_db.close_pool()

    assert asyncio.run(przebieg()) == []
