"""WYKRES KAPITAŁU ŁĄCZNEGO MA PODAWAĆ JEDNOSTKĘ I POWÓD BRAKU SUMY.

CO BYŁO ZŁE

API liczy obie te rzeczy i oddaje je od dawna (`kapital_laczny.py:174` i `179`),
z komentarzem mówiącym wprost, po co:

    # Waluta PRZY KAZDEJ SERII, nie jedna na wykres. Bez niej dwie linie
    # w roznych jednostkach wygladaja na porownywalne.
    ...
    # `null` znaczy "suma jest policzona". Tekst znaczy "nie jest, i oto
    # dlaczego" - powod idzie na ekran, a nie do logu.

Front nie czytał ani jednego z tych pól: `grep -rn "razem_powod\\|waluty"
web/src/` nie dawał ANI JEDNEGO trafienia. Komponent brał tylko `d.silniki`,
`d.punkty` i `d.zmiany`.

Skutek był podwójny. Krypto mierzy dziś w USDC, a equity w USD (migracje 0023
i 0047), więc `razem` jest `null` i przy wierszu „Razem” stało samo
`brak danych` (`Zmiana`) — nie do odróżnienia od stanu „druga platforma
jeszcze nie ruszyła", o którym ta sama karta zapewnia zdanie niżej. Obie linie
były przy tym na jednej osi bez jednostki, dokładnie wbrew temu, co API
deklaruje w komentarzu. Właściciel czytał wykres jako porównanie dwóch
rachunków w tej samej walucie.

Strona serwera jest pokryta (`test_dane_bez_zer.py`:
`test_RAZEM_nie_sumuje_dwoch_roznych_walut` i dwa sąsiednie). Tu chodzi
o drugą połowę umowy — o to, czy ktokolwiek te pola czyta.
"""
from __future__ import annotations

import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
UI = (KORZEN / "web/src/components/KapitalLaczny.jsx").read_text(encoding="utf-8")
API = (KORZEN / "services/api/routers/kapital_laczny.py").read_text(encoding="utf-8")


# ==================================================== powód braku sumy na ekranie

def test_front_wypisuje_razem_powod():
    """Powód idzie na ekran, a nie do logu — tak mówi komentarz przy polu."""
    assert "razem_powod" in UI, (
        "komponent nie czyta `razem_powod`, więc przy wierszu „Razem” stoi samo "
        "`brak danych` — to samo, co pokazuje rachunek, który jeszcze nie ruszył")


def test_powod_jest_RENDEROWANY_a_nie_tylko_odczytany():
    """Odczyt do zmiennej, której nikt nie wypisuje, jest odczytem donikąd."""
    assert re.search(r"\{\s*d(?:ane\.data)?\??\.razem_powod\s*\}", UI), (
        "`razem_powod` nie trafia do JSX — pole jest brane, ale nie widać go "
        "na ekranie")


def test_obie_strony_umowy_nadal_istnieja():
    """Strażnik bez drugiej strony broni umowy, której już nie ma."""
    assert '"razem_powod": razem_powod' in API
    assert '"waluty": waluty' in API


# ==================================================== jednostka przy każdej serii

def test_kazda_seria_silnika_niesie_swoja_walute():
    """Dwie linie w różnych jednostkach na jednej osi wyglądają na porównywalne
    rachunki — i tak właśnie były czytane."""
    assert "waluty" in UI, "komponent w ogóle nie czyta walut z odpowiedzi"
    assert re.search(r"label:\s*`[^`]*\$\{[^}]*waluty?", UI) or \
        re.search(r"etykietaWaluty\s*\(", UI), (
        "etykieta serii nie zawiera waluty — legenda wykresu dalej mówi "
        "„Krypto (Bybit)” i „Giełda (Alpaca)” bez jednostki")


def test_os_dostaje_jednostke_TYLKO_gdy_jest_jedna_na_caly_wykres():
    """Podpisanie osi jedną z dwóch walut jest gorsze niż brak podpisu: druga
    linia dostaje wtedy cudzą jednostkę, i to bez żadnego zastrzeżenia."""
    assert "jednostkaOsi" in UI, "brak rozstrzygnięcia, czy oś ma jednostkę"
    warunek = re.search(r"jednostkaOsi\s*=\s*(.+?);", UI, re.S)
    assert warunek, "nie da się odczytać, kiedy oś dostaje jednostkę"
    tresc = warunek.group(1)
    assert "length === 1" in tresc, (
        "oś dostaje jednostkę także wtedy, gdy walut jest więcej niż jedna")
    assert "every" in tresc, (
        "oś dostaje jednostkę mimo silnika o NIEZNANEJ walucie — a nieznana "
        "jednostka nie jest tą samą jednostką co znana")


def test_valueFmt_korzysta_z_ustalonej_jednostki():
    """Bez tego rozstrzygnięcie o jednostce osi nie zmienia niczego na ekranie."""
    assert re.search(r"valueFmt=\{[^}]*jednostkaOsi", UI, re.S), (
        "`valueFmt` nie używa ustalonej jednostki — liczby na osi zostają bez "
        "waluty niezależnie od tego, czy da się ją podać")
