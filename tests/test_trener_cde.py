"""Trening na demo przed live: zmiany C, D, E (renacode-infra: docs/projekty/trader/hipotezy.md H-5, H-6, H-7).

  * H-5: `--egzekucja-pesymistyczna --opoznienie N` w przebiegu produkcyjnym
    (bez `--sucho`), WYLACZNIE jako pelny model; `params` to rozrozniaja,
    `trainerCron` przekazuje obie flagi do crypto i equity,
  * H-6: na rynku bez shortow prog wejscia z rozkladu score > 0, wagi IC
    z obserwacji, na ktorych long jest mozliwy; `params["strona"]`,
  * H-7: czas trzymania w swiecach w logu i JSON, `--horyzont-etykiety N`
    zapisywany w `params`, domyslna liczba bez zmian.

Kazda z tych zmian robi z pierwszej wersji `candidate` - testy pilnuja, ze
`zmiana_struktury` to widzi, bo inaczej zmiana weszlaby do produkcji po cichu.
"""
from __future__ import annotations

import json
import random
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from services.engine import train as tr
from services.engine.horizons import get as get_horizon
from services.engine.portfolio import Egzekucja
from services.engine.signals import REGIMES, SignalOutput

KORZEN = Path(__file__).resolve().parents[1]
H4 = get_horizon("4h")
H1 = get_horizon("1h")


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["train.py", *argv])
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as e:
        tr.main()
    return str(e.value)


def _params(kind="spot_eu", egz=None, h=H4):
    egz = egz or Egzekucja(limit=True)
    return tr.params_przebiegu(h, 60, "ic", kind, True, egz, tr.strona_uczenia(kind))


# ================================================================== H-5

def test_H5_pelny_model_pesymistyczny_przechodzi_BEZ_sucho(monkeypatch):
    """Decyzja wlasciciela 2026-10-06/07. Dalej odmawia juz brak bazy -
    czyli bramka argumentow przepuscila przebieg produkcyjny."""
    assert "DATABASE_URL" in _main(monkeypatch, "--kind", "spot_eu", "--limit",
                                   "--egzekucja-pesymistyczna", "--opoznienie", "20")


@pytest.mark.parametrize("flagi", [
    ("--egzekucja-pesymistyczna",),
    ("--opoznienie", "20"),
    ("--egzekucja-pesymistyczna", "--opoznienie", "0"),
], ids=["bez_opoznienia", "bez_pesymistycznej", "opoznienie_zero"])
def test_H5_polowka_modelu_dalej_wymaga_sucho(monkeypatch, flagi):
    """Intencja dawnej blokady zostaje dla wariantow, ktorych zaden pomiar nie
    opisal jako modelu zblizonego do produkcji."""
    assert "--sucho" in _main(monkeypatch, "--limit", *flagi)


def test_H5_polowka_modelu_z_sucho_przechodzi(monkeypatch):
    assert "DATABASE_URL" in _main(monkeypatch, "--limit", "--opoznienie", "20",
                                   "--sucho", "--hipoteza", "H-5")


def test_H5_model_pesymistyczny_bez_limitu_odmawia(monkeypatch):
    assert "--limit" in _main(monkeypatch, "--egzekucja-pesymistyczna",
                              "--opoznienie", "20")


def test_H5_params_rozrozniaja_model_wypelnien_i_zglaszaja_zmiane_struktury():
    """Licznik N (`n_konfiguracji`) i bramka struktury czytaja `params` -
    bez tych kluczy przebieg pesymistyczny bylby ta sama proba co optymistyczny
    i wszedlby do produkcji jak zwykly tygodniowy przebieg."""
    stare = _params()
    nowe = _params(egz=Egzekucja(limit=True, pesymistyczna=True, opoznienie_min=20))
    assert nowe["egzekucja_pesymistyczna"] is True
    assert nowe["opoznienie_min"] == 20
    zmiana = tr.zmiana_struktury(stare, nowe)
    assert zmiana and "+egzekucja_pesymistyczna" in zmiana and "+opoznienie_min" in zmiana
    assert tr.status_nowej_wersji("learning", "demo", False, zmiana)[0] == "candidate"
    # Inne opoznienie to inna konfiguracja.
    inne = _params(egz=Egzekucja(limit=True, pesymistyczna=True, opoznienie_min=30))
    assert "opoznienie_min: 20 → 30" in tr.zmiana_struktury(nowe, inne)


helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


def _wywolania_trenera() -> list[list[str]]:
    w = subprocess.run(["helm", "template", str(KORZEN / "charts/trader"),
                        "-s", "templates/trainer-cron.yaml"],
                       capture_output=True, text=True)
    assert w.returncode == 0, w.stderr
    skrypt = re.sub(r"\\\n", " ", w.stdout)
    return [shlex.split(linia.strip(), comments=True)
            for linia in skrypt.splitlines()
            if linia.strip().startswith("python services/engine/train.py")]


