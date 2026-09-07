"""Diagnostyka IC: pominięcie jest wynikiem, a błąd standardowy uwzględnia nakładanie.

DWA PROBLEMY, OBA Z TEJ SAMEJ RODZINY CO RESZTA DNIA

1. Przy zbyt krótkim `--days` odpadały wszystkie instrumenty (próg 500 świec
   panelu), skrypt wypisywał pustą tabelę i kończył się zerem. „Żaden sygnał nic
   nie przewiduje" i „nie było czego liczyć" wyglądały identycznie.

2. Błąd standardowy IC liczony jako 1/√n zakłada niezależne obserwacje. Zwrot
   z k świec liczony przy każdej świecy nakłada się na k−1 sąsiednich pomiarów,
   więc naiwny błąd zaniża się tym mocniej, im dłuższy horyzont — czyli tam,
   gdzie IC zwykle wygląda najlepiej. Pierwszy pomiar mikrostruktury dał przez to
   „+0,1532, trzy i pół raza więcej niż najlepszy sygnał"; po poprawce wyszło
   1,6 błędu standardowego.
"""
import re
from pathlib import Path

ZRODLO = Path("services/engine/diagnose_signals.py").read_text()


def test_brak_danych_konczy_sie_kodem_niezerowym():
    assert "return 2" in ZRODLO, "pusty wynik kończy się sukcesem — nie do odróżnienia od zerowego IC"
    i = ZRODLO.index("if not zebrane:")
    assert "PRZERWANO" in ZRODLO[i:i + 300]


def test_pominiete_instrumenty_sa_wypisywane():
    """Cicha pętla `continue` ukrywała, ile instrumentów w ogóle weszło do pomiaru."""
    assert "pominiete.append" in ZRODLO
    assert "pominięte (panel <" in ZRODLO


def test_blad_standardowy_uwzglednia_nakladanie_obserwacji():
    """Efektywna liczba obserwacji to n/k, nie n."""
    assert re.search(r"n_ef\s*=\s*len\(xs\)\s*/\s*k", ZRODLO), \
        "błąd standardowy nie dzieli liczby obserwacji przez horyzont"


def test_cechy_bez_sygnalu_sa_mierzone_osobno():
    """Mierzenie to nie budowanie: IC cech, których żaden sygnał nie używa,
    odpowiada na pytanie „czy warto ten sygnał w ogóle zbudować"."""
    assert "CECHY_BEZ_SYGNALU" in ZRODLO
    assert "book_imbalance" in ZRODLO and "book_spread_bps" in ZRODLO


def test_prog_panelu_jest_stala_a_nie_liczba_w_warunku():
    """Liczba 500 wpisana w warunek nie da się wyjaśnić w komunikacie błędu."""
    assert "MIN_PANEL = 500" in ZRODLO
    assert "len(panel) < MIN_PANEL" in ZRODLO
