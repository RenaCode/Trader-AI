"""Wynik strony KRÓTKIEJ osobno od DŁUGIEJ — bo nikt go nigdy nie zmierzył.

DLACZEGO TO ISTNIEJE

Nagłówek `services/engine/portfolio.py` mówi wprost, przy polu `side`:
audyt zmierzył, że **135 ze 180 sygnałów ponad progiem to sygnały krótkie,
i nikt nie wie, ile były warte**. Trzy czwarte sygnałów bez żadnej miary — i to
w wariancie (perp), który istnieje wyłącznie po to, żeby krótkie były możliwe.

DRUGI POWÓD JEST MOCNIEJSZY I DOTYCZY PROGU WEJŚCIA

Próg wejścia to percentyl `abs(score)`, czyli JEDNA liczba dla obu stron.
Skala wyniku obu stron nie jest ta sama — zmierzone 2026-09-27 na produkcji,
reżim `range`: p88 strony długiej 0,6205 wobec 0,0568 strony krótkiej, czyli
JEDENASTOKROTNIE. Próg `range` = 0,3509 leży między nimi, więc po stronie
długiej przepuszcza mniej więcej zamierzone 12%, a po krótkiej niemal nic.
`range` jest przez to praktycznie JEDNOKIERUNKOWY i nikt tego nie postanowił —
to ta sama wada, co próg wspólny dla reżimów, tylko o poziom głębiej.

CZEGO TA ZMIANA NIE ROBI: nie rusza bramki. Najpierw liczba, potem decyzja.
Jeśli krótkie w `range` są bezwartościowe, dzisiejsze zachowanie jest
szczęśliwym trafem, który warto zapisać jako regułę; jeśli są dobre, tracimy je
przez skalę wyniku, a nie przez decyzję. Tego nie wie dziś nikt, bo tabeli
nie było.
"""
from __future__ import annotations

import re
from pathlib import Path

from services.engine.train import opis_stron

KORZEN = Path(__file__).resolve().parents[1]
TRAIN = (KORZEN / "services" / "engine" / "train.py").read_text(encoding="utf-8")


def _tekst(trans) -> str:
    return "\n".join(opis_stron(trans))


def test_brak_transakcji_to_BRAK_TABELI_a_nie_tabela_zer():
    """Zero Sharpe'a przy zerze transakcji czyta się jak pomiar. Nie jest nim."""
    assert opis_stron([]) == []


def test_obie_strony_MAJA_WLASNY_wiersz_i_wlasne_n():
    trans = [(0.01, 1, "range")] * 30 + [(-0.02, -1, "range")] * 12
    t = _tekst(trans)
    assert "długa" in t and "krótka" in t
    assert "n=30" in t and "n=12" in t
    assert "udział krótkich w transakcjach: 28.6%" in t, (
        f"udział krótkich policzony źle albo nie podany wcale: {t}")


def test_STRONY_NIE_DA_SIE_odtworzyc_ze_znaku_zwrotu():
    """Sedno istnienia kolumny `side`: stratny long i stratny short to ten sam
    znak zwrotu, a różne zdarzenia. Ta sama lista zwrotów przypisana raz do
    długich, raz do krótkich, musi dać RÓŻNE tabele."""
    zwroty = [-0.01, -0.02, +0.03, -0.04]
    dlugie = _tekst([(r, 1, "range") for r in zwroty])
    krotkie = _tekst([(r, -1, "range") for r in zwroty])
    assert dlugie != krotkie, (
        "ta sama lista zwrotów daje identyczną tabelę niezależnie od strony — "
        "czyli strona nie jest w ogóle czytana")
    assert "n=4" in dlugie and "n=0" in dlugie
    assert "n=4" in krotkie and "n=0" in krotkie


def test_JEDNA_TRANSAKCJA_to_NIE_Sharpe_a_nie_Sharpe_zero():
    """Odchylenie z jednej obserwacji nie istnieje. Zero w tym miejscu byłoby
    liczbą wziętą z niczego — a wygląda jak wynik neutralny."""
    # Jeden rezim, wiec bez rozbicia: dwa wiersze, dwie odmowy.
    t = _tekst([(0.05, 1, "range"), (-0.01, -1, "range")])
    assert t.count("za mało transakcji") == 2, (
        f"strona o jednej transakcji dostaje Sharpe'a zamiast odmowy: {t}")
    assert "Sharpe/transakcję" not in t, (
        f"przy n=1 w tabeli i tak stoi jakis Sharpe: {t}")


