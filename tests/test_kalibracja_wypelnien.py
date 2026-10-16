"""Model wypełnień kalibrowany do wypełnialności WEJŚĆ z demo (renacode-infra: docs/projekty/trader/hipotezy.md H-10).

SKĄD TEN PLIK

H-5 dopuszczała różnicę ≤ 10 pp między wypełnialnością wejść w symulacji
a na demo. Pierwszy przebieg po wdrożeniu (2026-10-11) wypisał 78,7% wobec
„ok. 65%" na demo — i to porównanie było podwójnie złe:

  * 78,7% to wejścia i wyjścia RAZEM (`metrics`: `wypelnione` = wejścia +
    wyjścia), a demo zmierzyło same wejścia (42/64, E-1). Wyjścia ponawiają
    się co decyzję, więc ta suma nie jest wielkością z demo;
  * nawet poprawnie policzona, wypełnialność modelu „świeca dotknęła limitu"
    jest górnym oszacowaniem: PostOnly stoi na końcu kolejki, a świece są
    z księgi ~1000× płynniejszej niż ta, na której stoi zlecenie.

Poprawka: osobne liczniki wejść, przebicie limitu (`Egzekucja.przebicie_bps`)
i kalibracja przebicia do wypełnialności demo (`kalibruj_przebicie`).

CZEGO TE TESTY PILNUJĄ PONAD MECHANIKĘ

Kalibracja patrzy WYŁĄCZNIE na to, czy wejście weszło. Funkcja nie dostaje
zwrotów — gdyby dostawała, „przebicie, przy którym demo się zgadza" byłoby
strojeniem strategii pod wynik.
"""
from __future__ import annotations

import inspect
import random
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.engine import train as tr
from services.engine.horizons import get as get_horizon
from services.engine.portfolio import (
    Egzekucja, glebokosc_bps, indeks_swiec, kalibruj_przebicie, metrics,
    simulate_portfolio,
)
from services.engine.signals import DEFAULT_WEIGHTS
from tests.test_portfolio import LIMITY_LUZNE, cechy_kupuj

KORZEN = Path(__file__).resolve().parents[1]
H4 = get_horizon("4h")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DEMO = 42 / 64          # wypełnialność wejść E-1 - liczba, do której kalibrujemy


# ------------------------------------------------------------ funkcja czysta

def test_glebokosc_zgadza_sie_ze_starym_warunkiem_dotkniecia():
    """Przebicie 0 ma być DOKŁADNIE starym modelem: `glebokosc >= 0` wtedy
    i tylko wtedy, gdy `low <= limit` (kupno) / `high >= limit` (sprzedaż)."""
    for low, high in ((99.0, 101.0), (100.0, 100.0), (100.01, 100.02), (99.99, 99.995)):
        s = {"low": low, "high": high}
        assert (glebokosc_bps(100.0, s, True) >= 0) == (low <= 100.0)
        assert (glebokosc_bps(100.0, s, False) >= 0) == (high >= 100.0)
    assert glebokosc_bps(100.0, {"low": 99.9, "high": 0}, True) == pytest.approx(10.0)
    assert glebokosc_bps(100.0, {"low": 0, "high": 100.2}, False) == pytest.approx(20.0)


def test_kalibracja_trafia_w_cel_od_dolu():
    """100 wejść o głębokościach -19..80 pb: 81 dotknęło. Cel 65,6% → przebicie,
    przy którym wchodzi 65 (≤ 65,6), nie 66."""
    gl = [float(g) for g in range(-19, 81)]
    x = kalibruj_przebicie(gl, 100, DEMO)
    weszlo = sum(1 for g in gl if g >= x)
    assert weszlo == 65
    assert x > 0


