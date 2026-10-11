"""Kandydatki Bramy 0 (docs/brama-0-gieldy.md, 2026-10-11) liczone z tablicy, nie
przepisane.

Trzy rzeczy, ktore ten plik pilnuje:

  1. Tablica `KANDYDACI_BRAMY_0` i dokument mowia o tych samych gieldach - wpis
     bez wiersza w dokumencie (albo odwrotnie) to liczba bez pochodzenia.
  2. Zadna kandydatka nie wychodzi OTWARTA z samych deklaracji: stawka nie jest
     z konta docelowego, spread nie jest z wlasnej ksiegi, niewypelnienie jest
     niezmierzone. Gdyby kiedys wyszla, to znaczy, ze ktos obszedl `brama_0`,
     a nie ze gielda stanial.
  3. Tablica kandydatek NIE jest cennikiem silnika: `CENNIK` ma te same klucze
     co przed nia, a `koszty(nazwa_kandydatki)` oddaje domyslny `spot`.

Zero wywolan sieci: spready w tablicy to zapis odczytu z 2026-10-11.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.engine.koszty import (
    CENNIK, DOMYSLNE, NIEROZSTRZYGNIETA, OTWARTA, PROG_BRAMY_0_PB, ZAMKNIETA,
    KANDYDACI_BRAMY_0, brama_0_kandydatki, koszty, zapas_na_niewypelnienie_pb,
)

DOKUMENT = Path(__file__).resolve().parents[1] / "docs" / "brama-0-gieldy.md"
# Koszt niewypelnienia z symulacji pesymistycznej na swiecach globalnych
# (log trenera 2026-10-11, tor 4h: "niewypelnienie: 0.400% (pomiar)").
# Liczba z INNEGO rynku - sluzy tu jako scenariusz, nie jako pomiar.
NIEWYPELNIENIE_SYMULACJI_PB = 40.0

KANDYDATKI = {k.nazwa: k for k in KANDYDACI_BRAMY_0}


def test_nazwy_kandydatek_sa_unikalne():
    assert len(KANDYDATKI) == len(KANDYDACI_BRAMY_0)


@pytest.mark.parametrize("k", KANDYDACI_BRAMY_0, ids=lambda k: k.nazwa)
def test_dokument_wymienia_kazda_kandydatke(k):
    tresc = DOKUMENT.read_text(encoding="utf-8")
    assert f"`{k.nazwa}`" in tresc, (
        f"docs/brama-0-gieldy.md nie wymienia `{k.nazwa}` - liczba w tablicy "
        f"bez wiersza w dokumencie nie ma pochodzenia")


@pytest.mark.parametrize("k", KANDYDACI_BRAMY_0, ids=lambda k: k.nazwa)
def test_zadna_kandydatka_nie_otwiera_bramy_z_deklaracji(k):
    b = brama_0_kandydatki(k)
    assert b.stan != OTWARTA
    # Braki sa wypisane z nazwy - nawet gdy werdykt juz zapadl na prowizji.
    tekst = "\n".join(b.powody)
    assert "niewypelnienia" in tekst
    if k.maker is not None:
        assert "konta docelowego" in tekst
    if k.spread_pb is not None:
        assert "obcej ksiegi" in tekst


@pytest.mark.parametrize(
    "k", [k for k in KANDYDACI_BRAMY_0 if k.rynek == "spot" and k.maker is not None],
    ids=lambda k: k.nazwa)
def test_kazdy_spot_w_ue_jest_zamkniety_na_samej_prowizji_i_spreadzie(k):
    """Najtanszy spot (crypto.com 0,075%) to 15 pb samej prowizji - ponad
    prog, zanim doliczy sie spread. To jest wynik analizy z 2026-10-11, a nie
    zalozenie: gdyby ktoras gielda spot zeszla ponizej, ten test ma paść."""
    b = brama_0_kandydatki(k)
    assert b.stan == ZAMKNIETA, (k.nazwa, b.runda_pb)
    assert 2 * k.maker * 1e4 > PROG_BRAMY_0_PB or b.runda_pb > PROG_BRAMY_0_PB
    zapas = zapas_na_niewypelnienie_pb(k)
    assert zapas is None or zapas < 0


def test_bybit_eu_w_tablicy_to_ta_sama_stawka_co_w_cenniku_silnika():
    """Jedyna stawka zmierzona z konta. Rozjazd miedzy tablica a `CENNIK`
    znaczylby, ze jedna z nich jest nieaktualna - czujka `mozliwosci.py
    --stawki` pilnuje `CENNIK`, ten test pilnuje tablicy."""
    k = KANDYDATKI["bybit_eu"]
    assert (k.maker, k.taker) == (koszty("spot_eu").maker, koszty("spot_eu").taker)
    assert brama_0_kandydatki(k).skladniki["prowizja"] == pytest.approx(50.0)


@pytest.mark.parametrize("nazwa", ["kraken_perp", "okx_xperps"])
def test_perpetual_mifid_zostaje_do_zmierzenia(nazwa):
    """Maker 0,02% = 4 pb: jedyne cenniki w UE, przy ktorych po prowizji
    zostaje miejsce na spread i niewypelnienie. Nie OTWARTA - do zmierzenia."""
    k = KANDYDATKI[nazwa]
    b = brama_0_kandydatki(k)
    assert b.stan == NIEROZSTRZYGNIETA
    assert b.skladniki["prowizja"] == pytest.approx(4.0)


def test_kraken_perp_zapas_na_niewypelnienie():
    """4 pb prowizji + 1,99 pb spreadu (mediana snapshotu) = 6 pb; zostaje 6 pb
    na niewypelnienie. Symulacja na swiecach globalnych daje 40 pb - gdyby to
    byla liczba z tej ksiegi, brama bylaby zamknieta 5x."""
    k = KANDYDATKI["kraken_perp"]
    assert zapas_na_niewypelnienie_pb(k) == pytest.approx(12.0 - 4.0 - 1.99)
    scenariusz = brama_0_kandydatki(k, niewypelnienie_pb=NIEWYPELNIENIE_SYMULACJI_PB)
    assert scenariusz.stan == ZAMKNIETA
    assert scenariusz.runda_pb == pytest.approx(45.99)


@pytest.mark.parametrize(
    "k", [k for k in KANDYDACI_BRAMY_0 if k.rynek != "stock"], ids=lambda k: k.nazwa)
def test_przy_niewypelnieniu_z_symulacji_zadna_krypto_nie_przechodzi(k):
    """Sam skladnik 40 pb przekracza prog 12 pb. Jesli ta liczba jest bliska
    prawdy takze na innej ksiedze, zmiana gieldy nie pomoze - rozstrzyga
    pomiar niewypelnienia na gieldzie docelowej, nie cennik."""
    b = brama_0_kandydatki(k, niewypelnienie_pb=NIEWYPELNIENIE_SYMULACJI_PB)
    assert b.stan == ZAMKNIETA


def test_kandydatki_bez_stawki_albo_spreadu_nie_maja_zapasu():
    """Zapas liczony bez skladnika wygladalby na wiekszy, niz jest."""
    assert zapas_na_niewypelnienie_pb(KANDYDATKI["coinbase_futures_eu"]) is None
    assert zapas_na_niewypelnienie_pb(KANDYDATKI["okx_xperps"]) is None
    assert zapas_na_niewypelnienie_pb(KANDYDATKI["alpaca_stock"]) is None


def test_alpaca_prowizja_zero_jest_liczba_a_nie_brakiem():
    """Zero prowizji to 0 pb w skladniku, a brama i tak nierozstrzygnieta:
    spread equity nikt nie zmierzyl (feed IEX, docs/08), oplaty regulacyjne
    sa poza symetrycznym ulamkiem ceny."""
    b = brama_0_kandydatki(KANDYDATKI["alpaca_stock"])
    assert b.skladniki["prowizja"] == 0.0
    assert b.stan == NIEROZSTRZYGNIETA


def test_tablica_kandydatek_nie_zmienia_cennika_silnika():
    assert set(CENNIK) == {"spot", "spot_eu", "perp", "stock", "etf"}
    for k in KANDYDACI_BRAMY_0:
        assert koszty(k.nazwa) is DOMYSLNE


def test_kandydatki_niedostepne_z_pl_sa_oznaczone_z_powodem():
    """False bez powodu w `uwaga` to flaga bez pochodzenia."""
    for k in KANDYDACI_BRAMY_0:
        if k.dostepna_pl is False:
            assert k.uwaga, k.nazwa
