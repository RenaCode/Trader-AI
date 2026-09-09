"""Brakujący sygnał: co robi z wynikiem, z pewnością i czy w ogóle widać, że go nie było.

CO TU JEST PILNOWANE

`combine()` liczy dwie rzeczy z jednego zestawu wag: WYNIK i PEWNOŚĆ. Przez
długi czas każda z nich traktowała brak sygnału inaczej — pewność dzieliła się
przez wagę faktycznie obecną, wynik przez domyślną jedynkę. Nikt tego nie
zaprojektował; nie było też testu, który by którąkolwiek z tych semantyk pinował,
więc różnica przetrwała, bo nic się od niej nie wywracało.

DLACZEGO TO NIE JEST KOSMETYKA. Progi wejścia i wyjścia (`train.score_threshold`)
są CENTYLAMI rozkładu |wyniku| policzonego na panelu treningowym. Próg ma sens
wyłącznie wtedy, gdy wielkość porównywana z nim ma tę samą skalę co ta, z której
próg powstał. Przy niedomkniętym mianowniku skala wyniku zależała od tego, ile
sygnałów akurat się policzyło — a to jest inna liczba w backteście i w produkcji.
Pomiar na dzienniku produkcyjnym (27.08–09.09.2026, 776 decyzji z modelem):
średnie pokrycie wag 0,893, a 686 decyzji zapadło na niepełnym komplecie.

Ten sam kod naprawiał już raz bliźniaczy rozjazd po stronie trenera — patrz
komentarz przy `bazowe = _bez_martwych(...)` w services/engine/train.py.
"""
from __future__ import annotations

import pytest

from services.engine.signals import (
    DEFAULT_WEIGHTS, EKSPERYMENTALNE, SIGNALS, SignalOutput, budzet_wag, combine,
    pokrycie_wag,
)

# Osiem sygnalow nieeksperymentalnych, wagi sumujace sie po module do 1 -
# dokladnie taki simplex produkuja `random_weights`, `ic_weights` i `_bez_martwych`.
WAGI = {"momentum_multi": 0.30, "mean_reversion_rsi": -0.20, "trend_follow": 0.10,
        "range_extreme": 0.10, "volume_breakout": 0.10, "news_shock": 0.10,
        "funding_carry": 0.05, "cross_section": 0.05}

# Ten sam budzet, ale WSZYSTKIE WAGI DODATNIE. Potrzebny tam, gdzie testujemy
# niezmienniczosc wyniku wzgledem braku sygnalu: przy wagach roznych znakow
# zgodne sygnaly NIE daja jednej wartosci (czesc glosuje odwrotnie), wiec
# niezmienniczosc tam po prostu nie zachodzi i nie ma czego pilnowac.
WAGI_DODATNIE = {k: abs(v) for k, v in WAGI.items()}

KOMPLET = set(SIGNALS) - EKSPERYMENTALNE


def test_wagi_w_tescie_sa_takim_simplexem_jak_produkcyjne():
    """Gdyby suma |wag| nie wynosila 1, ten plik mierzylby cos innego niz produkcja."""
    assert set(WAGI) == KOMPLET
    assert sum(abs(v) for v in WAGI.values()) == pytest.approx(1.0)
    for rezim, w in DEFAULT_WEIGHTS.items():
        assert sum(abs(v) for v in w.values()) == pytest.approx(1.0, abs=1e-9), rezim


# ------------------------------------------------- semantyka brakujacego sygnalu
def test_brak_sygnalu_nie_sciaga_wyniku_ku_zeru():
    """Sygnał, którego nie ma, NIE jest głosem na neutralność.

    Gdy wszystkie policzone sygnały mówią to samo z tą samą pewnością, wynik
    musi być taki sam niezależnie od tego, ilu sygnałów zabrakło. Inaczej sama
    niedostępność danych przesuwa wynik — i to proporcjonalnie do wagi, czyli
    najmocniej wtedy, gdy braknie sygnału NAJWAŻNIEJSZEGO.
    """
    zgodne = {n: SignalOutput(0.6, 1.0, {}) for n in KOMPLET}
    pelny, _, _ = combine(zgodne, "range", WAGI_DODATNIE)
    assert pelny == pytest.approx(0.6)

    for brakuje in sorted(KOMPLET):
        czesciowy, _, _ = combine({k: v for k, v in zgodne.items() if k != brakuje},
                                  "range", WAGI_DODATNIE)
        assert czesciowy == pytest.approx(pelny, abs=1e-12), (
            f"brak sygnału {brakuje} zmienił wynik z {pelny:.6f} na {czesciowy:.6f} "
            f"— a wszystkie pozostałe mówiły dokładnie to samo")