def test_kalibracja_tylko_zaostrza():
    """Symulacja wypełniająca RZADZIEJ niż demo zostaje bez zmian: model nie
    może wypełniać łatwiej niż „świeca dotknęła limitu"."""
    gl = [float(g) for g in range(-50, 50)]          # 50% dotknęło
    assert kalibruj_przebicie(gl, 100, DEMO) == 0.0
    assert kalibruj_przebicie([], 0, DEMO) == 0.0


def test_wejscia_bez_swiecy_sa_w_mianowniku():
    """Wejście bez świecy 1h jest niewypełnione przy każdym przebiciu, więc
    liczy się do prób, choć nie ma głębokości."""
    gl = [10.0] * 70                                 # 70 dotknęło
    assert kalibruj_przebicie(gl, 100, 0.70) == 0.0   # 70/100 = cel
    assert kalibruj_przebicie(gl, 100, 0.69) > 10.0   # remis: wszystkie odpadają


def test_kalibracja_odmawia_celu_spoza_przedzialu():
    with pytest.raises(ValueError):
        kalibruj_przebicie([1.0, 2.0], 2, 0.0)
    with pytest.raises(ValueError):
        kalibruj_przebicie([1.0, 2.0], 2, 65.6)      # procent zamiast ułamka


def test_kalibracja_nie_widzi_zwrotow():
    """Sygnatura jest kontraktem: tylko głębokości, liczba prób i cel."""
    assert list(inspect.signature(kalibruj_przebicie).parameters) == [
        "glebokosci", "prob", "wypelnialnosc_docelowa"]
    zrodlo = inspect.getsource(tr.kalibracja_wypelnien)
    assert "glebokosci_wejsc_oos" in zrodlo
    for zwrot in ("wszystkie_zwroty", "trades", '"ret"', "sharpe"):
        assert zwrot not in zrodlo, zwrot


# ------------------------------------------------- na symulacji portfelowej

def _rynek(n_sym: int, n4h: int, ziarno: int = 20261011):
    """Rynek płaski (cena 100) z dołkiem godzin z rozkładu jednostajnego
    0..25 pb. Przy spreadzie 10 pb limit kupna leży 5 pb pod ceną, więc
    świeca go dotyka w ~80% godzin - tyle co model sprzed H-10 (76-79%)."""
    rng = random.Random(ziarno)
    panele, swiece = {}, {}
    for i in range(n_sym):
        sym = f"S{i:02d}"
        s = []
        for k in range(4 * n4h + 8):
            d = rng.uniform(0.0, 0.0025)
            s.append({"t": T0 + timedelta(hours=k), "open": 100.0, "close": 100.0,
                      "low": 100.0 * (1 - d), "high": 100.0 * (1 + d)})
        swiece[sym] = indeks_swiec(s)
        panele[sym] = [{"t": s[4 * j]["t"], "f": cechy_kupuj(), "price": 100.0,
                        "low": min(x["low"] for x in s[4 * j:4 * j + 4]),
                        "high": max(x["high"] for x in s[4 * j:4 * j + 4])}
                       for j in range(n4h)]
    return panele, swiece


def _symuluj(panele, swiece, przebicie=0.0):
    egz = Egzekucja(limit=True, opoznienie_min=20, pesymistyczna=True,
                    spread_per_instrument=dict.fromkeys(panele, 10.0),
                    przebicie_bps=przebicie)
    return simulate_portfolio(
        panele, DEFAULT_WEIGHTS, dict.fromkeys(panele, 2.0),
        dict.fromkeys(panele, (0.05, 0.02)), H4, 0.05, LIMITY_LUZNE, kind="spot",
        egzekucja=egz, swiece_wykonania=swiece)


def _wypelnialnosc(res):
    return res.wypelnione_wejscia / (res.wypelnione_wejscia + res.niewypelnione_wejscia)