@helm
def test_H5_trainerCron_przekazuje_model_pesymistyczny_crypto_i_equity(monkeypatch):
    wywolania = _wywolania_trenera()
    silniki = {(a[a.index("--engine") + 1] if "--engine" in a else "crypto",
                a[a.index("--bar") + 1]) for a in wywolania}
    # Tor crypto 1h wylaczony 2026-10-18 (B-0 = STOP, `trainerCron.horyzonty`).
    assert silniki == {("crypto", "4h"), ("equity", "1h")}
    for argv in wywolania:
        assert "--egzekucja-pesymistyczna" in argv, argv
        assert argv[argv.index("--opoznienie") + 1] == "20", argv
        assert "--sucho" not in argv
        # Ten sam parser i ta sama bramka argumentow co w podzie: przebieg
        # z chartu nie moze konczyc sie odmowa "wymaga --sucho".
        assert "DATABASE_URL" in _main(monkeypatch, *argv[2:])


# ================================================================== H-6

@pytest.mark.parametrize("kind,strona", [("spot", "dluga"), ("spot_eu", "dluga"),
                                         ("stock", "dluga"), ("perp", None)])
def test_H6_strona_uczenia_z_tej_samej_flagi_co_symulacja(kind, strona):
    assert tr.strona_uczenia(kind) == strona
    p = _params(kind)
    assert p.get("strona") == strona


def test_H6_klucz_strona_zglasza_zmiane_struktury_tylko_tam_gdzie_shortow_nie_ma():
    """Rynek z shortami (`perp`) zachowuje swoje `params`. Tor equity od
    2026-10-18 (A-5) dostaje `strona: dluga` - aktywna wersja sprzed zmiany
    (bez klucza) widzi zmiane struktury, wiec nowa wersja idzie jako
    `candidate`, a nie cicho na miejsce aktywnej."""
    przed = {k: v for k, v in _params("spot_eu").items() if k != "strona"}
    assert "+strona" in tr.zmiana_struktury(przed, _params("spot_eu"))
    przed_stock = {k: v for k, v in _params("stock").items() if k != "strona"}
    assert "+strona" in tr.zmiana_struktury(przed_stock, _params("stock"))
    assert tr.zmiana_struktury(_params("perp"), _params("perp")) is None


def _panel(n):
    return [{"t": i, "price": 100.0, "f": {}} for i in range(n)]


def test_H6_proby_progu_z_wynikow_dodatnich_a_nie_z_modulu():
    """W rezimie, w ktorym duze wyniki sa ujemne, |score| podnosilo prog
    shortami, ktorych na spocie nikt nie zagra."""
    wyniki = [-0.9, -0.8, -0.7, 0.1, 0.2, 0.0]
    cache = {"AAA": [("trend_down", {"trend_follow": SignalOutput(s, 0.7, {})})
                     for s in wyniki]}
    wagi = {r: {"trend_follow": 1.0} for r in REGIMES}
    obie = tr.proby_wynikow({"AAA": _panel(6)}, wagi, {"AAA": 2.0}, H4, cache)
    dluga = tr.proby_wynikow({"AAA": _panel(6)}, wagi, {"AAA": 2.0}, H4, cache,
                             tylko_dluga=True)
    # `combine` skaluje wynik pewnoscia - porownujemy proporcje, nie liczby.
    skala = max(obie["trend_down"]) / 0.9
    assert sorted(obie["trend_down"]) == pytest.approx(
        [skala * x for x in (0.0, 0.1, 0.2, 0.7, 0.8, 0.9)])
    # Zero odpada: przy score == 0 nie ma wejscia w zadna strone.
    assert sorted(dluga["trend_down"]) == pytest.approx([skala * 0.1, skala * 0.2])


def _scena_dwoch_prorokow(n=600, ziarno=7):
    """`momentum_multi` przewiduje tylko SPADKI (wielkosc wyniku sledzi zwrot,
    gdy zwrot < 0; przy wzroscie wynik dodatni, ale losowy), `trend_follow`
    przewiduje tylko WZROSTY (lustrzanie). Dwustronne IC nie widzi miedzy nimi
    roznicy; dla longa liczy sie wylacznie drugi."""
    rng = random.Random(ziarno)
    ceny = [100.0]
    for _ in range(n + 5):
        ceny.append(ceny[-1] * (1 + rng.gauss(0, 0.01)))
    panel = [{"t": i, "price": c} for i, c in enumerate(ceny)]
    cache = []
    for i in range(len(ceny)):
        j = i + 1 + 3
        r = (ceny[j] / ceny[i + 1] - 1) if j < len(ceny) else 0.0
        if r < 0:
            spadki, wzrosty = max(-1.0, r * 30), -rng.uniform(0.05, 1.0)
        else:
            spadki, wzrosty = rng.uniform(0.05, 1.0), min(1.0, r * 30)
        cache.append(("range", {"momentum_multi": SignalOutput(spadki, 0.7, {}),
                                "trend_follow": SignalOutput(wzrosty, 0.7, {})}))
    return {"AAA": panel}, {"AAA": cache}


