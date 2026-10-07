"""Dozór porównuje przebiegi liczone TĄ SAMĄ miarą - nie definicję z definicją.

Od 2026-10-02 trener liczy odsetek dodatnich okien i medianę Sharpe'a PO
dopłacie z karty kosztu, a wcześniej liczył je brutto. Pierwszy przebieg po
wdrożeniu porównany z ostatnim sprzed niego zgłosiłby „mediana Sharpe zmieniła
znak" i odsetek spoza przedziału - zmianę DEFINICJI, nie rynku. Tor 1h na 1400
dniach schodzi przy tym na 36/89 = 40,4%, o włos od progu `degraded`.

Miarę wiersza dziennika rozpoznaje metryka okna `doplata_pb`, którą trener
zapisuje od tej zmiany - bez migracji.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.engine.dozor import (
    MIARA_BRUTTO, MIARA_PO_KARCIE, SQL_STAN_NAUKI, miara_przebiegu,
    wybierz_poprzedni, zmiany,
)

KORZEN = Path(__file__).resolve().parents[1]
T = datetime(2026, 10, 3, 2, 40, tzinfo=timezone.utc)


def _wiersz(dni_temu: int, *, doplata_pb, mediana: float, dodatnie: int,
            okna: int = 89, status: str = "learning") -> dict:
    return {"status": status, "windows_total": okna, "windows_positive": dodatnie,
            "positive_ci_low": (dodatnie / okna) - 0.1,
            "positive_ci_high": (dodatnie / okna) + 0.1,
            "median_oos_sharpe": mediana, "max_psi": 0.3, "trials_run": 1,
            "run_at": T - timedelta(days=dni_temu), "doplata_pb": doplata_pb}


def test_brak_metryki_i_zero_to_BRUTTO_dodatnia_doplata_to_PO_KARCIE():
    assert miara_przebiegu({"doplata_pb": None}) == MIARA_BRUTTO
    assert miara_przebiegu({}) == MIARA_BRUTTO
    # Zero: liczby identyczne z brutto - ta sama miara, nie nowa.
    assert miara_przebiegu({"doplata_pb": 0.0}) == MIARA_BRUTTO
    assert miara_przebiegu({"doplata_pb": 5.85}) == MIARA_PO_KARCIE


def test_pierwszy_przebieg_po_wdrozeniu_NIE_zglasza_zmiany_znaku():
    """SEDNO. Stary wiersz brutto z dodatnią medianą, nowy po dopłacie z ujemną.
    Bez rozpoznania miary to „mediana zmieniła znak" + odsetek poza przedziałem."""
    wiersze = [_wiersz(0, doplata_pb=5.85, mediana=-0.012, dodatnie=36),
               _wiersz(1, doplata_pb=None, mediana=+0.031, dodatnie=45),
               _wiersz(2, doplata_pb=None, mediana=+0.029, dodatnie=44)]
    poprzedni, pominiete = wybierz_poprzedni(wiersze)
    assert poprzedni is None and pominiete == 2
    powody = zmiany(poprzedni, wiersze[0], pominiete)
    assert len(powody) == 1
    assert "zmienila znak" not in powody[0]
    assert "inną definicją" in powody[0] and "POMINIĘTE" in powody[0]
    # Kanarek: bez wyboru po mierze ta sama para dawała fałszywy alarm.
    stare = zmiany(wiersze[1], wiersze[0])
    assert any("zmienila znak" in p for p in stare)


def test_drugi_przebieg_po_wdrozeniu_porownuje_sie_z_pierwszym_po_wdrozeniu():
    wiersze = [_wiersz(0, doplata_pb=5.90, mediana=-0.010, dodatnie=36),
               _wiersz(1, doplata_pb=5.85, mediana=-0.012, dodatnie=36),
               _wiersz(2, doplata_pb=None, mediana=+0.031, dodatnie=45)]
    poprzedni, pominiete = wybierz_poprzedni(wiersze)
    assert poprzedni is wiersze[1] and pominiete == 0
    assert zmiany(poprzedni, wiersze[0], pominiete) == []


