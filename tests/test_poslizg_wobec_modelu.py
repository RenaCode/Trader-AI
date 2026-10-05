"""Realizacja z gieldy wobec modelu pesymistycznego H-1/H-3 (`poslizg.wobec_modelu`).

Po to chodzi strategia momentum na demo (E-1): wypelnialnosc wejsc,
opoznienie wysylki, prowizje i wyjscie wzgledem ceny decyzji, kazde obok
liczby, ktora przyjal model. Testy na wierszach w ksztalcie zapytan
`ZAPYTANIE` / `ZAPYTANIE_ZLECENIA` - bez bazy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from services.engine import poslizg as p

UTC = timezone.utc
POLNOC = datetime(2026, 10, 5, tzinfo=UTC)


def test_model_to_liczby_z_H3_i_cennika():
    m = p.model_pesymistyczny("spot_eu")
    assert m["opoznienie_min"] == 20
    assert m["wypelnialnosc_wejsc"] == (0.770, 0.796)
    assert (m["prowizja_wejscia"], m["prowizja_wyjscia"]) == (0.0025, 0.0025)
    assert m["poslizg_wyjscia"] == 0.0004


def test_zamkniecie_swiecy_z_chwili_decyzji():
    assert p.koniec_swiecy_decyzji(POLNOC + timedelta(minutes=7, seconds=3)) == POLNOC
    assert p.koniec_swiecy_decyzji(POLNOC + timedelta(hours=4, minutes=1)) == \
        POLNOC + timedelta(hours=4)


def _zlecenie(plan, status, minuty, rodzaj="wejscie"):
    return {"planned_order_id": plan, "status": status, "rodzaj_planu": rodzaj,
            "decided_at": POLNOC + timedelta(minutes=5),
            "submitted_at": POLNOC + timedelta(minutes=minuty)}


def test_opoznienie_i_wypelnialnosc_licza_PLANY_wejsc_nie_wiersze():
    zlecenia = [
        _zlecenie(1, "new", 6), _zlecenie(1, "filled", 9),    # jeden plan, dwa wiersze
        _zlecenie(2, "cancelled", 12),
        _zlecenie(3, "filled", 30),
        _zlecenie(4, "rejected", 7, rodzaj="wyjscie"),        # wyjscie - poza proba
    ]
    m = p.wobec_modelu([], zlecenia)
    w = m["wypelnialnosc_wejsc"]
    assert (w["wypelnionych"], w["niewypelnionych"], w["rozstrzygnietych"]) == (2, 1, 3)
    assert w["udzial"] == pytest.approx(2 / 3)
    o = m["opoznienie"]
    assert o["n"] == 3 and o["mediana"] == 12 and o["max"] == 30


def test_prowizja_kupna_w_walucie_BAZOWEJ_przeliczona_po_cenie():
    kupno = {"rodzaj_planu": "wejscie", "symbol": "BTCUSDC", "cena_wykonania": D("60000"),
             "ilosc": D("0.01"), "prowizja": D("0.00001"), "waluta_prowizji": "BTC"}
    sprzedaz = {"rodzaj_planu": "wyjscie", "symbol": "BTCUSDC",
                "cena_wykonania": D("61000"), "ilosc": D("0.01"),
                "prowizja": D("1.525"), "waluta_prowizji": "USDC"}
    assert p.stawka_prowizji(kupno) == pytest.approx(0.0010)
    assert p.stawka_prowizji(sprzedaz) == pytest.approx(0.0025)
    pr = p.prowizje([kupno, sprzedaz])
    assert pr["wejscie"]["n"] == pr["wyjscie"]["n"] == 1


def test_wyjscie_wzgledem_decyzji_dodatnie_to_koszt():
    wiersze = [{"rodzaj_planu": "wyjscie", "strona": "sell", "cena_decyzji": D("100"),
                "cena_wykonania": D("99.9")},
               {"rodzaj_planu": "wyjscie", "strona": "sell", "cena_decyzji": D("100"),
                "cena_wykonania": D("100.1")},
               {"rodzaj_planu": "wejscie", "strona": "buy", "cena_decyzji": D("100"),
                "cena_wykonania": D("99.5")}]
    wy = p.poslizg_od_decyzji(wiersze, "wyjscie")
    assert wy["n"] == 2 and wy["srednia"] == pytest.approx(0.0)
    we = p.poslizg_od_decyzji(wiersze, "wejscie")
    assert we["n"] == 1 and we["srednia"] == pytest.approx(-0.005)


def test_opis_bez_obserwacji_mowi_NIE_ZMIERZONO_przy_kazdej_mierze():
    linie = p.opis_wobec_modelu(p.wobec_modelu([], []))
    assert len(linie) == 7
    assert all("NIE ZMIERZONO" in x for x in linie[1:])
    assert "model 77.0%–79.6%" in linie[1] and "model 20 min" in linie[2]


def test_zapytania_filtruja_po_strategii_decyzji():
    for q in (p.ZAPYTANIE, p.ZAPYTANIE_ZLECENIA, p.ZAPYTANIE_ZWROT_NAPRZOD):
        zwarte = " ".join(q.split())
        assert "d.strategia = %(strategia)s" in zwarte
        assert "%(strategia)s::text IS NULL" in zwarte
