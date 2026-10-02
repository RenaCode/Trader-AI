"""Pokrycie wag: ile warta jest transakcja z jednego sygnału wzmocnionego 20×.

DLACZEGO TO ISTNIEJE

`signals.combine` dzieli wynik przez sumę |wag| sygnałów, które się **odezwały**.
Renormalizacja mówi „pozostałe reprezentują całość", więc wynik z jednego
sygnału może sięgnąć pełnej siły. Docstring `combine` nazywa to **ceną tej
semantyki** i zostawia decyzję „od jakiego pokrycia nie handlujemy" jako jawną
i **niepodjętą**.

Zmierzone na produkcji 2026-09-27, decyzje z 21 dni:

    reżim       decyzji  mediana pokrycia  poniżej 0,5
    range           639             0,051    590 (92%)
    high_vol        269             1,000      0
    trend_up        142             1,000      0

W `range` odzywają się tylko `news_shock` (waga 0,0) i `funding_carry`
(waga ~0,04); sześć sygnałów cenowo-wolumenowych milczy w 91% decyzji, bo
w konsolidacji nie mają czego powiedzieć. Wynik jest więc zwykle **jednym
sygnałem podzielonym przez 0,05**, czyli wzmocnionym dwudziestokrotnie — a
`range` jest jedynym reżimem z ujemnym Sharpe'em na transakcję (−0,0478 na 437
transakcjach, tor 4h).

NA PRODUKCJI REŻIMU I POKRYCIA ROZPLEŚĆ SIĘ NIE DA: całe niskie pokrycie jest
w `range`, cały `range` ma niskie pokrycie. „Niskie pokrycie szkodzi" i „`range`
jest trudny" dają tam identyczne dane. Rozstrzygnąć to może tylko symulacja —
i po to pokrycie trafia do transakcji oraz do raportu.

CZEGO TE TESTY PILNUJĄ NAJMOCNIEJ: że ślad z `combine` to **ten sam mianownik**,
przez który dzieli się wynik, a nie druga kopia rachunku. Pokrycia nie da się
odzyskać z wyniku ani z pewności (`conf` jest już przez nie podzielone), więc
rozjazd dwóch kopii byłby niewidoczny — a kłamałaby dokładnie ta liczba, na
której ktoś postawi próg.
"""
from __future__ import annotations

from pathlib import Path

from services.engine.signals import SignalOutput, combine
from services.engine.train import opis_pokrycia

KORZEN = Path(__file__).resolve().parents[1]
TRAIN = (KORZEN / "services" / "engine" / "train.py").read_text(encoding="utf-8")
PORTFEL = (KORZEN / "services" / "engine"
           / "portfolio.py").read_text(encoding="utf-8")


def _out(score: float, conf: float = 1.0) -> SignalOutput:
    return SignalOutput(score=score, confidence=conf, inputs={})


# NAZWY PRAWDZIWE, i to nie jest kosmetyka. Dwie sciezki `combine` roznia sie
# tym, po czym iteruja: szybka po `precomputed`, ta z rozbiciem po `SIGNALS`
# z pominieciem nazw, ktorych w `precomputed` nie ma. Nazwa wymyslona daje wiec
# na jednej sciezce pokrycie 0,1, a na drugiej 0,0 - i test z atrapa nazwy
# "wykrylby" wade, ktorej nie ma, albo przeoczyl te, ktora jest.
WAGI = {"momentum_multi": 0.6, "mean_reversion_rsi": 0.3, "range_extreme": 0.1}
JEDEN = "range_extreme"       # waga 0,1
NIEZNANY = "sygnal_ktorego_nie_ma"


# ================================================= ŚLAD Z `combine`

def test_slad_oddaje_TEN_SAM_mianownik_ktorym_combine_dzieli():
    """Sprawdzane WYNIKIEM, nie porównaniem dwóch rachunków.

    Jeden sygnał o wadze 0,1 i wyniku 0,5: gdyby mianownikiem była suma
    wszystkich wag (1,0), wynik byłby 0,05. Mianownikiem jest waga obecna (0,1),
    więc wynik to 0,5 — i ślad musi podać dokładnie tę 0,1.
    """
    slad: dict = {}
    wynik, _conf, _ = combine({JEDEN: _out(0.5)}, "range", WAGI,
                              with_contributions=False, slad=slad)
    assert abs(wynik - 0.5) < 1e-9, (
        f"mianownik nie jest wagą obecną — wynik {wynik}, oczekiwany 0,5")
    assert abs(slad["pokrycie"] - 0.1) < 1e-9, (
        f"ślad podaje {slad.get('pokrycie')!r}, a `combine` dzieliło przez 0,1")


def test_slad_dziala_TAKZE_ze_rozbiciem_na_wklady():
    """Dwie ścieżki `combine` (z rozbiciem i bez) liczą mianownik osobno, więc
    obie muszą ślad wypełniać — inaczej pokrycie zależałoby od tego, czy
    wołający chciał rozbicie."""
    slad: dict = {}
    combine({JEDEN: _out(0.5)}, "range", WAGI, with_contributions=True, slad=slad)
    assert abs(slad["pokrycie"] - 0.1) < 1e-9, (
        "ścieżka Z rozbiciem nie wypełnia śladu — pokrycie zależy od "
        "`with_contributions`, czyli od czegoś, co go nie dotyczy")


def test_pelne_pokrycie_to_suma_wag_ktore_sie_odezwaly():
    slad: dict = {}
    combine({n: _out(0.2) for n in WAGI}, "range", WAGI,
            with_contributions=False, slad=slad)
    assert abs(slad["pokrycie"] - 1.0) < 1e-9