def test_wynik_jest_srednia_wazona_po_wadze_OBECNEJ():
    """Mianownik wyniku to suma |wag| sygnałów, które się policzyły — nie 1,0.

    Liczby wpisane wprost, żeby test padał na WARTOŚCI, a nie na przepisanym
    tu jeszcze raz wzorze z kodu.
    """
    policzone = {"momentum_multi":     SignalOutput(0.8, 0.50, {}),
                 "mean_reversion_rsi": SignalOutput(-0.4, 1.00, {}),
                 "trend_follow":       SignalOutput(0.2, 0.25, {})}
    # licznik      = 0,30*0,8*0,50 + (-0,20)*(-0,4)*1,00 + 0,10*0,2*0,25 = 0,205
    # waga obecna  = 0,30 + 0,20 + 0,10                                  = 0,60
    # wynik        = 0,205 / 0,60                                        = 0,341667
    # pewnosc      = (0,50*0,30 + 1,00*0,20 + 0,25*0,10) / 0,60          = 0,625
    wynik, pewnosc, _ = combine(policzone, "range", WAGI)
    assert wynik == pytest.approx(0.3416666667, abs=1e-9), (
        "wynik podzielony przez domyślną jedynkę zamiast przez wagę obecną "
        f"(0,205 zamiast 0,341667?) — dostaliśmy {wynik:.6f}")
    assert pewnosc == pytest.approx(0.625, abs=1e-9)


def test_wynik_i_pewnosc_maja_TEN_SAM_mianownik():
    """Dwie średnie z tych samych składników nie mogą mieć dwóch różnych podstaw.

    Sprawdzane przez SKALOWANIE: pomnożenie wszystkich wag przez stałą nie może
    zmienić ani wyniku, ani pewności, bo obie są średnimi ważonymi. Przy
    mianowniku „na sztywno 1,0" wynik przeskalowałby się razem z wagami,
    a pewność nie — i to jest cała treść tej niespójności.
    """
    policzone = {"momentum_multi":     SignalOutput(0.8, 0.50, {}),
                 "mean_reversion_rsi": SignalOutput(-0.4, 1.00, {}),
                 "trend_follow":       SignalOutput(0.2, 0.25, {})}
    a = combine(policzone, "range", WAGI)
    b = combine(policzone, "range", {k: 3.0 * v for k, v in WAGI.items()})
    assert a[0] == pytest.approx(b[0], abs=1e-9), "wynik zależy od SKALI wag"
    assert a[1] == pytest.approx(b[1], abs=1e-9), "pewność zależy od SKALI wag"


def test_pusty_komplet_nie_wywraca_i_nie_zmysla_wyniku():
    """Zero policzonych sygnałów to brak zdania, nie zdanie „zero"."""
    assert combine({}, "range", WAGI) == (0.0, 0.0, [])
    assert combine({}, "range", WAGI, with_contributions=False) == (0.0, 0.0, [])


# ------------------------------------------------------------- obie sciezki
def test_obie_sciezki_zgodne_takze_przy_BRAKACH():
    """`with_contributions` przełącza tylko rozbicie, nigdy wynik.

    Istniejący test tej równości (tests/test_signals.py) karmi obie ścieżki
    KOMPLETEM sygnałów, więc nie dotyka mianownika. Renormalizacja jest liczona
    w dwóch osobnych pętlach i to tutaj rozjazd byłby najłatwiejszy.
    """
    policzone = {"momentum_multi":     SignalOutput(0.8, 0.50, {}),
                 "mean_reversion_rsi": SignalOutput(-0.4, 1.00, {}),
                 "cross_section":      SignalOutput(0.9, 0.75, {})}
    for n in range(1, len(policzone) + 1):
        podzbior = dict(list(policzone.items())[:n])
        for rezim in ("trend_up", "range", "high_vol"):
            pelne = combine(podzbior, rezim, WAGI, with_contributions=True)
            szybkie = combine(podzbior, rezim, WAGI, with_contributions=False)
            assert pelne[0] == szybkie[0], f"wynik rozjechany, {n} sygnałów, {rezim}"
            assert pelne[1] == szybkie[1], f"pewność rozjechana, {n} sygnałów, {rezim}"


# ------------------------------------------------------------------ pokrycie
def test_pokrycie_mierzy_UTRACONY_budzet_wag():
    """Po renormalizacji wynik już nie zdradza, ile sygnałów za nim stało.

    To jest cena tej semantyki i dlatego pokrycie musi być osobną liczbą:
    +0,34 z trzech sygnałów wygląda identycznie jak +0,34 z ośmiu.
    """
    assert pokrycie_wag(KOMPLET, "range", WAGI) == pytest.approx(1.0)
    # 0,30 + 0,20 + 0,10 z budzetu 1,00
    assert pokrycie_wag(["momentum_multi", "mean_reversion_rsi", "trend_follow"],
                        "range", WAGI) == pytest.approx(0.60)
    assert pokrycie_wag([], "range", WAGI) == pytest.approx(0.0)


