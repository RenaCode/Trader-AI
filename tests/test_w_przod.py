"""H-4: test w przód „czy trening się uczy" — pary okien i werdykt.

Kryterium jest zapisane w docs/hipotezy.md PRZED pomiarem; ten plik pilnuje,
że kod liczy dokładnie to, co tam stoi: okna rozłączne, wersje zamrożone przed
oknem, n = liczba okien, werdykt z pierwszych N_OKIEN i ani jednego więcej.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.engine.w_przod import (
    N_OKIEN, POCZATEK, PROG_LEPSZYCH, miara_okna, pary_okien, poczatek_tygodnia,
    werdykt)

UTC = timezone.utc


def _niedziela(d: int) -> datetime:
    """Planowy trener: niedziela 04:40 czasu warszawskiego = 02:40 UTC."""
    return datetime(2026, 10, d, 2, 40, tzinfo=UTC)


def test_tydzien_zaczyna_sie_w_poniedzialek_o_polnocy_UTC():
    assert poczatek_tygodnia(datetime(2026, 10, 11, 2, 40, tzinfo=UTC)) == \
        datetime(2026, 10, 5, tzinfo=UTC)
    assert POCZATEK.weekday() == 0


def test_wersja_oceniana_dopiero_w_tygodniu_PO_zapisaniu():
    wersje = [(40, _niedziela(4)), (41, _niedziela(11)), (42, _niedziela(18))]
    pary = pary_okien(wersje, teraz=datetime(2026, 10, 27, tzinfo=UTC))
    assert [(p.od.day, p.nowa, p.poprzednia) for p in pary] == [(12, 41, 40), (19, 42, 41)]
    for p in pary:
        assert p.do - p.od == timedelta(days=7)
        zapis_nowej = dict(wersje)[p.nowa]
        assert zapis_nowej < p.od, "prognoza wersji dotknęła okna sprzed jej zapisu"


def test_okna_sa_rozlaczne_a_okno_otwarte_nie_liczy_sie():
    wersje = [(v, _niedziela(4) + timedelta(days=7 * i)) for i, v in enumerate(range(40, 46))]
    pary = pary_okien(wersje, teraz=datetime(2026, 11, 4, tzinfo=UTC))
    for a, b in zip(pary, pary[1:]):
        assert a.do <= b.od
    assert all(p.do <= datetime(2026, 11, 4, tzinfo=UTC) for p in pary)


def test_reczna_powtorka_w_tym_samym_tygodniu_NIE_daje_drugiej_pary():
    """04.10: planowy przebieg i ręczne powtórzenie kilka godzin później. Dwie
    wersje przed jednym oknem to jedna para (najnowsza), nie dwie próby."""
    wersje = [(39, _niedziela(4) - timedelta(days=7)), (40, _niedziela(4)),
              (41, _niedziela(4) + timedelta(hours=6)), (42, _niedziela(11))]
    pary = pary_okien(wersje, teraz=datetime(2026, 10, 20, tzinfo=UTC))
    assert [(p.nowa, p.poprzednia) for p in pary] == [(42, 41)]


def test_tydzien_bez_nowej_wersji_nie_daje_pary():
    wersje = [(40, _niedziela(4)), (41, _niedziela(18))]
    pary = pary_okien(wersje, teraz=datetime(2026, 10, 27, tzinfo=UTC))
    assert [(p.od.day, p.nowa, p.poprzednia) for p in pary] == [(19, 41, 40)]


def test_okna_sprzed_zapisania_hipotezy_sie_nie_licza():
    wersje = [(30, datetime(2026, 9, 6, tzinfo=UTC)), (31, datetime(2026, 9, 13, tzinfo=UTC))]
    assert pary_okien(wersje, teraz=datetime(2026, 10, 10, tzinfo=UTC)) == []


def test_werdykt_dopiero_po_N_rozstrzygnietych_oknach():
    stan, opis = werdykt([0.1] * (N_OKIEN - 1) + [0.0] * 5)
    assert stan == "w toku" and "remisów 5" in opis


def test_kryterium_progowe_i_kanarek():
    assert werdykt([0.1] * PROG_LEPSZYCH + [-0.1] * (N_OKIEN - PROG_LEPSZYCH))[0] == "UCZY SIĘ"
    assert werdykt([0.1] * (PROG_LEPSZYCH - 1)
                   + [-0.1] * (N_OKIEN - PROG_LEPSZYCH + 1))[0] == "NIE WYKAZANO"


def test_okna_po_N_nie_zmieniaja_werdyktu():
    """Zatrzymanie warunkowe: dokładanie okien, aż wynik „wyjdzie", to nie test."""
    pierwsze = [0.1] * (PROG_LEPSZYCH - 1) + [-0.1] * (N_OKIEN - PROG_LEPSZYCH + 1)
    assert werdykt(pierwsze + [0.1] * 50)[0] == "NIE WYKAZANO"


def test_miara_okna_mowi_ze_jej_nie_ma_zamiast_zwracac_zero():
    with pytest.raises(NotImplementedError, match="H-4"):
        miara_okna(None, {}, None)
