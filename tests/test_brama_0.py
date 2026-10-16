"""Brama 0 jako liczba z danych, nie zdanie w dokumencie (renacode-infra: docs/projekty/trader/08-przed-go-live.md, 2026-10-11).

Runda = 2 × maker (konto DOCELOWE) + spread własnej księgi z 30 dni +
koszt niewypełnienia z demo; próg 12 pb, bo przy IC 0,03–0,04 i σ zwrotu
12 h 3,2% przewaga brutto to ~10–13 pb. Do 11.10 brama stała w renacode-infra: docs/projekty/trader/08-przed-go-live.md jako
OTWARTA pozycja listy, a w kodzie nie było nic, co by ją liczyło — więc nic
nie mogło jej zamknąć.

Zero wywołań giełdy: funkcja dostaje liczby, a stawka `spot_eu` pochodzi
z `CENNIK` (odczyt z konta demo 2026-10-05).
"""
from __future__ import annotations

import pytest

from services.engine.koszty import (
    NIEROZSTRZYGNIETA, OTWARTA, PROG_BRAMY_0_PB, ZAMKNIETA, brama_0, koszty,
    opis_bramy_0,
)


def test_bybit_eu_nie_przechodzi_bramy_0_na_samej_prowizji():
    """Maker 0,25% to 50 pb na rundę - ponad czterokrotność progu, zanim
    doliczy się spread (7 pb z karty 11.10) i niewypełnienie."""
    b = brama_0(koszty("spot_eu").maker, stawka_konta_docelowego=False,
                spread_pb=7.03, spread_nasza_ksiega=False, niewypelnienie_pb=None)
    assert b.stan == ZAMKNIETA
    assert b.skladniki["prowizja"] == pytest.approx(50.0)
    assert b.runda_pb == pytest.approx(57.03)
    # Braki są wypisane, choć werdyktu nie zmieniają.
    assert any("konta docelowego" in p for p in b.powody)
    assert any("niewypelnienia" in p for p in b.powody)
    tekst = "\n".join(opis_bramy_0(b))
    assert "NIE ZMIERZONO" in tekst and "ZAMKNIETA" in tekst


def test_otwiera_wylacznie_komplet_pomiarow_ponizej_progu():
    ok = brama_0(0.0002, stawka_konta_docelowego=True, spread_pb=3.0,
                 spread_nasza_ksiega=True, niewypelnienie_pb=4.0)
    assert ok.stan == OTWARTA and ok.runda_pb == pytest.approx(11.0)
    # Ta sama runda ze stawką z demo - nie wiadomo, czy live płaci tyle samo.
    demo = brama_0(0.0002, stawka_konta_docelowego=False, spread_pb=3.0,
                   spread_nasza_ksiega=True, niewypelnienie_pb=4.0)
    assert demo.stan == NIEROZSTRZYGNIETA
    # Spread z obcej księgi to tylko podłoga.
    obcy = brama_0(0.0002, stawka_konta_docelowego=True, spread_pb=3.0,
                   spread_nasza_ksiega=False, niewypelnienie_pb=4.0)
    assert obcy.stan == NIEROZSTRZYGNIETA
    # Brak niewypełnienia nie jest zerem.
    brak = brama_0(0.0002, stawka_konta_docelowego=True, spread_pb=3.0,
                   spread_nasza_ksiega=True, niewypelnienie_pb=None)
    assert brak.stan == NIEROZSTRZYGNIETA


def test_prog_jest_wlacznie():
    b = brama_0(0.0002, stawka_konta_docelowego=True, spread_pb=4.0,
                spread_nasza_ksiega=True, niewypelnienie_pb=PROG_BRAMY_0_PB - 8.0)
    assert b.runda_pb == pytest.approx(PROG_BRAMY_0_PB) and b.stan == OTWARTA


def test_spadajacy_rynek_nie_otwiera_bramy():
    """Ujemny koszt niewypełnienia = wejścia, które nie weszły, straciłyby.
    Na demo 03-05.10: −152 pb w 48 h na 12 niewypełnionych - to kierunek rynku,
    nie tańsza egzekucja. Wchodzi jako zero."""
    b = brama_0(0.0005, stawka_konta_docelowego=True, spread_pb=3.0,
                spread_nasza_ksiega=True, niewypelnienie_pb=-52.0)
    assert b.skladniki["niewypelnienie"] == 0.0
    assert b.runda_pb == pytest.approx(13.0) and b.stan == ZAMKNIETA


def test_trener_wypisuje_brame_0():
    """Każdy przebieg z limitem pokazuje werdykt w logu - w niedzielę, bez pytania."""
    import inspect

    from services.engine import train as tr
    zrodlo = inspect.getsource(tr.wypisz_podsumowanie)
    assert "opis_bramy_0(brama_0(" in zrodlo
    assert "stawka_konta_docelowego=False" in zrodlo