def test_kalibracja_na_danych_testowych_sprowadza_symulacje_do_demo():
    """Kryterium H-5 na rynku syntetycznym: przed kalibracją różnica > 10 pp,
    po kalibracji (przebicie z głębokości tych samych prób, drugi przebieg
    z tym przebiciem) ≤ 10 pp. Drugi przebieg ma INNE próby - wejście, które
    nie weszło, ponawia się na następnej świecy - więc zgodność nie jest
    tożsamością, tylko sprawdzeniem, że kalibracja przenosi się na przebieg."""
    panele, swiece = _rynek(40, 12)
    przed = _symuluj(panele, swiece)
    proby = przed.wypelnione_wejscia + przed.niewypelnione_wejscia
    assert proby >= 40
    assert len(przed.glebokosci_wejsc) == proby       # świece są wszędzie
    assert _wypelnialnosc(przed) - DEMO > 0.10

    x = kalibruj_przebicie(przed.glebokosci_wejsc, proby, DEMO)
    po = _symuluj(panele, swiece, x)
    assert x > 0
    assert abs(_wypelnialnosc(po) - DEMO) <= 0.10
    assert _wypelnialnosc(po) < _wypelnialnosc(przed)


def test_przebicie_zero_to_model_sprzed_zmiany():
    """Regresja: nowe pole w wartości domyślnej nie zmienia ani jednej transakcji."""
    panele, swiece = _rynek(8, 10)
    a = _symuluj(panele, swiece)
    b = _symuluj(panele, swiece, 0.0)
    assert a.trades == b.trades and a.equity_curve == b.equity_curve


def test_przebicie_nie_dotyczy_wyjsc():
    """Wyjście na produkcji po dwóch odrzuceniach PostOnly idzie takerem;
    przebicie skalibrowane na wejściach nie może go utrudniać. Jedyne miejsce,
    które czyta przebicie, stoi za `wejscie` - wyjścia wołają
    `zlecenie_z_limitem` bez tej flagi."""
    zrodlo = inspect.getsource(simulate_portfolio)
    assert zrodlo.count("przebicie_bps") == 1
    assert zrodlo.count("wejscie=True") == 1
    assert zrodlo.count("zlecenie_z_limitem(") == 3     # definicja, wyjście, wejście


def test_metryki_rozdzielaja_wejscia_od_wyjsc():
    """Do 11.10 trener zestawiał z demo sumę wejść i wyjść; H-5 mówi o wejściach."""
    panele, swiece = _rynek(10, 8)
    m = metrics(_symuluj(panele, swiece), min_trades=1)
    assert "wypelnione_wejscia" in m and "niewypelnione_wejscia" in m
    assert m["wypelnione"] >= m["wypelnione_wejscia"]


# ------------------------------------------------------------ trener

def test_trener_kalibruje_z_przebiegu_bez_przebicia():
    z = tr.Przebieg()
    z.glebokosci_wejsc_oos = [float(g) for g in range(-19, 81)]
    z.wypelnione_wejsc_oos, z.niewypelnione_wejsc_oos = 81, 19
    x, linie = tr.kalibracja_wypelnien(z, DEMO)
    assert x > 0
    assert any("81.0%" in linia and "65.6%" in linia for linia in linie)


def test_log_trenera_porownuje_z_demo_WEJSCIA_a_nie_sume():
    z = tr.Przebieg()
    z.wypelnione_oos, z.niewypelnione_oos = 1472, 399        # przebieg 11.10
    z.wypelnione_wejsc_oos, z.niewypelnione_wejsc_oos = 700, 300
    linie = tr.opis_wypelnialnosci_wejsc(z, Egzekucja(limit=True, pesymistyczna=True,
                                                      opoznienie_min=20))
    assert "70.0%" in linie[0] and "65.6%" in linie[0] and "42/64" in linie[0]
    assert "78.7%" not in " ".join(linie)
    zrodlo = inspect.getsource(tr.wypisz_podsumowanie)
    assert "Produkcja demo: ok. 65%" not in zrodlo
    # Equity nie ma pomiaru demo - nie wolno mu podsuwać liczby z crypto.
    akcje = tr.opis_wypelnialnosci_wejsc(z, Egzekucja(limit=True), "stock")
    assert "65.6%" not in " ".join(akcje) and "70.0%" in akcje[0]