def test_brak_sygnalu_o_ZEROWEJ_wadze_to_nie_jest_brak():
    """Sygnał wyzerowany przez `_bez_martwych` nie miał czego wnieść.

    Gdyby pokrycie liczyło sygnały, a nie wagę, każdy model z wyzerowanym
    martwym sygnałem raportowałby stałe „niepełne pokrycie" i licznik przestałby
    cokolwiek znaczyć — a martwe sygnały są tu regułą, nie wyjątkiem.
    """
    wagi = dict(WAGI, funding_carry=0.0)
    wagi["momentum_multi"] = 0.35                     # budzet nadal 1,00
    bez_martwego = KOMPLET - {"funding_carry"}
    assert pokrycie_wag(bez_martwego, "range", wagi) == pytest.approx(1.0)


def test_wylaczony_eksperyment_nie_zaniza_pokrycia():
    """Sygnał eksperymentalny jest w wektorze wag, ale `precompute` go pomija.

    Model zapisuje wagi dla wszystkich dziewięciu sygnałów (`random_weights`
    iteruje po całym SIGNALS), więc gdyby budżet liczył się po kluczach wag,
    produkcja z wyłączonym eksperymentem raportowałaby braki, których nie ma.
    """
    assert EKSPERYMENTALNE, "test opisuje sytuację, której już nie ma"
    wagi = dict(WAGI, **{n: 0.5 for n in EKSPERYMENTALNE})
    assert pokrycie_wag(KOMPLET, "range", wagi) == pytest.approx(1.0)
    # ...ale gdy eksperyment JEST włączony, wchodzi do budżetu i braki liczą się
    # względem niego.
    assert budzet_wag("range", wagi, KOMPLET | EKSPERYMENTALNE) == pytest.approx(
        1.0 + 0.5 * len(EKSPERYMENTALNE))


# ------------------------------------------------------- widocznosc w dzienniku
def test_petla_zapisuje_pokrycie_do_dziennika():
    """Bez zapisu pytanie „ile decyzji zapadło na niepełnym komplecie" jest bez odpowiedzi.

    Dało się je policzyć okrężnie — zestawiając `journal.decision_contribution`
    z wektorem wag wersji modelu dla reżimu decyzji — i tak właśnie zmierzono
    88% niepełnych decyzji na oknie 27.08–09.09.2026. Ale rachunek wymagający
    trzech złączeń i znajomości semantyki wag nie jest widocznością; nikt go
    nie zrobi, dopóki czegoś nie szuka.
    """
    from services.engine import loop

    zapisane: dict[str, object] = {}

    class Kursor:
        def execute(self, sql, params=None):
            if "INSERT INTO journal.decision " in sql:
                zapisane["sql"], zapisane["params"] = sql, params

        def fetchone(self):
            return {"id": 1}

        def executemany(self, sql, rows):
            pass

    werdykt = type("V", (), {"verdict": "allow", "reason": None})()
    loop.record_decision(
        Kursor(), "crypto", "demo", 7, __import__("datetime").datetime(2026, 9, 9),
        "hold", None, 100, 0.5, "range", {"rsi": 50.0}, werdykt, "uzasadnienie",
        [], {}, 12, 130, 0.8125)

    kolumny = zapisane["sql"].split("journal.decision (", 1)[1].split(")", 1)[0]
    assert "pokrycie_sygnalow" in kolumny, (
        "INSERT nie zapisuje pokrycia — wynik po renormalizacji nie odróżnia "
        "decyzji z trzech sygnałów od decyzji z ośmiu")
    assert 0.8125 in zapisane["params"], "kolumna w SQL, ale wartość nie dolatuje"


def test_kolumna_pokrycia_istnieje_w_migracjach():
    """Kod pisze do kolumny, której schemat może nie mieć — i to jest cicha awaria.

    Wyjątek `column ... does not exist` wywraca CAŁĄ transakcję cyklu
    decyzyjnego, a proces w `workers` jest osłonięty przez `|| true`, więc
    z zewnątrz wygląda to na przebieg zakończony powodzeniem.
    """
    from tests.atrapa_bazy import MIGRACJE

    tresc = "\n".join(p.read_text(encoding="utf-8") for p in MIGRACJE)
    assert "pokrycie_sygnalow" in tresc, "brak migracji dodającej kolumnę"
    assert "ALTER TABLE journal.decision" in tresc


def test_petla_liczy_decyzje_na_niepelnym_komplecie():
    """Licznik idzie do heartbeatu — jedynego miejsca, gdzie stan przebiegu widać z zewnątrz."""
    import re
    from pathlib import Path

    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    assert '"niepelne_sygnaly": 0' in zrodlo, "licznik nie jest inicjalizowany w stats"
    assert re.search(r'stats\["niepelne_sygnaly"\] \+= 1', zrodlo), \
        "licznik zadeklarowany, ale nigdy nie rośnie"
    # stats trafia do risk.engine_heartbeat.detail w tym samym pliku.
    assert "json.dumps(stats)" in zrodlo
