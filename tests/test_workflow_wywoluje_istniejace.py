"""Workflow nie może wołać pliku, którego nie ma.

CO SIĘ STAŁO

`services/seed_demo.py` usunięto 2026-08-27 razem z decyzją, że system pracuje
wyłącznie na realnych świecach. Krok CI „Migracje i dane testowe" wołał go
dalej. Python kończył się wtedy kodem 2 („nie mogę otworzyć pliku"), krok padał,
a **test dymny nie uruchamiał się wcale** — przez 11 dni i 145 commitów.

DLACZEGO NIKT TEGO NIE ZAUWAŻYŁ

Lista przebiegów pokazywała mieszankę zielonych i czerwonych. Zielone to były
commity `chore: obraz sha-*`, które zmieniają wyłącznie chart i są pomijane
przez filtr ścieżek. Z listy wyglądało to jak CI, które czasem pada — a nie jak
CI, które nie działa od dnia usunięcia pliku.

Test kosztuje milisekundy i złapałby to w pierwszym przebiegu po usunięciu.
"""
import re
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((KORZEN / ".github" / "workflows").glob("*.yml"))

# Wywolania w rodzaju `python sciezka/plik.py`. Interesuje nas sciezka, ktora
# wyglada na plik repozytorium - nie moduly (-m) ani polecenia powloki.
WZOR = re.compile(r"\bpython3?\s+(?!-)([\w./-]+\.py)\b")


def test_sa_jakies_workflow():
    """Kanarek: gdyby katalog zniknął albo zmienił nazwę, reszta testów
    przechodziłaby pusto i niczego by nie pilnowała."""
    assert WORKFLOWS, "brak plików workflow — test niczego nie sprawdza"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_wolane_skrypty_istnieja(wf):
    tresc = wf.read_text()
    brakujace = []
    for sciezka in sorted(set(WZOR.findall(tresc))):
        if not (KORZEN / sciezka).is_file():
            brakujace.append(sciezka)
    assert not brakujace, (
        f"{wf.name} woła nieistniejące pliki: {', '.join(brakujace)}. "
        f"Krok zakończy się kodem 2, a kroki po nim nie wykonają się wcale.")


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_wolane_skrypty_node_istnieja(wf):
    """To samo dla `node plik.mjs` — test dymny jest wołany właśnie tak."""
    wzor = re.compile(r"\bnode\s+(?!-)([\w./-]+\.(?:mjs|js|cjs))\b")
    tresc = wf.read_text()
    brakujace = [s for s in sorted(set(wzor.findall(tresc)))
                 if not (KORZEN / s).is_file()]
    assert not brakujace, f"{wf.name} woła nieistniejące pliki: {brakujace}"