def test_kalibracja_to_konfiguracja_w_params_i_klasa_miary():
    egz = Egzekucja(limit=True, pesymistyczna=True, opoznienie_min=20)
    stare = tr.params_przebiegu(H4, 60, "ic", "spot_eu", True, egz, "dluga")
    from dataclasses import replace
    kal = replace(egz, wypelnialnosc_docelowa=0.656)
    nowe = tr.params_przebiegu(H4, 60, "ic", "spot_eu", True, kal, "dluga")
    assert "wypelnialnosc_docelowa" not in stare
    assert nowe["wypelnialnosc_docelowa"] == 0.656
    assert "+wypelnialnosc_docelowa" in tr.zmiana_struktury(stare, nowe)
    # Przebicie wychodzi z danych co przebieg - NIE jest kluczem `params`,
    # inaczej każdy tydzień byłby nową konfiguracją w N.
    z_przebiciem = replace(kal, przebicie_bps=12.5)
    assert tr.params_przebiegu(H4, 60, "ic", "spot_eu", True, z_przebiciem, "dluga") == nowe

    from services.engine.dozor import SQL_STAN_NAUKI, klasa_miary
    from services.engine.koszty import karta_rundy
    zn = tr.znacznik_miary(karta_rundy("spot_eu", maker=True), kal)
    assert zn["wypelnialnosc_docelowa"] == 0.656
    assert "m.metric = 'wypelnialnosc_docelowa'" in SQL_STAN_NAUKI
    wiersz = dict(prowizja_pb=50.0, egzekucja_pesymistyczna=1, opoznienie_min=20)
    assert klasa_miary(dict(wiersz, wypelnialnosc_docelowa=0.656)) != klasa_miary(wiersz)
    assert klasa_miary(dict(wiersz, wypelnialnosc_docelowa=0)) == klasa_miary(wiersz)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")
def test_chart_kalibruje_tylko_tory_crypto():
    """Pomiar 42/64 to wejścia crypto; equity nie ma pomiaru na koncie."""
    w = subprocess.run(["helm", "template", str(KORZEN / "charts/trader"),
                        "-s", "templates/trainer-cron.yaml"],
                       capture_output=True, text=True)
    assert w.returncode == 0, w.stderr
    wywolania = [linia for linia in re.sub(r"\\\n", " ", w.stdout).splitlines()
                 if linia.strip().startswith("python services/engine/train.py")]
    crypto = [x for x in wywolania if "--engine equity" not in x]
    equity = [x for x in wywolania if "--engine equity" in x]
    assert len(crypto) == 2 and equity
    assert all('--wypelnialnosc-docelowa "0.656"' in x for x in crypto)
    assert not any("--wypelnialnosc-docelowa" in x for x in equity)


def _main(monkeypatch, *argv):
    import sys
    monkeypatch.setattr(sys, "argv", ["train.py", *argv])
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as e:
        tr.main()
    return str(e.value)


def test_cel_kalibracji_tylko_przy_pelnym_modelu_pesymistycznym(monkeypatch):
    assert "--wypelnialnosc-docelowa" in _main(
        monkeypatch, "--kind", "spot_eu", "--limit", "--wypelnialnosc-docelowa", "0.656")
    assert "ulamek" in _main(
        monkeypatch, "--kind", "spot_eu", "--limit", "--egzekucja-pesymistyczna",
        "--opoznienie", "20", "--wypelnialnosc-docelowa", "65.6")
    assert "DATABASE_URL" in _main(
        monkeypatch, "--kind", "spot_eu", "--limit", "--egzekucja-pesymistyczna",
        "--opoznienie", "20", "--wypelnialnosc-docelowa", "0.656")
