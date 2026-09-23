"""Z jakiego kodu zbudowano proces, ktory wlasnie cos zapisuje.

DLACZEGO TO W OGOLE JEST POTRZEBNE

`learn.model_version.git_sha` istnieje od migracji 0006 i we wszystkich 131
wierszach na produkcji jest NULL. Skutek: przy wynikach modelu nie da sie
powiedziec, JAKI KOD je policzyl. Rozjazd trening-produkcja zdarzyl sie w tym
projekcie juz kilka razy (prowizja perpetuali na spocie, prog liczony z pelnych
wag przy handlu wagami po renormalizacji) i za kazdym razem pierwsze pytanie
brzmialo "od kiedy?" - a odpowiedz trzeba bylo zgadywac z dat commitow.

SAM COMMIT NIE WYSTARCZA. `SYGNALY_EKSPERYMENTALNE` zmienia sklad sygnalow
zmienna srodowiskowa, wiec ten sam commit potrafi policzyc dwa rozne modele.
Dlatego trener zapisuje obok sha takze rozstrzygniety zbior wlaczonych
eksperymentow (patrz train.py) - to sha opisuje kod, a tamto opisuje jego
konfiguracje.

SKAD BIERZEMY SHA

Z `GIT_SHA` w srodowisku, ustawianego w karcie z tagu obrazu. Tag ma postac
`sha-12de83b` (workflow docker-publish), wiec przedrostek scinamy tutaj, a nie
w szablonie helmowym - zeby regula miala jedno miejsce i test.

W kontenerze NIE MA repozytorium git ani binarki `git`, wiec siegniecie po
`git rev-parse` jest wylacznie udogodnieniem dla uruchomien lokalnych. Gdy nie
wyjdzie zadne z dwoch zrodel, zwracamy None - i to jest STAN POPRAWNY, znaczacy
"nie wiadomo", a nie pusty napis udajacy odpowiedz.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

PRZEDROSTEK_TAGU = "sha-"

# Najkrotszy skrot, jaki git w ogole wystawia (`--short` daje dzis 7 znakow),
# i pelna dlugosc sha-1. Bez DOLNEJ granicy sam alfabet szesnastkowy przepuszcza
# nazwy galezi i tagow zlozone z tych liter: `beef`, `ada`, `decade`, `face`
# — wszystkie przeszlyby jako SHA i trafily do `learn.model_version.git_sha`
# jako odpowiedz na pytanie "jaki kod to policzyl".
DLUGOSC_SHA = range(7, 41)

# Znacznik doklejany, gdy drzewo robocze ma niezacommitowane zmiany. Sufiks,
# a nie None, bo commit jest tu POLOWA prawdy i lepiej oddac polowe z etykieta
# niz nic: `git checkout <sha>` nadal prowadzi w okolice, a `-dirty` mowi
# wprost, ze dokladnego odtworzenia nie bedzie. Ten sam sufiks, ktorego uzywa
# `git describe --dirty`, zeby nie wymyslac wlasnej konwencji.
SUFIKS_BRUDNY = "-dirty"


def _z_srodowiska() -> str | None:
    surowy = (os.environ.get("GIT_SHA") or "").strip()
    if not surowy:
        return None
    if surowy.startswith(PRZEDROSTEK_TAGU):
        surowy = surowy[len(PRZEDROSTEK_TAGU):]
    # Tag `latest` albo nazwa galezi nie jest sha i nie ma udawac, ze jest.
    # DLUGOSC TEZ SIE LICZY, nie tylko alfabet — patrz `DLUGOSC_SHA`.
    if (len(surowy) not in DLUGOSC_SHA
            or any(z not in "0123456789abcdef" for z in surowy.lower())):
        return None
    return surowy.lower()


def _czy_brudne(korzen: Path) -> bool | None:
    """Czy drzewo robocze rozni sie od HEAD. None, gdy nie da sie stwierdzic.

    `git status --porcelain` oddaje pusty napis dla drzewa czystego i po linii
    na kazdy plik zmieniony, dodany albo nieznany. Pliki nieznane TEZ licza sie
    tutaj jako roznica: nowy modul, ktorego nikt nie zacommitowal, jest czescia
    kodu, ktory wlasnie policzyl wynik.
    """
    try:
        wynik = subprocess.run(
            ["git", "-C", str(korzen), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if wynik.returncode != 0:
        return None
    return bool(wynik.stdout.strip())


def _z_repozytorium() -> str | None:
    """SHA z lokalnego repozytorium, z ADNOTACJA o brudnym drzewie roboczym.

    `git rev-parse HEAD` opisuje OSTATNI COMMIT, a nie zawartosc drzewa
    roboczego. Rozjazd nie jest hipotetyczny: analityk uruchamia lokalnie
    `train.py` bez `--sucho` na niezacommitowanej zmianie w `signals.py`,
    `train.py` wpisuje zwrocony sha do `learn.model_version.git_sha`, a ktos
    pozniej robi `git checkout <ten sha>` i NIE ODTWARZA wyniku. Moduł istnieje
    wylacznie po to, zeby na pytanie „jaki kod to policzyl" dalo sie odpowiedziec
    bez zgadywania — a sha opisujacy inny kod niz uruchomiony jest gorszy niz
    brak sha, bo wyglada na odpowiedz.

    NIEPEWNOSCI CO DO CZYSTOSCI TEZ NIE UKRYWAMY. Gdy `git status` nie odpowie
    (brak binarki, timeout, uszkodzone repozytorium), nie wiemy, czy drzewo
    bylo czyste — i wtedy sufiks jest doklejany tak samo, bo „nie wiem, czy to
    ten kod" nalezy po tej samej stronie co „na pewno nie ten".
    """
    korzen = Path(__file__).resolve().parents[2]
    if not (korzen / ".git").exists():
        return None
    try:
        wynik = subprocess.run(
            ["git", "-C", str(korzen), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = wynik.stdout.strip()
    if wynik.returncode != 0 or not sha:
        return None
    return sha if _czy_brudne(korzen) is False else sha + SUFIKS_BRUDNY


def sha_kodu() -> str | None:
    """Skrocony SHA commita, z ktorego pochodzi ten kod, albo None.

    Z uruchomienia lokalnego na brudnym drzewie wraca `<sha>-dirty`: commit
    jest wtedy polowa prawdy i etykieta mowi to wprost. Obraz kontenerowy
    buduje sie z commita, wiec `GIT_SHA` nigdy nie niesie tego sufiksu.
    """
    return _z_srodowiska() or _z_repozytorium()
