"""Prowizja i model wypelnien sa czescia klasy miary (2026-10-07).

05.10 stawka maker `spot_eu` wzrosla z 0,10% na 0,25% (runda 0,2% -> 0,5%)
przy tych samych `kind` i `limit` w `params`; 07.10 trener przeszedl na model
pesymistyczny z opoznieniem 20 min (H-5). Przebieg 11.10 porownany z 05.10
zglosilby regres, ktory jest zmiana kosztu, nie rynku.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.engine.dozor import (
    SQL_STAN_NAUKI, STAN_NAUKI_WSTECZ, klasa_miary, miara_przebiegu,
    wybierz_poprzedni, zmiany,
)
from services.engine.koszty import karta_rundy
from services.engine.portfolio import Egzekucja
from services.engine.train import znacznik_miary

T = datetime(2026, 10, 11, 2, 40, tzinfo=timezone.utc)


def _wiersz(dni_temu: int, *, mediana: float, dodatnie: int, okna: int = 89,
            **koszt) -> dict:
    w = {"status": "learning", "windows_total": okna, "windows_positive": dodatnie,
         "positive_ci_low": dodatnie / okna - 0.05,
         "positive_ci_high": dodatnie / okna + 0.05,
         "median_oos_sharpe": mediana, "max_psi": 0.3, "trials_run": 1,
         "run_at": T - timedelta(days=dni_temu), "doplata_pb": 2.5,
         "spread_niezmierzony": 0.0}
    w.update(koszt)
    return w


def test_rozna_prowizja_NIE_jest_porownywana():
    """SEDNO: 11.10 (runda 50 pb) i 05.10 (runda 20 pb), poza tym identyczne
    klasy - bez prowizji w klasie byloby „mediana zmienila znak"."""
    nowy = _wiersz(0, mediana=-0.02, dodatnie=30, prowizja_pb=50.0)
    stary = _wiersz(6, mediana=+0.03, dodatnie=45, prowizja_pb=20.0)
    assert klasa_miary(nowy) != klasa_miary(stary)
    poprzedni, pominiete = wybierz_poprzedni([nowy, stary])
    assert poprzedni is None and pominiete == 1
    powody = zmiany(poprzedni, nowy, pominiete, bezposredni=stary)
    assert not any("zmienila znak" in p for p in powody)
    assert not any("poza przedzialem" in p for p in powody)
    # Kanarek: bez rozroznienia ta sama para dawala falszywy regres.
    assert any("zmienila znak" in p for p in zmiany(stary, nowy))


def test_pesymistyczna_i_opoznienie_zmieniaja_klase():
    baza = _wiersz(0, mediana=0.01, dodatnie=40, prowizja_pb=50.0)
    pes = dict(baza, egzekucja_pesymistyczna=1, opoznienie_min=20)
    assert klasa_miary(pes) != klasa_miary(baza)
    assert klasa_miary(dict(pes, opoznienie_min=30)) != klasa_miary(pes)
    assert "pesymistyczne" in miara_przebiegu(pes) and "20 min" in miara_przebiegu(pes)


def test_przebieg_11_10_porownuje_sie_z_przebiegiem_tej_samej_klasy():
    nowy = _wiersz(0, mediana=0.01, dodatnie=40, prowizja_pb=50.0,
                   egzekucja_pesymistyczna=1, opoznienie_min=20)
    reczny_05_10 = _wiersz(6, mediana=0.05, dodatnie=55, prowizja_pb=20.0)
    zeszly = _wiersz(7, mediana=0.012, dodatnie=41, prowizja_pb=50.0,
                     egzekucja_pesymistyczna=1, opoznienie_min=20)
    poprzedni, pominiete = wybierz_poprzedni([nowy, reczny_05_10, zeszly])
    assert poprzedni is zeszly and pominiete == 1


def test_wiersze_bez_metryk_porownuja_sie_jak_dotad():
    """Przebiegi sprzed metryki (NULL wszedzie) - ta sama klasa co dawniej."""
    a = _wiersz(0, mediana=0.01, dodatnie=40)
    b = _wiersz(7, mediana=0.01, dodatnie=40, egzekucja_pesymistyczna=0,
                opoznienie_min=None)
    assert klasa_miary(a) == klasa_miary(b)
    assert wybierz_poprzedni([a, b]) == (b, 0)


def test_trener_zapisuje_prowizje_i_model_wypelnien():
    karta = karta_rundy("spot_eu", maker=True)
    z = znacznik_miary(karta, Egzekucja(limit=True, pesymistyczna=True,
                                        opoznienie_min=20))
    assert z["prowizja_pb"] == 50.0
    assert z["egzekucja_pesymistyczna"] == 1.0 and z["opoznienie_min"] == 20.0
    z0 = znacznik_miary(karta_rundy("spot", maker=True), Egzekucja(limit=True))
    assert z0["prowizja_pb"] == 20.0
    assert z0["egzekucja_pesymistyczna"] == 0.0 and z0["opoznienie_min"] == 0.0


def test_dozor_czyta_te_same_nazwy_metryk():
    for nazwa in ("prowizja_pb", "egzekucja_pesymistyczna", "opoznienie_min"):
        assert f"m.metric = '{nazwa}'" in SQL_STAN_NAUKI
    # Dla przebiegow miedzy H-5 a ta zmiana model wypelnien z `params`.
    assert "mv.params->>'egzekucja_pesymistyczna'" in SQL_STAN_NAUKI
    assert "mv.params->>'opoznienie_min'" in SQL_STAN_NAUKI


def test_okno_wstecz_dla_treningu_tygodniowego():
    """Trening tygodniowy: 14 wierszy to ponad 3 miesiace. 6 to 6 tygodni bez
    przebiegow recznych - poprzednik nie starszy niz ~poltora miesiaca."""
    assert 4 <= STAN_NAUKI_WSTECZ <= 8
