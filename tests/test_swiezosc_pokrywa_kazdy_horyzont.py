"""Czujka świeżości musi objąć KAŻDY horyzont, na którym coś się liczy.

CO BYŁO NIE TAK

`zywotnosc-cron.yaml` wołał `dozor.py --co swiezosc` JEDEN raz i bez `--bar`,
czyli z wartością domyślną `4h` (`dozor.main`). Produkcja liczy cechy na dwóch
horyzontach (`workers.features`: "4h,1h") i trenuje oba
(`trainerCron.horyzonty`). Tor 1h nie był objęty ANI RAZ.

DLACZEGO TO NIE JEST FORMALNOŚĆ

Oba horyzonty składają się ze świec ŹRÓDŁOWYCH `1h`, więc członu
`market.candle` to nie dotyczy — wypada identycznie. Ale próg dla
`feat.feature_value` liczy się z `h.bar_minutes` (`sprawdz_swiezosc` podaje
`min_bar`), czyli 240 minut dla świecy 4h i 60 dla 1h. Potok cech 1h może więc
stać ponad trzy godziny i być w całości PRZEZROCZYSTY dla sprawdzenia liczonego
dla 4h.

Rozjazd tego samego kształtu — horyzont dołożony do trenera i nie dołożony do
cech — kosztował 21 godzin martwych cech 2026-09-04, przy zerowym liczniku
restartów.

CZEGO TEN TEST CELOWO NIE OBEJMUJE

Toru equity. `--co swiezosc` nie dostaje `--engine equity` w żadnym wywołaniu,
więc świeżość danych equity nie jest dziś sprawdzana wcale — to OSOBNE
ustalenie, poza zakresem tej paczki, i nie wolno go tu przemilczeć ani zamknąć
po cichu.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = Path(os.environ.get("CHART_TRADER", KORZEN / "charts/trader"))
VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
ZYWOTNOSC = CHART / "templates/zywotnosc-cron.yaml"

_KOMENTARZ_HELMA = re.compile(r"\{\{-?/\*.*?\*/-?\}\}", re.S)
_AKCJA_HELMA = re.compile(r"\{\{-?.*?-?\}\}", re.S)


def _wywolania_swiezosci() -> list[str]:
    """Wiersze `--co swiezosc` z szablonu, z podstawionymi wartościami `--bar`.

    Pętla `{{- range }}` po horyzontach jest tu ROZWIJANA — inaczej test
    widziałby jedno wywołanie także wtedy, gdy Helm wygeneruje ich dwa, i nie
    umiałby odróżnić poprawki od jej braku.
    """
    tekst = _KOMENTARZ_HELMA.sub("", ZYWOTNOSC.read_text(encoding="utf-8"))
    horyzonty = (VALUES["trainerCron"].get("horyzonty")
                 or [VALUES["trainerCron"]["bar"]])
    wiersze: list[str] = []
    w_petli = False
    for wiersz in tekst.splitlines():
        goly = wiersz.strip()
        if goly.startswith("{{") and _AKCJA_HELMA.fullmatch(goly):
            if "range" in goly and "horyzonty" in goly:
                w_petli = True
            elif "end" in goly:
                w_petli = False
            continue
        if "--co swiezosc" not in goly:
            continue
        if w_petli:
            for h in horyzonty:
                wiersze.append(_AKCJA_HELMA.sub(h, goly))
        else:
            wiersze.append(_AKCJA_HELMA.sub("SZABLON", goly))
    return wiersze


def _bar(wiersz: str) -> str:
    """Świeca, o którą pyta to wywołanie. Brak `--bar` = wartość domyślna dozoru."""
    m = re.search(r"--bar\s+\"?([\w]+)\"?", wiersz)
    if m:
        return m.group(1)
    from services.engine import dozor          # noqa: F401  (import dla jasności)
    return "4h"                                # domyślna w `dozor.main`


def test_swiezosc_sprawdzana_dla_KAZDEGO_trenowanego_horyzontu():
    """Sedno poprawki 11h."""
    wywolania = _wywolania_swiezosci()
    assert wywolania, "zniknęła czujka świeżości danych"
    objete = {_bar(w) for w in wywolania}
    trenowane = set(VALUES["trainerCron"].get("horyzonty")
                    or [VALUES["trainerCron"]["bar"]])
    assert trenowane <= objete, (
        f"świeżość sprawdzana dla {sorted(objete)}, a trenowane są "
        f"{sorted(trenowane)}. Tor bez czujki może stać i wygląda wtedy jak "
        f"rynek, który przestał dawać sygnały")


def test_czujka_swiezosci_pokrywa_tez_liste_cech():
    """Kanarek na trzecie miejsce z tą samą listą.

    `workers.features` i `trainerCron.horyzonty` są już przez siebie pilnowane
    (`test_petle_w_serwisach`), ale czujka to trzecia lista — i gdyby była
    przepisana, rozjechałaby się cicho z obiema.
    """
    cmd = VALUES["workers"]["features"]["command"]
    cechy = {b.strip() for b in cmd[cmd.index("--bar") + 1].split(",")}
    objete = {_bar(w) for w in _wywolania_swiezosci()}
    assert cechy <= objete, (
        f"cechy liczone dla {sorted(cechy)}, a świeżość sprawdzana dla "
        f"{sorted(objete)} — stojący potok jednego horyzontu jest niewidoczny")


def test_kod_wyjscia_KAZDEGO_wywolania_jest_zbierany():
    """Wywołanie dołożone bez `|| RC=$?` nie zapala Joba, gdy czujka się wywróci.

    Tak właśnie wyglądała wada, którą ten chart naprawiał 2026-09-22: kod
    wyjścia dozoru nie był zbierany i wywrócona czujka kończyła się Jobem
    ZIELONYM.
    """
    for w in _wywolania_swiezosci():
        assert "RC=$?" in w, (
            f"wywołanie bez zbierania kodu wyjścia: {w!r}")


def test_prog_cech_ROZNI_SIE_miedzy_horyzontami():
    """To jest powód, dla którego jedno wywołanie nie wystarczało.

    Gdyby próg był wspólny, poprawka byłaby ozdobą: sprawdzenie dla 4h mówiłoby
    dokładnie to samo co dla 1h i osobne wywołanie nic by nie dodawało.
    """
    from services.engine.horizons import H1H, H4H

    assert H1H.source_timeframe == H4H.source_timeframe, (
        "świece źródłowe się rozjechały — wtedy różnic jest więcej niż ta, "
        "którą ten test opisuje")
    assert H1H.bar_minutes != H4H.bar_minutes, (
        "próg świeżości cech liczy się z `bar_minutes`; równe wartości znaczyłyby, "
        "że osobne wywołanie dla 1h nie mierzy niczego nowego")
    assert H1H.bar_minutes < H4H.bar_minutes, (
        "tor 1h ma CIAŚNIEJSZY próg, więc to on wypada z pola widzenia "
        "sprawdzenia liczonego dla 4h — nie odwrotnie")


def test_swiezosc_equity_NADAL_NIE_JEST_SPRAWDZANA_i_to_jest_zapisane():
    """Świadomie NIE naprawione w tej paczce — i dlatego zapisane w teście.

    `sprawdz_swiezosc` filtruje po silniku (naprawione 2026-09-24), ale żadne
    wywołanie w tym chartcie nie podaje `--engine equity`. Potok equity może
    więc stać i czujka tego nie zauważy — tak samo jak nie zauważała, dopóki
    zapytanie szło po całej tabeli.

    Test jest CELOWO napisany jako opis stanu, a nie jako wymaganie: nie wolno
    dołożyć wywołania dla equity bez sprawdzenia, jak `poza_sesja` zachowa się
    przy potoku, który jeszcze nie działa. Kiedy to ustalenie zostanie zamknięte,
    ten test ma PADNĄĆ i zostać zamieniony na wymaganie.
    """
    silniki = [w for w in _wywolania_swiezosci() if "--engine" in w]
    assert not silniki, (
        f"czujka świeżości dostała `--engine` ({silniki}) — jeśli to celowa "
        f"zmiana, zamień ten test na wymaganie pokrycia obu silników i sprawdź "
        f"zachowanie `poza_sesja` dla toru, który jeszcze nie handluje")
