"""Refaktor `train.train` na czyste funkcje nie zmienia wyniku (audyt Fable T2).

ZRZUT REFERENCYJNY (`tests/fixtures/trener_rownowaznosc.json`) zostal
wygenerowany PRZED refaktorem, na kodzie 4358d81 (commit b08a2b5), z deterministycznego panelu
syntetycznego ponizej (stale ziarno, zero polaczen z baza - `train(None, ...,
panel=plik)`). Test porownuje z nim CALY wydruk trenera i slownik
`PODSUMOWANIE_JSON` dla kilku wariantow, ktore przechodza przez rozne galezie
`train()`: wybor `ic`, `argmax` (losowanie z ziarnem), `bazowe`, prog wspolny
i model wypelnien pesymistyczny.

PRZEGENEROWANY RAZ, w T4 (progi na wagach IC, audyt Fable F-2.1): warianty
`ic*` zmienily sie wylacznie przez progi liczone na `best_w` i klucz
`progi_na_wagach` w `params` (sprawdzone: z progami na `bazowe` i bez klucza
wynik byl identyczny ze zrzutem z b08a2b5); `argmax` i `bazowe` bez zmian.
Doszedl wariant `ic_siatka` - siatka zakotwiczona i piec przesuniec.

PRZEGENEROWANY DRUGI RAZ, 2026-10-07 (H-5, H-6, H-7 w docs/hipotezy.md),
SWIADOMIE, bo test pilnowal starej decyzji: na `spot_eu` (bez shortow) progi
i wagi IC liczyly sie dwustronnie. Sprawdzone dwuetapowo przed zapisem:
  1. z H-6 wylaczonym (`strona_uczenia` -> None) KAZDY wariant dawal ten sam
     JSON i ten sam wydruk co zrzut z T4, poza nowymi kluczami
     (`strona`, `trzymanie_swiec`, `horyzont_etykiety_swiec`) i nowymi liniami
     logu (uczenie/horyzont etykiety, czas trzymania) - czyli H-5 (flaga
     w CLI) i H-7 (pomiar w logu) nie zmieniaja wyniku;
  2. z H-6 wlaczonym zmieniaja sie wszystkie warianty (wszystkie sa na
     `spot_eu`) - to jest zamierzona zmiana strategii, nie regresja.
Doszedl wariant `ic_stock` (rynek z shortami): pilnuje, ze tam uczenie
zostaje dwustronne i `params` nie dostaja klucza `strona`.

PRZEGENEROWANY TRZECI RAZ, 2026-10-07 (para produkcyjna z ostatniego okna,
`train.parametry_produkcji`): zmienia sie WYLACZNIE linia „Progi wejscia dla
produkcji" (i dopisek o rezimie bez progu w ostatnim oknie w `ic_siatka`).
Sprawdzone przed zapisem: `PODSUMOWANIE_JSON` kazdego wariantu przechodzi
`_bliskie` ze zrzutem sprzed zmiany, a pozostale linie wydruku sa identyczne -
walk-forward i miary OOS bez zmian, inna jest tylko para, ktora dostaje
produkcja.

Zmiana wyniku zamierzona (nowa miara, poprawka) oznacza swiadome
przegenerowanie zrzutu:

    .venv/bin/python -B tests/test_trener_rownowaznosc.py --zapisz

i opis w commicie, CO sie zmienilo i dlaczego. Zrzut przegenerowany "zeby
test przeszedl" przestaje cokolwiek chronic.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.engine import panel_plik  # noqa: E402
from services.engine import train as tr  # noqa: E402
from services.engine.horizons import get as get_horizon  # noqa: E402
from services.engine.portfolio import Egzekucja  # noqa: E402

from panel_syntetyczny import DNI, plik_syntetyczny  # noqa: E402

ZRZUT = Path(__file__).parent / "fixtures" / "trener_rownowaznosc.json"
H4 = get_horizon("4h")

# nazwa -> argumenty `train()` poza panelem
WARIANTY = {
    "ic": dict(egzekucja=Egzekucja(limit=True), wybor="ic"),
    "argmax": dict(egzekucja=Egzekucja(limit=True), wybor="argmax", trials=3),
    "bazowe": dict(egzekucja=Egzekucja(), wybor="bazowe"),
    "ic_wspolny": dict(egzekucja=Egzekucja(limit=True), wybor="ic",
                       progi_per_rezim=False),
    "ic_pesymistyczna": dict(egzekucja=Egzekucja(limit=True, opoznienie_min=20,
                                                 pesymistyczna=True), wybor="ic"),
    # Rynek Z SHORTAMI (H-6): uczenie dwustronne jak przed 2026-10-07.
    "ic_stock": dict(egzekucja=Egzekucja(limit=True), wybor="ic", kind="stock"),
    # Siatka produkcji (T4): kotwica w kalendarzu i piec przesuniec. `days`
    # o 2 krotsze niz plik - zapas na przesuniecie -2.
    "ic_siatka": dict(egzekucja=Egzekucja(limit=True), wybor="ic", days=DNI - 2,
                      od=tr.KOTWICA_SIATKI, przesuniecia=tr.PRZESUNIECIA_SIATKI),
}


def przebieg(plik: dict, wariant: str) -> dict:
    a = dict(WARIANTY[wariant])
    trials = a.pop("trials", 2)
    dni = a.pop("days", DNI)
    kind = a.pop("kind", "spot_eu")
    wydruk = io.StringIO()
    with contextlib.redirect_stdout(wydruk):
        wynik = tr.train(None, "crypto", dni, trials, H4, kind, sucho=True,
                         panel=plik, **a)
    # Przez JSON: krotki -> listy, tak jak w zrzucie.
    return {"json": json.loads(json.dumps(wynik)), "stdout": wydruk.getvalue()}


@pytest.fixture(scope="module")
def plik(tmp_path_factory):
    sciezka = tmp_path_factory.mktemp("rownowaznosc") / "panel.json.gz"
    panel_plik.zapisz(sciezka, plik_syntetyczny())
    return panel_plik.wczytaj(sciezka)


@pytest.fixture(scope="module")
def zrzut():
    return json.loads(ZRZUT.read_text(encoding="utf-8"))


# TOLERANCJA ZMIENNOPRZECINKOWA, NIE ROWNOSC BITOWA. Ta sama logika na innej
# wersji Pythona/numpy (CI: 3.12, lokalnie: 3.14) sumuje w innej kolejnosci
# i rozni sie na 15-16 cyfrze znaczacej (zmierzone 2026-10-03: 2.4994552304595206
# vs ...214). Refaktor, ktory zmienia logike, przesuwa wyniki o rzedy wiecej,
# wiec 1e-9 wzglednie nadal go lapie.
_LICZBA = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")


def _bliskie(a, b, sciezka="") -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        assert set(a) == set(b), f"{sciezka}: klucze {sorted(set(a) ^ set(b))}"
        for k in a:
            _bliskie(a[k], b[k], f"{sciezka}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"{sciezka}: dlugosc {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            _bliskie(x, y, f"{sciezka}[{i}]")
    elif (isinstance(a, float) or isinstance(b, float)) and not isinstance(a, bool):
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), f"{sciezka}: {a} != {b}"
    else:
        assert a == b, f"{sciezka}: {a!r} != {b!r}"


def _liczba(m) -> str:
    v = float(m.group())
    # Ta sama tolerancja bezwzgledna co w `_bliskie`: DSR rzedu 1e-14 (wariant
    # `ic_pesymistyczna`) rozni sie miedzy wersjami Pythona juz na trzeciej
    # cyfrze znaczacej (1.21194875e-14 vs 1.2101431e-14 lokalnie na 3.14) - to
    # zero numeryczne, nie wynik. JSON przechodzil, wydruk nie.
    return "0" if abs(v) < 1e-12 else f"{v:.9g}"


def _znormalizuj(tekst: str) -> list[str]:
    return [_LICZBA.sub(_liczba, w) for w in tekst.splitlines()]


@pytest.mark.parametrize("wariant", sorted(WARIANTY))
def test_wynik_identyczny_z_referencja(plik, zrzut, wariant):
    teraz = przebieg(plik, wariant)
    ref = zrzut[wariant]
    _bliskie(teraz["json"], ref["json"])
    assert _znormalizuj(teraz["stdout"]) == _znormalizuj(ref["stdout"])


def test_referencja_ma_okna_i_transakcje(zrzut):
    """Zrzut, ktory nie ma okien, niczego by nie chronil."""
    for wariant, ref in zrzut.items():
        assert ref["json"]["okna"] >= 4, wariant
        assert ref["json"]["transakcji"] >= 100, wariant


def test_zadna_funkcja_trenera_nie_ma_ponad_200_linii():
    """`train()` jest orkiestratorem, nie miejscem na logike (audyt Fable T2):
    mierzone AST-em, razem z docstringiem i komentarzami."""
    import ast
    drzewo = ast.parse(Path(tr.__file__).read_text(encoding="utf-8"))
    dlugosci = {n.name: n.end_lineno - n.lineno + 1 for n in ast.walk(drzewo)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    za_dlugie = {n: d for n, d in dlugosci.items() if d > 200}
    assert not za_dlugie, f"funkcje dluzsze niz 200 linii: {za_dlugie}"
    assert dlugosci["train"] <= 200


if __name__ == "__main__":
    if "--zapisz" not in sys.argv:
        sys.exit("uzycie: tests/test_trener_rownowaznosc.py --zapisz")
    import tempfile
    with tempfile.TemporaryDirectory() as kat:
        sciezka = Path(kat) / "panel.json.gz"
        panel_plik.zapisz(sciezka, plik_syntetyczny())
        p = panel_plik.wczytaj(sciezka)
    ZRZUT.parent.mkdir(exist_ok=True)
    wynik = {w: przebieg(p, w) for w in sorted(WARIANTY)}
    ZRZUT.write_text(json.dumps(wynik, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")
    for w, r in wynik.items():
        print(w, r["json"]["okna"], r["json"]["transakcji"], r["json"]["status"])