def test_reczny_przebieg_brutto_miedzy_dwoma_po_karcie_jest_przeskakiwany():
    wiersze = [_wiersz(0, doplata_pb=5.9, mediana=-0.01, dodatnie=36),
               _wiersz(0, doplata_pb=0.0, mediana=+0.03, dodatnie=45),
               _wiersz(1, doplata_pb=5.8, mediana=-0.01, dodatnie=37)]
    poprzedni, pominiete = wybierz_poprzedni(wiersze)
    assert poprzedni is wiersze[2] and pominiete == 1
    assert zmiany(poprzedni, wiersze[0], pominiete) == []


def test_pierwszy_przebieg_w_historii_zostaje_pierwszym():
    w = [_wiersz(0, doplata_pb=5.85, mediana=0.01, dodatnie=50)]
    assert wybierz_poprzedni(w) == (None, 0)
    assert zmiany(None, w[0], 0) == ["pierwszy zapisany przebieg — nie ma z czym porownac"]


def test_dozor_i_trener_uzywaja_TEJ_SAMEJ_nazwy_metryki():
    """Rozjazd nazwy dałby po cichu „brutto" przy każdym przebiegu - i dozór
    wróciłby do porównywania wszystkiego ze wszystkim."""
    trener = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    assert "agg.update(znacznik_miary(karta, egzekucja))" in trener
    assert '"doplata_pb": karta.doplata_poza_symulacja * 1e4' in trener
    assert '"spread_niezmierzony"' in trener
    assert "m.metric = 'spread_niezmierzony'" in SQL_STAN_NAUKI
    assert "m.metric = 'doplata_pb'" in SQL_STAN_NAUKI
    dozor = (KORZEN / "services/engine/dozor.py").read_text(encoding="utf-8")
    assert "wybierz_poprzedni(wiersze)" in dozor
    assert "ORDER BY run_at DESC LIMIT 2" not in dozor


# ------------------------------------------------- zmiana dopłaty w tej samej klasie

def test_zmiana_doplaty_ponad_pol_pb_jest_NAZWANA_a_porownanie_idzie_dalej():
    """Ta sama klasa miary (oba po karcie), ale dopłata 5,85 -> 7,10 pb.
    Zdanie informacyjne MUSI być, a porównanie znaku mediany - nadal."""
    wiersze = [_wiersz(0, doplata_pb=7.10, mediana=-0.01, dodatnie=36),
               _wiersz(1, doplata_pb=5.85, mediana=+0.02, dodatnie=37)]
    poprzedni, pominiete = wybierz_poprzedni(wiersze)
    assert poprzedni is wiersze[1]
    powody = zmiany(poprzedni, wiersze[0], pominiete)
    assert any("dopłata zmieniła się z 5.85 na 7.10 pb" in p for p in powody), powody
    assert any("zmienila znak" in p for p in powody), (
        "zdanie o dopłacie zastąpiło porównanie zamiast mu towarzyszyć")


def test_wahanie_doplaty_ponizej_progu_to_cisza():
    wiersze = [_wiersz(0, doplata_pb=6.20, mediana=0.02, dodatnie=36),
               _wiersz(1, doplata_pb=5.85, mediana=0.02, dodatnie=36)]
    poprzedni, pominiete = wybierz_poprzedni(wiersze)
    assert zmiany(poprzedni, wiersze[0], pominiete) == []


def test_SAMA_zmiana_doplaty_nie_wywoluje_powiadomienia():
    """Kontekst, nie powód: 5,85 -> 7,10 pb przy niezmienionych miarach to cisza."""
    wiersze = [_wiersz(0, doplata_pb=7.10, mediana=0.02, dodatnie=36),
               _wiersz(1, doplata_pb=5.85, mediana=0.02, dodatnie=36)]
    poprzedni, pominiete = wybierz_poprzedni(wiersze)
    assert zmiany(poprzedni, wiersze[0], pominiete) == []
