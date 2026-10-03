"""`--znacznik` nie moze stanac na szumie arytmetyki double.

Do 2026-10-03 `rozjazd_cech` i `rozjazd_przekroju` porownywaly zapisana wartosc
cechy z przeliczona TERAZ przez `!=`. Ta sama cecha z tych samych swiec potrafi
roznic sie w ostatnich bitach (kolejnosc sumowania, inny poczatek okna), a jedna
taka roznica blokowala znacznik przy poprawnych danych.
"""
from __future__ import annotations

from services.ingest.korekta_podzialow import policz_rozjazd, rozne


def test_szum_ostatnich_bitow_nie_jest_rozjazdem():
    a = 0.1 + 0.2                      # 0.30000000000000004
    assert a != 0.3
    assert not rozne(a, 0.3)
    przed = {("rsi", 1): 0.3, ("ret", 1): 0.0, ("atr", 1): 1234.5678}
    po = {("rsi", 1): a, ("ret", 1): 1e-17, ("atr", 1): 1234.5678 * (1 + 1e-13)}
    assert policz_rozjazd(przed, po) == (0, 0)


def test_skok_po_podziale_i_brak_dalej_lapane():
    przed = {("close_rel", 1): 1.0, ("ret", 1): 0.01}
    po = {("close_rel", 1): 0.1, ("ret", 1): 0.0101, ("nowa", 1): 5.0}
    assert policz_rozjazd(przed, po) == (2, 1)
    assert rozne(None, 0.0) and not rozne(None, None)
