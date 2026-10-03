"""Czujka świeżości musi objąć KAŻDY tor i KAŻDY horyzont, na którym coś się liczy.

CO BYŁO NIE TAK — DWIE RUNDY, TEN SAM KSZTAŁT

RUNDA 1 (2026-09-25). `zywotnosc-cron.yaml` wołał `dozor.py --co swiezosc` JEDEN
raz i bez `--bar`, czyli z wartością domyślną `4h` (`dozor.main`). Produkcja
liczy cechy na dwóch horyzontach (`workers.features`: "4h,1h") i trenuje oba
(`trainerCron.horyzonty`). Tor 1h nie był objęty ANI RAZ.

RUNDA 2 (2026-09-27). To samo wywołanie szło też bez `--engine`, czyli
z domyślnym `crypto`. `sprawdz_swiezosc` filtruje po silniku od 2026-09-24 — więc
od tego dnia filtr działał poprawnie i wykluczał equity ze WSZYSTKICH wywołań,
jakie istniały. Potok equity mógł stać tygodniami i ta czujka nie zauważyłaby
tego ani razu.

DLACZEGO TO NIE JEST FORMALNOŚĆ

Oba horyzonty crypto składają się ze świec ŹRÓDŁOWYCH `1h`, więc członu
`market.candle` to nie dotyczy — wypada identycznie. Ale próg dla
`feat.feature_value` liczy się z `h.bar_minutes` (`sprawdz_swiezosc` podaje
`min_bar`), czyli 240 minut dla świecy 4h i 60 dla 1h. Potok cech 1h może więc
stać ponad trzy godziny i być w całości PRZEZROCZYSTY dla sprawdzenia liczonego
dla 4h.

Rozjazd tego samego kształtu — horyzont dołożony do trenera i nie dołożony do
cech — kosztował 21 godzin martwych cech 2026-09-04, przy zerowym liczniku
restartów.

PĘTLE HELMA SĄ TU ROZWIJANE, NIE POMIJANE

`manifesty()` w `test_chart_nadzor.py` zamienia akcje Helma na napis `SZABLON`,
bo pyta o pola, dla których podstawienie jest bez znaczenia. Tutaj podstawienie
JEST treścią pytania: test, który widzi jedno wywołanie także wtedy, gdy Helm
wygeneruje ich trzy, nie umie odróżnić poprawki od jej braku. Zagnieżdżone
`range` po torach i po horyzontach są więc mnożone tak, jak zrobi to Helm —
a `test_rozwijanie_petli_ZGADZA_SIE_z_renderem_chartu` przykłada wynik do prawdziwego
rendera chartu, kiedy binarka jest dostępna.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = Path(os.environ.get("CHART_TRADER", KORZEN / "charts/trader"))
VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
ZYWOTNOSC = CHART / "templates/zywotnosc-cron.yaml"

_KOMENTARZ_HELMA = re.compile(r"\{\{-?/\*.*?\*/-?\}\}", re.S)
_AKCJA_HELMA = re.compile(r"\{\{-?.*?-?\}\}", re.S)
_RANGE = re.compile(r"\{\{-?\s*range\s+\$(\w+)\s*:=\s*(.+?)\s*-?\}\}")
_END = re.compile(r"\{\{-?\s*end\s*-?\}\}")

# Nazwy zmiennych pętli Helma, które ten parser umie rozwinąć.
PETLA_TORY = "$tory"
PETLA_HORYZONTY = "$tor.horyzonty"
WYR_SILNIK = "$tor.engine"
WYR_SWIECA = "$h"
# KSZTALT SPRZED 2026-09-27, rozwijany CELOWO. Petla po samych horyzontach, bez
# torow - wtedy `--engine` nie bylo podawane wcale, czyli szlo domyslne `crypto`.
#
# Parser musi go rozumiec, zeby kanarek MOWIL, co jest nie tak. Uruchomiony
# przeciw chartowi sprzed zmiany (CHART_TRADER na kopie z HEAD) ma padac na
# zdaniu "swiezosc sprawdzana dla ['crypto'], a torow jest ['crypto','equity']",
# a nie na "nieznana petla w szablonie" - drugie jest prawdziwe i bezuzyteczne.
PETLA_HORYZONTY_STARA = ".Values.trainerCron.horyzonty | default (list .Values.trainerCron.bar)"
ZNANE_PETLE = (PETLA_TORY, PETLA_HORYZONTY, PETLA_HORYZONTY_STARA)


def _horyzonty_crypto() -> list[str]:
    return VALUES["trainerCron"].get("horyzonty") or [VALUES["trainerCron"]["bar"]]


def tory() -> list[dict]:
    """Tory (silnik + jego horyzonty) — TAK SAMO, jak składa je szablon.

    Lista NIE JEST tu przepisana: każdy horyzont pochodzi z `trainerCron`, czyli
    z tego samego miejsca, które czyta szablon i które `test_petle_w_serwisach`
    przykłada do `workers.features`. Własna kopia byłaby trzecim miejscem z tą
    listą i rozjechałaby się cicho.
    """
    wynik = [dict(engine="crypto", horyzonty=_horyzonty_crypto())]
    # Equity jest pilnowane, gdy chodzi jego potok cech - nie gdy chodzi trening
    # (trening bywa wstrzymany, a potok i silnik dalej pracuja; 2026-10-03).
    if VALUES["workers"]["features-equity"]["enabled"]:
        rowne = VALUES["trainerCron"]["equity"]["horyzonty"]
        wynik.append(dict(engine="equity", horyzonty=rowne))
    return wynik


def _podstaw(wiersz: str, wartosci: dict) -> str:
    out = wiersz
    for wyrazenie, wartosc in wartosci.items():
        wzor = r"\{\{-?\s*" + re.escape(wyrazenie) + r"\s*(\|\s*quote\s*)?-?\}\}"
        out = re.sub(wzor, chr(34) + wartosc + chr(34), out)
    return _AKCJA_HELMA.sub("SZABLON", out)


def wywolania(co: str) -> list[str]:
    """Wiersze `dozor.py --co <co>` z szablonu, z ROZWINIĘTYMI pętlami.

    Zagnieżdżenie jest w tym pliku dwupoziomowe (tory, a w środku horyzonty
    tego toru) i tylko takie rozwijamy. Pętla o innym kształcie kończy się
    WYJĄTKIEM, a nie milczącym pominięciem: parser, który nie rozumie
    szablonu, ma o tym powiedzieć, zamiast oddać krótszą listę i uspokoić
    wszystkie testy niżej.
    """
    tekst = _KOMENTARZ_HELMA.sub("", ZYWOTNOSC.read_text(encoding="utf-8"))
    stos: list = []
    wynik: list = []
    for wiersz in tekst.splitlines():
        goly = wiersz.strip()
        if goly.startswith("{{") and _AKCJA_HELMA.fullmatch(goly):
            m = _RANGE.match(goly)
            if m:
                zrodlo = m.group(2)
                assert zrodlo in ZNANE_PETLE, (
                    f"nieznana pętla w szablonie: {goly!r} — dopisz jej "
                    f"rozwinięcie, inaczej ten test policzy za mało wywołań")
                stos.append(zrodlo)
            elif _END.match(goly) and stos:
                stos.pop()
            continue
        if f"--co {co}" not in goly:
            continue
        if not stos:
            wynik.append(_podstaw(goly, {}))
        elif stos == [PETLA_TORY]:
            for t in tory():
                wynik.append(_podstaw(goly, {WYR_SILNIK: t["engine"]}))
        elif stos == [PETLA_HORYZONTY_STARA]:
            for h in _horyzonty_crypto():
                wynik.append(_podstaw(goly, {WYR_SWIECA: h}))
        elif stos == [PETLA_TORY, PETLA_HORYZONTY]:
            for t in tory():
                for h in t["horyzonty"]:
                    para = {WYR_SILNIK: t["engine"], WYR_SWIECA: h}
                    wynik.append(_podstaw(goly, para))
        else:
            raise AssertionError(f"nieobsłużone zagnieżdżenie {stos}: {goly!r}")
    return wynik


def _arg(wiersz: str, nazwa: str, domyslnie: str) -> str:
    """Wartość argumentu wywołania. Brak argumentu = wartość domyślna z CLI."""
    m = re.search(rf"--{nazwa}\s+\"?([\w-]+)\"?", wiersz)
    return m.group(1) if m else domyslnie


def objete(co: str) -> set:
    """Pary (silnik, świeca), które te wywołania naprawdę obejmują."""
    pary = set()
    for w in wywolania(co):
        pary.add((_arg(w, "engine", "crypto"), _arg(w, "bar", "4h")))
    return pary


# ======================================================== rozwijanie pętli
#
# Bez tego kanarka wszystkie testy niżej mierzą wynik parsera, nie szablonu.

def test_rozwijanie_petli_ZGADZA_SIE_z_renderem_chartu():
    """Parser wyżej kontra prawdziwy render szablonu.

    Pomija się, gdy nie ma binarki — i dlatego NIE JEST jedynym testem w tym
    pliku, a tylko jego uziemieniem. Test, który się pomija, niczego nie
    pilnuje; test, który mierzy własny parser, pilnuje parsera.
    """
    if not shutil.which("helm"):
        pytest.skip("brak binarki helm")
    polecenie = ["helm", "template", str(CHART)]
    out = subprocess.run(polecenie, capture_output=True, text=True, check=True)
    for co in ("swiezosc", "instrumenty", "poslizg"):
        z_rendera = set()
        for w in out.stdout.splitlines():
            if f"--co {co}" in w and w.strip().startswith("python"):
                silnik = _arg(w, "engine", "crypto")
                z_rendera.add((silnik, _arg(w, "bar", "4h")))
        assert z_rendera == objete(co), (
            f"--co {co}: render daje {sorted(z_rendera)}, a parser tego pliku "
            f"widzi {sorted(objete(co))}")


# ==================================================== świeżość: horyzonty

def test_swiezosc_sprawdzana_dla_KAZDEGO_trenowanego_horyzontu():
    """Sedno poprawki 11h — teraz PER TOR.

    Suma horyzontów po wszystkich torach nie wystarcza: gdyby crypto dostało
    4h i 1h, a equity nic, zbiór horyzontów i tak byłby pełny. Pytanie brzmi
    „czy TEN tor ma sprawdzany TEN horyzont".
    """
    pary = objete("swiezosc")
    assert pary, "zniknęła czujka świeżości danych"
    for t in tory():
        silnik = t["engine"]
        for h in t["horyzonty"]:
            assert (silnik, h) in pary, (
                f"tor {silnik} trenuje świecę {h}, a świeżość tej pary nie "
                f"jest sprawdzana: {sorted(pary)}. Tor bez czujki może stać "
                f"i wygląda wtedy jak rynek, który przestał dawać sygnały")


@pytest.mark.parametrize("worker,engine", [("features", "crypto"),
                                           ("features-equity", "equity")])
def test_czujka_swiezosci_pokrywa_tez_liste_cech(worker, engine):
    """Kanarek na trzecie i czwarte miejsce z tą samą listą.

    `workers.features` i `trainerCron.horyzonty` są już przez siebie pilnowane
    (`test_petle_w_serwisach`), ale czujka to trzecia lista — i gdyby była
    przepisana, rozjechałaby się cicho z obiema. Potok cech equity
    (`workers.features-equity`) jest czwartą i do 2026-09-27 nie pilnowało go
    nic: żadne wywołanie czujki nie podawało `--engine equity`.
    """
    cmd = VALUES["workers"][worker]["command"]
    cechy = set()
    for b in cmd[cmd.index("--bar") + 1].split(","):
        cechy.add(b.strip())
    pary = objete("swiezosc")
    for h in cechy:
        assert (engine, h) in pary, (
            f"{worker} liczy cechy {sorted(cechy)} dla {engine}, a świeżość "
            f"sprawdzana jest dla {sorted(pary)} — stojący potok jednego "
            f"horyzontu jest niewidoczny")


def test_prog_cech_ROZNI_SIE_miedzy_horyzontami():
    """To jest powód, dla którego jedno wywołanie nie wystarczało.

    Gdyby próg był wspólny, poprawka byłaby ozdobą: sprawdzenie dla 4h
    mówiłoby dokładnie to samo co dla 1h i osobne wywołanie nic by nie
    dodawało.
    """
    from services.engine.horizons import H1H, H4H

    assert H1H.source_timeframe == H4H.source_timeframe, (
        "świece źródłowe się rozjechały — wtedy różnic jest więcej niż ta, "
        "którą ten test opisuje")
    assert H1H.bar_minutes != H4H.bar_minutes, (
        "próg świeżości cech liczy się z `bar_minutes`; równe wartości "
        "znaczyłyby, że osobne wywołanie dla 1h nie mierzy niczego nowego")
    assert H1H.bar_minutes < H4H.bar_minutes, (
        "tor 1h ma CIAŚNIEJSZY próg, więc to on wypada z pola widzenia "
        "sprawdzenia liczonego dla 4h — nie odwrotnie")


# ======================================================== świeżość: tory

def test_swiezosc_OBEJMUJE_tor_equity():
    """Zamiana opisu stanu na WYMAGANIE (2026-09-27).

    Do tego dnia stał tu test o odwrotnej treści — „equity NADAL nie jest
    sprawdzane" — z adnotacją, że wolno go zamienić na wymaganie dopiero po
    sprawdzeniu, jak czujka zachowa się przy torze z sesjami. Sprawdzone
    i zmierzone: tests/test_dozorca_dwa_tory.py, sekcja CZUJKI.
    """
    silniki = set()
    for e, _ in objete("swiezosc"):
        silniki.add(e)
    wszystkie = set()
    for t in tory():
        wszystkie.add(t["engine"])
    assert silniki == wszystkie, (
        f"świeżość sprawdzana dla {sorted(silniki)}, a torów jest "
        f"{sorted(wszystkie)}")


# =================================================== kod wyjścia zadania

def test_kod_wyjscia_KAZDEGO_wywolania_jest_zbierany():
    """Wywołanie dołożone bez zbierania kodu nie zapala Joba, gdy czujka padnie.

    Tak właśnie wyglądała wada, którą ten chart naprawiał 2026-09-22: kod
    wyjścia dozoru nie był zbierany i wywrócona czujka kończyła się Jobem
    ZIELONYM.

    `--co poslizg` jest świadomym wyjątkiem i pilnuje go test niżej: to
    POMIAR, nie czujka, i nigdy nie wysyła powiadomienia.
    """
    for co in ("swiezosc", "instrumenty"):
        linie = wywolania(co)
        assert linie, f"zniknęła czujka --co {co}"
        for w in linie:
            assert "RC=" in w, f"wywołanie bez zbierania kodu wyjścia: {w!r}"


def test_poslizg_NADAL_nie_zapala_zadania():
    """Pomiar nie jest czujką — i nie wolno go w nią zamienić przez symetrię.

    `sprawdz_poslizg` zwraca zawsze 0, bo „niezerowy kod zapaliłby zadanie na
    czerwono, czyli podniósłby alarm bez treści". Dopisanie zbierania kodu do
    tej linii wyglądałoby na porządek, a znaczyłoby alarm o wielkości, dla
    której nie ma jeszcze żadnego progu — przy zerowej liczbie wypełnień na
    torze equity zapalałby się co dziesięć minut.
    """
    linie = wywolania("poslizg")
    assert linie, "zniknął pomiar poślizgu"
    for w in linie:
        assert "RC=" not in w, (
            f"poślizg dopisuje się do kodu wyjścia zadania: {w!r} — to raport, "
            f"nie czujka; próg alarmu nie istnieje")