def test_BLAD_STANDARDOWY_stoi_obok_kazdego_Sharpe():
    """Przy Sharpe rzędu 0,1 i n rzędu stu błąd jest rzędu całej miary. Bez tej
    kolumny szum czyta się jako „strona krótka jest gorsza"."""
    trans = [(0.01 * (i % 7 - 3), 1, "range") for i in range(100)]
    t = _tekst(trans)
    assert "±0.1000" in t, f"brak błędu standardowego 1/√100 = 0,1: {t}"
    assert re.search(r"Sharpe/transakcję [+-]\d\.\d{4} ±", t), (
        "błąd standardowy nie stoi PRZY Sharpe — w osobnej linii nikt go nie "
        "zestawi z liczbą, której dotyczy")


def test_ROZBICIE_PO_REZIMACH_bo_asymetria_jest_rezimowa():
    """Asymetria 11× jest zmierzona w `range`, nie w całej próbie. Tabela bez
    reżimów nie odpowiada na pytanie, które ją wywołało."""
    trans = ([(0.02, 1, "range")] * 10 + [(-0.03, -1, "range")] * 10
             + [(0.01, 1, "trend_up")] * 10 + [(0.04, -1, "trend_up")] * 10)
    t = _tekst(trans)
    assert "po reżimach" in t
    assert "range" in t and "trend_up" in t


def test_JEDEN_REZIM_to_BRAK_rozbicia():
    """Rozbicie po jednym reżimie powtarzałoby wiersze wyżej — i sugerowało, że
    porównanie reżimów zostało zrobione."""
    t = _tekst([(0.01, 1, "range")] * 20 + [(-0.01, -1, "range")] * 20)
    assert "po reżimach" not in t


def test_tabela_MOWI_ze_NIE_JEST_TESTEM():
    """Transakcje nie są niezależne (te same świece, te same okna), więc ±1/√n
    nie jest przedziałem ufności dla różnicy stron. Bez tego zdania pierwsza
    różnica przekraczająca słupki zostanie przedstawiona jako ustalenie."""
    t = _tekst([(0.01, 1, "range")] * 60 + [(-0.01, -1, "range")] * 60)
    assert "NIE JEST TU TESTEM" in t
    assert "nie są niezależne" in t or "NIE są niezależne" in t


# ============================================ SKŁAD W TRENERZE

def test_trener_zbiera_strone_TAM_GDZIE_ZNA_REZIM():
    """`dominant` powstaje PO zebraniu zwrotów okna. Zebranie strony razem ze
    zwrotami dawało NameError na pierwszym oknie — a przy zmiennej już
    zdefiniowanej skutek byłby gorszy i cichy: reżim POPRZEDNIEGO okna
    dopisany do transakcji tego."""
    i_dom = TRAIN.index("dominant = rezim_dominujacy(regimes)")
    i_zbior = TRAIN.index("transakcje_stron.extend(")
    assert i_zbior > i_dom, (
        "strony zbierane PRZED wyliczeniem `dominant` — reżim przy transakcjach "
        "byłby z poprzedniego okna albo w ogóle niezdefiniowany")


def test_trener_zbiera_TE_SAME_okna_co_zlaczona_proba():
    """Tabela stron musi opisywać tę samą próbę, co Sharpe całości. Inaczej
    dwie liczby w jednym raporcie mówią o dwóch różnych próbach."""
    fragment = TRAIN[TRAIN.index("dominant = rezim_dominujacy(regimes)"):
                     TRAIN.index("transakcje_stron.extend(")]
    assert "if informative:" in fragment, (
        "strony zbierane także z okien NIEinformatywnych, których złączona "
        "próba `wszystkie_zwroty` nie zawiera")


def test_trener_CZYTA_side_z_transakcji_a_nie_wpisuje_stalej():
    """DZIURA W OSŁONIE, ZNALEZIONA KANARKIEM 2026-09-27.

    Podmiana `int(t.get("side", 1))` na literał `1` — czyli zignorowanie strony
    w całości — NIE ZEPSUŁA ani jednego testu. Testy `opis_stron` sprawdzają
    samą funkcję, a ta dostawała wtedy poprawne dane o niepoprawnej treści:
    tabela dalej się liczyła, dalej miała dwa wiersze i dalej wyglądała jak
    pomiar, tylko wszystkie transakcje były w niej długie.
    """
    fragment = TRAIN[TRAIN.index("transakcje_stron.extend("):]
    fragment = fragment[:fragment.index(")\n")]
    assert 't["side"]' in fragment or 't.get("side"' in fragment, (
        f"trener nie czyta `side` z transakcji — tabela stron opisuje jedną "
        f"stronę pod dwiema nazwami. Zebrane: {fragment!r}")


def test_trener_WYPISUJE_te_tabele():
    assert "for linia in opis_stron(transakcje_stron):" in TRAIN, (
        "policzone i niewypisane — przy `--sucho` nie ma tego nigdzie")
