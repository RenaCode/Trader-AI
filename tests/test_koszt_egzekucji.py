"""Pętla decyzyjna i trener MUSZĄ liczyć ten sam koszt.

DLACZEGO OSOBNY PLIK

Ten rozjazd zdarzył się dwa razy. Za pierwszym razem stała `FEE = 0,055%` —
stawka taker kontraktów wieczystych — siedziała w symulacji portfelowej, choć
handlujemy spotem; naprawione 2026-08-31 (renacode-infra: docs/projekty/trader/03-wynik-walk-forward.md). Poprawka poszła do
`koszty.py` i `portfolio.py`, a `loop.py` — czyli to, co faktycznie prowadzi
rachunek papierowy — zostało ze starą stałą na kolejny tydzień.

Nie objawiało się to awarią. Krzywa kapitału po prostu rosła szybciej, niż
powinna, i wyglądała jak wynik.
"""
import re
from pathlib import Path

import pytest

from services.engine.koszty import CENNIK, POSLIZG_MAKER, POSLIZG_TAKER, koszty, runda

# Import WEWNATRZ testow, nie na gorze modulu: kanarek na stala prowizji ma
# padac na asercji takze wtedy, gdy `stawki` jeszcze nie istnieje. Import na
# gorze wywracalby caly plik na zbieraniu testow i ukrywal wlasciwy powod.


@pytest.mark.parametrize("kind", sorted(CENNIK))
@pytest.mark.parametrize("limit", [True, False])
def test_stawka_petli_zgadza_sie_z_cennikiem(kind, limit):
    from services.engine.loop import stawki

    fee, slip = stawki(kind, limit)
    k = koszty(kind)
    assert float(fee) == (k.maker if limit else k.taker)
    assert float(slip) == (POSLIZG_MAKER if limit else POSLIZG_TAKER)


@pytest.mark.parametrize("kind", sorted(CENNIK))
@pytest.mark.parametrize("limit", [True, False])
def test_dwie_nogi_petli_daja_runde_trenera(kind, limit):
    """Trener raportuje koszt RUNDY, pętla nalicza koszt JEDNEJ NOGI.

    Jeśli te dwie liczby przestaną się zgadzać, trener będzie kalibrował pod
    inny koszt, niż płaci rachunek papierowy — a różnica jest niewidoczna,
    bo obie strony wyglądają poprawnie z osobna.
    """
    from services.engine.loop import stawki

    fee, slip = stawki(kind, limit)
    assert 2 * (float(fee) + float(slip)) == pytest.approx(runda(kind, limit), rel=1e-12)


def test_petla_nie_ma_wlasnej_stalej_prowizji():
    """Kanarek na powrót problemu.

    Stała prowizji w `loop.py` jest dokładnie tym, co pozwoliło rozjazdowi
    przetrwać tydzień: koszty.py mówił jedno, produkcja liczyła drugie.
    """
    zrodlo = Path("services/engine/loop.py").read_text()
    kod = "\n".join(l for l in zrodlo.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"^\s*FEE_RATE\s*=", kod, re.M), \
        "loop.py znowu ma własną stawkę prowizji zamiast czytać z koszty.py"
    assert not re.search(r"^\s*SLIPPAGE\s*=", kod, re.M), \
        "loop.py znowu ma własny poślizg zamiast czytać z koszty.py"


def test_cennik_unijny_jest_drozszy_od_globalnego_na_obu_stronach():
    """Kanarek na sens całej zmiany: gdyby stawki były równe, test wyżej
    przechodziłby także dla złego cennika.

    Od 2026-10-05 podmiot unijny nalicza 0,250% także makerowi - limit nie
    daje już rabatu prowizji względem zlecenia rynkowego."""
    assert koszty("spot_eu").taker > koszty("spot").taker
    assert koszty("spot_eu").maker > koszty("spot").maker
    assert koszty("spot_eu").maker == koszty("spot_eu").taker
