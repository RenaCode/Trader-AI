"""Dokumentacja musi umieć powiedzieć, jak uruchomić testy — i nie kłamać o ich liczbie.

TRZY WADY O JEDNYM KSZTAŁCIE: napis, który był prawdziwy w dniu wpisania i nikt
go od tego czasu nie zmierzył.

  1. `README.md` nie zawierał ANI RAZ polecenia uruchamiającego `pytest`. Sekcja
     „Testy" opisywała wyłącznie test dymny interfejsu (`node tests/smoke.spec.mjs`),
     więc 1992 testy jednostkowe istniały w repozytorium bez instrukcji obsługi.
  2. `README.md` mówił „123 testy jednostkowe" przy faktycznych 1992 —
     szesnastokrotnie mniej. Liczba, która raz odjechała, nie wraca sama.
  3. `requirements-dev.txt` mówił „zależności wyłącznie do testów" i wymieniał
     sam `pytest`. Zdanie prawdziwe co do słowa, mylące co do skutku: świeży
     klon dostaje 59 błędów kolekcji, bo testy importują `services/*`, a te
     importują warstwę produkcyjną.

DLACZEGO LICZBA JEST PILNOWANA Z TOLERANCJĄ, A NIE NA RÓWNOŚĆ

Równość wymagałaby edycji README przy KAŻDYM dołożonym teście, więc pierwszy
człowiek w pośpiechu wyłączyłby ten test albo wpisałby „ok" na chybił trafił.
Tolerancja odpowiada na pytanie, które naprawdę tu stoi: czy ten napis jeszcze
opisuje rzeczywistość. Rozjazd 123 wobec 1992 to 1520% i zapala się od razu;
dwadzieścia nowych testów to 1% i nie zapala nic.

Liczba porównywana jest z TYM, CO ZEBRAŁ BIEŻĄCY PRZEBIEG (`request.session`),
a nie z drugim, własnym zliczaniem funkcji `test_*`. Parametryzacja sprawia, że
funkcji jest dużo mniej niż testów, więc własny licznik mierzyłby inną wielkość
niż ta, którą README podaje — i rozjechałby się z nią w drugą stronę.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
README = KORZEN / "README.md"
DEV = KORZEN / "requirements-dev.txt"
PROD = KORZEN / "services/api/requirements.txt"
CI = KORZEN / ".github/workflows/tests.yml"

# Poniżej tej liczby zebranych testów przebieg jest CZĘŚCIOWY (ktoś uruchomił
# jeden plik), więc porównywanie go z liczbą z README nie ma sensu.
PELNY_PRZEBIEG_OD = 100

# Dopuszczalny rozjazd napisu z pomiarem. 15% to około trzystu testów przy
# dzisiejszym zestawie — dość, żeby normalna praca nie zapalała testu, i o dwa
# rzędy wielkości mniej niż rozjazd, który tu zastaliśmy.
TOLERANCJA = 0.15


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _zadeklarowana_liczba() -> int:
    m = re.search(r"(\d[\d\s]*)\s*test(?:y|ów|ow)\s+jednostkow", _readme())
    assert m, ("README nie podaje liczby testów jednostkowych — nie ma czego "
               "porównać z pomiarem")
    return int(re.sub(r"\s", "", m.group(1)))


def test_README_podaje_polecenie_uruchamiajace_pytest():
    """Sedno pierwszej wady: testy bez instrukcji obsługi."""
    tekst = _readme()
    assert "pytest" in tekst, (
        "README nie wspomina `pytest` ANI RAZ, choć repozytorium ma prawie dwa "
        "tysiące testów jednostkowych")
    assert re.search(r"python\s+-B\s+-m\s+pytest", tekst), (
        "README nie podaje polecenia uruchamiającego zestaw. `-B` należy do "
        "polecenia, nie do ozdoby: bez niego mutacja pliku o niezmienionym "
        "rozmiarze trafia w stary `.pyc` i daje wynik nie z tego kodu")


def test_README_mowi_ze_potrzebne_sa_OBA_pliki_zaleznosci():
    """Druga połowa tej samej wady. Bez tego zdania świeży klon dostaje 59
    błędów kolekcji, a komunikat o brakującym module nie mówi, czego brakuje."""
    tekst = _readme()
    assert "services/api/requirements.txt" in tekst and "requirements-dev.txt" in tekst, (
        "README nie mówi, że do uruchomienia testów potrzebne są oba pliki "
        "zależności")


def test_polecenie_z_README_jest_TYM_SAMYM_co_w_CI():
    """Dwie różne instrukcje znaczyłyby, że jedna z nich nie jest sprawdzana.

    Sprawdzana jest ta z CI, więc rozjazd zostawiałby człowieka z poleceniem,
    którego nikt nie uruchamia.
    """
    ci = CI.read_text(encoding="utf-8")
    tekst = _readme()
    for plik in ("services/api/requirements.txt", "requirements-dev.txt"):
        assert plik in ci, f"CI nie instaluje {plik} — rozjazd z README"
    assert "-B -m pytest" in ci, "CI nie uruchamia pytest z `-B`, a README tak"
    assert "-B -m pytest" in tekst


def test_requirements_dev_NIE_twierdzi_ze_wystarcza_sam():
    """`zależności wyłącznie do testów` + sam `pytest` = zdanie prawdziwe co do
    słowa i mylące co do skutku."""
    tekst = DEV.read_text(encoding="utf-8")
    assert "services/api/requirements.txt" in tekst, (
        "requirements-dev.txt nie wskazuje pliku produkcyjnego, bez którego "
        "testy się nie zbiorą")
    assert re.search(r"wylacznie do testow|wyłącznie do testów", tekst) is None or \
        "services/api/requirements.txt" in tekst, (
        "opis nadal sugeruje, że ten plik wystarcza")


def test_requirements_produkcyjne_NIE_deklaruja_liczby_testow():
    """Liczba wpisana raz w plik zależności nie jest pomiarem.

    Stała tam liczba zaliczonych testów z datą 23.09.2026 — przy faktycznych
    ponad 1900 dwa dni później. Taki napis czyta się jak dowód zielonności TYCH
    wersji, a dowodem jest przebieg CI, który sam się datuje.
    """
    tekst = PROD.read_text(encoding="utf-8")
    twierdzenia = re.findall(r"\d[\d\s]*passed", tekst)
    assert not twierdzenia, (
        f"services/api/requirements.txt deklaruje wynik zestawu: {twierdzenia}. "
        f"Nikt tej liczby nie odświeża, a z każdym dniem coraz bardziej nie "
        f"odpowiada rzeczywistości")


def test_liczba_testow_w_README_zgadza_sie_z_POMIAREM(request):
    """Sedno drugiej wady, mierzone bieżącym przebiegiem.

    Pomijany, gdy ktoś uruchomił pojedynczy plik — wtedy „zebrane" nie opisuje
    zestawu i porównanie mówiłoby o czymś innym.
    """
    zebrane = len(request.session.items)
    if zebrane < PELNY_PRZEBIEG_OD:
        pytest.skip(f"przebieg częściowy ({zebrane} testów) — liczba z README "
                    f"opisuje CAŁY zestaw, uruchom `python -B -m pytest`")

    deklarowane = _zadeklarowana_liczba()
    rozjazd = abs(zebrane - deklarowane) / zebrane
    assert rozjazd <= TOLERANCJA, (
        f"README mówi „{deklarowane} testy jednostkowe”, a ten przebieg zebrał "
        f"{zebrane} — rozjazd {rozjazd:.0%} przy dopuszczalnych "
        f"{TOLERANCJA:.0%}. Popraw liczbę w README (sekcja „Testy”)")


def test_README_podaje_TAKZE_date_pomiaru():
    """Liczba bez daty nie da się sprawdzić po fakcie.

    „123 testy jednostkowe" nie mówiło, kiedy było prawdą, więc nie było jak
    zauważyć, że przestało — a to jest różnica między liczbą a pomiarem.
    """
    tekst = _readme()
    m = re.search(r"test(?:y|ów|ow)\s+jednostkow\w*[^\n]*\n?[^\n]*", tekst)
    assert m, "nie znaleziono wpisu o testach jednostkowych"
    assert re.search(r"\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}", m.group(0)), (
        f"wpis o liczbie testów nie podaje daty pomiaru: {m.group(0)!r}")


def test_pytest_q_wypisuje_linie_podsumowania(tmp_path):
    """`addopts = -q` w `pytest.ini` plus `-q` z linii poleceń daje `-qq`,
    a przy `-qq` pytest NIE wypisuje linii „N passed, M skipped”.

    Polecenie z komentarzy w testach (`pytest -q tests/...`) i z audytów kończyło
    się więc samymi kropkami — wynik trzeba było zgadywać z kodu wyjścia.
    Sprawdzone uruchomieniem, nie odczytem pliku: liczy się to, co widzi
    człowiek.
    """
    import subprocess
    import sys

    (tmp_path / "test_kanarek.py").write_text("def test_ok():\n    pass\n")
    wynik = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(KORZEN / "pytest.ini"), "--rootdir", str(tmp_path),
         str(tmp_path / "test_kanarek.py")],
        capture_output=True, text=True, timeout=60)
    assert wynik.returncode == 0, wynik.stdout + wynik.stderr
    assert "1 passed" in wynik.stdout, (
        f"`pytest -q` nie wypisał podsumowania:\n{wynik.stdout}")
