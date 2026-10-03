"""Przebieg bez poprzednika tej samej miary NIE ucisza statusu, okien ani dryfu.

Do 2026-10-03 `dozor.zmiany` przy `poprzedni is None and pominiete_inna_miara`
oddawal JEDNO zdanie i `return`. Status validated → degraded (bramkuje handel),
ubytek polowy okien i dryf ponad progiem przechodzily bez slowa - a `windows_total`
i `max_psi` od definicji miary w ogole nie zaleza. Stan przejsciowy nie jest
jednorazowy: `doplata_pb = 0` wychodzi przy kazdej luce w `book_snapshot`
(`zmierz_spread` -> `bps=None`), wiec przeskok 0 → 5.85 → 0 wchodzil w te galaz
za kazdym razem.

Druga polowa (R-b): wartosc znacznika miary zapisywanego przez trenera i
rozroznienie „karta bez pomiaru spreadu" od brutto.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.engine.dozor import (
    MIARA_BEZ_SPREADU, MIARA_BRUTTO, MIARA_PO_KARCIE, klasa_miary,
    miara_przebiegu, ocen_stan_nauki,
)
from services.engine.koszty import karta_rundy
from services.engine.train import znacznik_miary

T = datetime(2026, 10, 3, 2, 40, tzinfo=timezone.utc)


def _w(dni, *, doplata_pb, status="validated", psi=0.1, okna=40, dodatnie=None,
       mediana=0.02, bez_spreadu=None):
    dodatnie = okna // 2 if dodatnie is None else dodatnie
    return {"status": status, "windows_total": okna, "windows_positive": dodatnie,
            "positive_ci_low": dodatnie / okna - 0.1,
            "positive_ci_high": dodatnie / okna + 0.1,
            "median_oos_sharpe": mediana, "max_psi": psi, "trials_run": 1,
            "run_at": T - timedelta(days=dni), "doplata_pb": doplata_pb,
            "spread_niezmierzony": bez_spreadu}


def test_zmiana_miary_nie_ucisza_statusu_okien_i_dryfu():
    """Przypadek z raportu audytu, wprost."""
    wiersze = [_w(0, doplata_pb=5.85, status="degraded", psi=1.4, okna=20),
               _w(1, doplata_pb=None, status="validated", psi=0.1, okna=40)]
    powody, pominiete = ocen_stan_nauki(wiersze)
    assert pominiete == 1
    tekst = " | ".join(powody)
    assert "validated → degraded" in tekst
    assert "zmiana definicji miary" in tekst
    assert "ubylo okien" in tekst
    assert "dryf cech" in tekst
    # A liczby zalezne od miary - nadal pominiete.
    assert "zmienila znak" not in tekst and "poza przedzialem" not in tekst
    assert any("POMINIĘTE" in p for p in powody)


def test_przeskok_0_585_0_status_z_bezposrednim_liczby_z_ta_sama_miara():
    """0 (luka w spreadzie) → 5.85 → 0. Bieżący porównuje ODSETEK i ZNAK z
    przebiegiem sprzed dwóch dni (ta sama klasa), a STATUS z wczorajszym."""
    wiersze = [_w(0, doplata_pb=0.0, bez_spreadu=1.0, status="validated",
                  mediana=+0.02, dodatnie=20),
               _w(1, doplata_pb=5.85, bez_spreadu=0.0, status="degraded",
                  mediana=-0.01, dodatnie=14),
               _w(2, doplata_pb=0.0, bez_spreadu=1.0, status="validated",
                  mediana=+0.02, dodatnie=20)]
    powody, pominiete = ocen_stan_nauki(wiersze)
    assert pominiete == 1
    tekst = " | ".join(powody)
    assert "degraded → validated" in tekst
    assert MIARA_BEZ_SPREADU in tekst and MIARA_PO_KARCIE in tekst
    # znak mediany i odsetek liczone z wierszem [2], z którym się zgadzają
    assert "zmienila znak" not in tekst and "poza przedzialem" not in tekst


def test_przeskok_na_karte_po_dwoch_lukach_mowi_o_statusie():
    wiersze = [_w(0, doplata_pb=5.85, bez_spreadu=0.0, status="degraded"),
               _w(1, doplata_pb=0.0, bez_spreadu=1.0, status="validated"),
               _w(2, doplata_pb=0.0, bez_spreadu=1.0, status="validated")]
    powody, pominiete = ocen_stan_nauki(wiersze)
    assert pominiete == 2
    assert any("validated → degraded" in p for p in powody)


def test_bez_zmian_po_przeskoku_miary_zostaje_tylko_zdanie_o_mierze():
    """Kanarek w drugą stronę: poprawka nie może zamienić każdego przeskoku
    w lawinę powodów - status, okna i dryf bez zmian nie dają nic ponad zdanie."""
    wiersze = [_w(0, doplata_pb=5.85), _w(1, doplata_pb=None)]
    powody, _ = ocen_stan_nauki(wiersze)
    assert len(powody) == 1 and "POMINIĘTE" in powody[0]


# ------------------------------------------------------------- R-b: znacznik
def test_znacznik_niesie_doplate_z_karty_co_do_wartosci():
    karta = karta_rundy("spot_eu", True, spread_bps=5.85)
    z = znacznik_miary(karta)
    assert z["doplata_pb"] == pytest.approx(karta.doplata_poza_symulacja * 1e4)
    assert z["doplata_pb"] == pytest.approx(5.85)
    assert z["spread_niezmierzony"] == 0.0
    assert miara_przebiegu(z) == MIARA_PO_KARCIE


def test_brak_pomiaru_spreadu_to_zero_ze_znacznikiem_nie_brutto():
    karta = karta_rundy("spot_eu", True, spread_bps=None)
    z = znacznik_miary(karta)
    assert z == {"doplata_pb": 0.0, "spread_niezmierzony": 1.0}
    assert miara_przebiegu(z) == MIARA_BEZ_SPREADU
    # Liczbowo to samo co brutto - wolno porównywać, nazwa jest inna.
    assert klasa_miary(z) == MIARA_BRUTTO


def test_przebieg_sprzed_znacznika_dalej_brutto():
    assert miara_przebiegu({"doplata_pb": 0.0}) == MIARA_BRUTTO
    assert miara_przebiegu({"doplata_pb": None, "spread_niezmierzony": 1.0}) == MIARA_BRUTTO