def test_slad_wypelnia_sie_TAKZE_przy_pokryciu_ZERO():
    """Zero to stan, o którym trzeba wiedzieć najbardziej: wynik jest wtedy
    zerem z definicji, a nie z pomiaru. Brak klucza w śladzie byłby
    nieodróżnialny od „nikt nie pytał"."""
    slad: dict = {}
    wynik, conf, _ = combine({NIEZNANY: _out(0.9)}, "range", WAGI,
                             with_contributions=False, slad=slad)
    assert (wynik, conf) == (0.0, 0.0)
    assert slad["pokrycie"] == 0.0, (
        "przy zerowym pokryciu ślad nie dostaje wartości — a to jedyny "
        "przypadek, w którym wynik nie mówi nic o rynku")


def test_brak_sladu_NIE_ZMIENIA_wyniku():
    """Ścieżka bez śladu jest tą, którą idą miliony wywołań symulacji."""
    a = combine({JEDEN: _out(0.5)}, "range", WAGI, with_contributions=False)
    b = combine({JEDEN: _out(0.5)}, "range", WAGI, with_contributions=False,
                slad={})
    assert a == b


# ================================================= SKŁAD W SYMULACJI

def test_pozycja_NIESIE_pokrycie_a_transakcja_je_ODDAJE():
    assert "pokrycie: float = 1.0" in PORTFEL, (
        "`Position` nie ma pola `pokrycie` — transakcja nie ma czego oddać")
    assert PORTFEL.count('"pokrycie": pos.pokrycie') == 2, (
        "pokrycie nie trafia do OBU zapisów transakcji (zamknięcie normalne "
        "i domknięcie na końcu okna). Brak w jednym z nich znaczy, że część "
        "próby ma wartość domyślną 1,0 zamiast zmierzonej")


def test_pokrycie_liczone_TYLKO_przy_otwarciu_pozycji():
    """`combine` idzie dla każdej świecy każdego instrumentu — ślad przy TAMTYM
    wywołaniu alokowałby słownik miliony razy. Przy otwarciu wywołań jest tyle,
    ile transakcji."""
    i = PORTFEL.index("positions[sym] = Position(")
    przed = PORTFEL[max(0, i - 1200):i]
    assert "slad=_slad_pokrycia" in przed, (
        "pokrycie nie jest liczone przy otwarciu pozycji")
    for linia in PORTFEL.splitlines():
        naga = linia.split("#")[0]
        if "combine(" in naga and "slad=" in naga and "_slad_pokrycia" not in naga:
            raise AssertionError(
                f"inne wywołanie `combine` też przekazuje ślad: {linia.strip()!r} "
                f"— jeśli stoi w pętli po świecach, alokuje słownik na świecę")


def test_trener_zbiera_i_WYPISUJE_tabele_pokrycia():
    assert "transakcje_pokrycia.extend(" in TRAIN
    assert "for linia in opis_pokrycia(transakcje_pokrycia):" in TRAIN, (
        "policzone i niewypisane — przy `--sucho` nie ma tego nigdzie")
    i_zbior = TRAIN.index("transakcje_pokrycia.extend(")
    fragment = TRAIN[TRAIN.index("dominant = rezim_dominujacy(regimes)"):i_zbior]
    assert "if informative:" in fragment, (
        "pokrycie zbierane także z okien NIEinformatywnych, których złączona "
        "próba nie zawiera")


# ================================================= TABELA

def _tekst(trans) -> str:
    return "\n".join(opis_pokrycia(trans))


def test_brak_transakcji_to_BRAK_TABELI():
    assert opis_pokrycia([]) == []


def test_kubelki_ROZDZIELAJA_transakcje_po_pokryciu():
    trans = ([(0.01, 0.05, "range")] * 30 + [(0.02, 1.0, "high_vol")] * 30)
    t = _tekst(trans)
    assert "pokrycie 0.0–0.2" in t and "pokrycie 0.9–1.0" in t
    assert t.count("n=30") >= 2


def test_pokrycie_ROWNE_JEDEN_wpada_do_najwyzszego_kubelka():
    """Górna granica ostatniego kubełka to 1,01 właśnie dlatego: pokrycie 1,0
    jest najczęstszą wartością i wypadnięcie jej z tabeli byłoby ciche."""
    t = _tekst([(0.01, 1.0, "range")] * 20)
    assert "pokrycie 0.9–1.0" in t
    assert "n=20" in t


def test_rezim_o_STALYM_pokryciu_jest_NAZWANY_a_nie_tylko_wypisany():
    """Reżim, w którym pokrycie się nie zmienia, nie rozdziela niczego —
    a wypisany bez tego zdania wygląda tak samo jak ten, który rozdziela."""
    trans = ([(0.01, 0.05, "range")] * 20 + [(0.02, 0.05, "range")] * 5
             + [(0.03, 1.0, "high_vol")] * 20)
    t = _tekst(trans)
    assert "NIE ZMIENIA SIĘ w tym reżimie" in t, (
        f"reżim o jednym kubełku wypisany bez zastrzeżenia: {t}")


def test_rezim_o_ZMIENNYM_pokryciu_NIE_dostaje_tego_zastrzezenia():
    trans = ([(0.01, 0.05, "range")] * 20 + [(0.02, 1.0, "range")] * 20
             + [(0.03, 1.0, "high_vol")] * 20)
    t = _tekst(trans)
    assert t.count("NIE ZMIENIA SIĘ w tym reżimie") == 1, (
        f"zastrzeżenie trafiło do reżimu, w którym pokrycie SIĘ zmienia: {t}")


def test_tabela_MOWI_ze_NIE_JEST_TESTEM():
    t = _tekst([(0.01, 0.05, "range")] * 20 + [(0.02, 1.0, "high_vol")] * 20)
    assert "To NIE JEST test" in t
    assert "nie są niezależne" in t
