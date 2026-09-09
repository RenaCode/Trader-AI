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


def _z_srodowiska() -> str | None:
    surowy = (os.environ.get("GIT_SHA") or "").strip()
    if not surowy:
        return None
    if surowy.startswith(PRZEDROSTEK_TAGU):
        surowy = surowy[len(PRZEDROSTEK_TAGU):]
    # Tag `latest` albo nazwa galezi nie jest sha i nie ma udawac, ze jest.
    if not surowy or any(z not in "0123456789abcdef" for z in surowy.lower()):
        return None
    return surowy.lower()


def _z_repozytorium() -> str | None:
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
    return sha if wynik.returncode == 0 and sha else None


def sha_kodu() -> str | None:
    """Skrocony SHA commita, z ktorego pochodzi ten kod, albo None."""
    return _z_srodowiska() or _z_repozytorium()