def test_H6_wagi_IC_dla_longa_nie_nagradzaja_przewidywania_spadkow():
    panels, cache = _scena_dwoch_prorokow()
    obie = tr.ic_weights(panels, cache, set(), 3)["range"]
    dluga = tr.ic_weights(panels, cache, set(), 3, tylko_dluga=True)["range"]
    # Dwustronnie oba sygnaly wygladaja podobnie...
    assert obie["momentum_multi"] > 0.25 and obie["trend_follow"] > 0.25
    # ...a dla longa liczy sie ten, ktory przewiduje wzrosty.
    assert dluga["trend_follow"] > 0.7
    assert abs(dluga["momentum_multi"]) < 0.3
    assert dluga["trend_follow"] - obie["trend_follow"] > 0.2


def test_H6_domyslnie_wagi_IC_bez_zmian():
    """`ic_weights` bez flagi = dokladnie wynik sprzed H-6 (diagnose_timing
    i rynki z shortami)."""
    panels, cache = _scena_dwoch_prorokow()
    assert (tr.ic_weights(panels, cache, set(), 3)
            == tr._wagi_z_ic(panels, cache, set(), 3))


# ================================================================== H-7

def test_H7_trzymanie_rozdziela_wyjscia_sygnalem_od_domknietych_koncem_okna():
    t = tr.trzymanie_transakcji([(1, False), (2, False), (3, False), (10, True)])
    assert t["n"] == 4 and t["ucietych_koncem_okna"] == 1
    assert t["sygnalem_q1_med_q3"][1] == 2.0
    assert t["wszystkie_q1_med_q3"][1] == 2.5
    assert tr.trzymanie_transakcji([]) is None
    linie = tr.opis_trzymania(t, H4)
    assert "horyzont etykiety IC 3 świec (12 h)" in "\n".join(linie)


def test_H7_kazda_transakcja_symulacji_niesie_liczbe_swiec():
    zrodlo = (KORZEN / "services/engine/portfolio.py").read_text(encoding="utf-8")
    assert zrodlo.count('"swiec":') == 2, (
        "liczba swiec trzymania w obu zapisach transakcji (wyjscie sygnalem "
        "i domkniecie koncem okna)")
    assert '"koniec_okna": True' in zrodlo


def test_H7_zrzut_rownowaznosci_ma_trzymanie_dla_kazdej_transakcji():
    zrzut = json.loads((KORZEN / "tests/fixtures/trener_rownowaznosc.json")
                       .read_text(encoding="utf-8"))
    for wariant, ref in zrzut.items():
        j = ref["json"]
        assert j["trzymanie_swiec"]["n"] == j["transakcji"], wariant
        assert j["horyzont_etykiety_swiec"] == H4.ic_horizon_bars, wariant
        assert "Czas trzymania (OOS" in ref["stdout"], wariant


def test_H7_domyslny_horyzont_etykiety_NIE_zmieniony():
    """Uczciwosc pomiaru wazniejsza niz ladny wynik: liczba zmienia sie
    dopiero po kroku 2 H-7, nie w tej zmianie."""
    assert H4.ic_horizon_bars == 3 and H1.ic_horizon_bars == 12
    assert "ic_horizon_bars" not in _params(h=H4)


def test_H7_inny_horyzont_etykiety_w_params_i_jako_zmiana_struktury():
    from dataclasses import replace
    stare = _params(h=H4)
    nowe = _params(h=replace(H4, ic_horizon_bars=6))
    assert nowe["ic_horizon_bars"] == 6
    assert "+ic_horizon_bars" in tr.zmiana_struktury(stare, nowe)
    # 1h: klucz juz jest, zmienia sie wartosc.
    assert "ic_horizon_bars: 12 → 6" in tr.zmiana_struktury(
        _params(h=H1), _params(h=replace(H1, ic_horizon_bars=6)))


def test_H7_flaga_horyzontu_etykiety(monkeypatch):
    assert "DATABASE_URL" in _main(monkeypatch, "--horyzont-etykiety", "6")
    assert ">= 1" in _main(monkeypatch, "--horyzont-etykiety", "0")
    with pytest.raises(ValueError):
        tr.train(None, "crypto", 10, 1, H4, panel={"do": None}, sucho=True,
                 horyzont_etykiety=0)


# ============================================================ rejestr
#
# Test "H-5, H-6, H-7 maja wiersz w rejestrze" usuniety 2026-10-16: rejestr
# hipotez przeniesiono do prywatnego repo (renacode-infra:
# docs/projekty/trader/hipotezy.md), ktorego CI nie widzi. Bez pliku
# `sprawdz_hipoteze` sprawdza tylko format, wiec test przechodzilby na pusto.
# Sama bramka rejestru ma test na rejestrze tymczasowym
# (tests/test_hipoteza_wymagana.py).
