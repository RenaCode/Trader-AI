"""Tabela IC per sygnal w logu trenera (A-3) - diagnostyka, nie decyzja.

Ze wynik treningu sie od niej nie zmienia, pilnuje zrzut
`tests/fixtures/trener_rownowaznosc.json` (przegenerowany 2026-10-18 po
sprawdzeniu, ze bez tabeli wydruk i JSON sa identyczne). Tu: ze liczy to,
co mowi, i ze jest ta sama definicja obserwacji co wagi.
"""
from __future__ import annotations

import random

from services.engine import train as tr
from services.engine.horizons import get as get_horizon
from services.engine.signals import SIGNALS, SignalOutput

H4 = get_horizon("4h")


def _okno(n: int = 200, ziarno: int = 7):
    """`trend_follow` przewiduje zwrot H swiec naprzod, `momentum_multi` go
    odwraca, `range_extreme` to szum. Polowa swiec w `trend_up`, polowa
    w `range`."""
    rng = random.Random(ziarno)
    zwroty = [rng.gauss(0, 0.01) for _ in range(n + 10)]
    ceny = [100.0]
    for z in zwroty:
        ceny.append(ceny[-1] * (1 + z))
    h = H4.ic_horizon_bars
    panel, cache = [], []
    for i in range(n):
        przyszly = ceny[i + 1 + h] / ceny[i + 1] - 1
        panel.append({"t": i, "price": ceny[i], "f": {}})
        cache.append(("trend_up" if i % 2 else "range", {
            "trend_follow": SignalOutput(przyszly * 50, 0.7, {}),
            "momentum_multi": SignalOutput(-przyszly * 50, 0.7, {}),
            "range_extreme": SignalOutput(rng.uniform(-1, 1), 0.7, {}),
        }))
    # ostatnie swiece panelu potrzebuja cen do etykiety
    for j in range(n, n + h + 1):
        panel.append({"t": j, "price": ceny[j], "f": {}})
        cache.append(("range", {}))
    return {"AAA": panel}, {"AAA": cache}


def test_ic_ma_znak_i_grupy():
    panels, cache = _okno()
    ic = tr.ic_sygnalow(panels, cache, H4.ic_horizon_bars)
    assert ic[tr.RAZEM_IC]["trend_follow"] > 0.9
    assert ic[tr.RAZEM_IC]["momentum_multi"] < -0.9
    assert abs(ic[tr.RAZEM_IC]["range_extreme"]) < 0.3
    assert set(ic) == {tr.RAZEM_IC, "trend_up", "range"}
    # sygnal bez obserwacji nie dostaje zera, ktore udawaloby pomiar
    assert "news_shock" not in ic[tr.RAZEM_IC]


def test_ta_sama_definicja_obserwacji_co_wagi():
    """Znak wag z IC (`ic_weights`) zgadza sie ze znakiem IC z tabeli."""
    panels, cache = _okno()
    ic = tr.ic_sygnalow(panels, cache, H4.ic_horizon_bars)[tr.RAZEM_IC]
    wagi = tr.ic_weights(panels, cache, set(), H4.ic_horizon_bars)
    for regime in ("trend_up", "range"):
        assert wagi[regime]["trend_follow"] > 0 and ic["trend_follow"] > 0
        assert wagi[regime]["momentum_multi"] < 0 and ic["momentum_multi"] < 0


def test_opis_tabeli():
    okna = [tr.ic_sygnalow(*_okno(ziarno=z), H4.ic_horizon_bars) for z in range(3)]
    linie = tr.opis_ic_sygnalow(okna, H4)
    assert linie[0].startswith("IC SYGNAŁÓW NA OKNACH TESTOWYCH")
    assert "nie wpływa na wagi" in linie[0]
    # naglowek + wiersz na kazdy sygnal
    assert len(linie) == 2 + len(SIGNALS)
    wiersz = next(w for w in linie if w.strip().startswith("trend_follow"))
    assert "3/3" in wiersz
    wiersz = next(w for w in linie if w.strip().startswith("news_shock"))
    assert "—" in wiersz
    assert tr.opis_ic_sygnalow([], H4) == []


def test_filtr_swiecy_zaweza_obserwacje():
    """Recenzja #39, pkt 6: na rynku z sesja IC tylko ze swiec, z ktorych
    symulacja moze wejsc. Filtr odrzucajacy wszystko nie zostawia IC."""
    panels, cache = _okno()
    assert tr.ic_sygnalow(panels, cache, H4.ic_horizon_bars,
                          filtr_swiecy=lambda r: False) == {}
    polowa = tr.ic_sygnalow(panels, cache, H4.ic_horizon_bars,
                            filtr_swiecy=lambda r: r["t"] % 2 == 1)
    # tylko swiece nieparzyste, czyli wylacznie reżim `trend_up`
    assert set(polowa) == {tr.RAZEM_IC, "trend_up"}


def test_swieca_wejscia_z_kalendarza_sesji():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from services.engine.portfolio import Egzekucja
    h1 = get_horizon("1h")
    egz = Egzekucja(limit=True, opoznienie_min=20, pesymistyczna=True)
    assert tr.swieca_wejscia(SimpleNamespace(kind="spot_eu", h=h1, egzekucja=egz)) is None
    f = tr.swieca_wejscia(SimpleNamespace(kind="stock", h=h1, egzekucja=egz))
    dzien = datetime(2026, 7, 15, tzinfo=timezone.utc)
    godziny = [g for g in range(24) if f({"t": dzien.replace(hour=g)})]
    assert godziny == [13, 14, 15, 16, 17, 18]
