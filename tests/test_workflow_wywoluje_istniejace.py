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


# --- proxy /api musi działać także w podglądzie produkcyjnego bundla --------
def test_proxy_api_dziala_takze_w_preview():
    """Test dymny celowo używa `vite preview`, żeby sprawdzać ten bundle, który
    pójdzie na serwer — a nie wersję deweloperską z innym kodem.

    Proxy zdefiniowane wyłącznie w `server` działa tylko przy `vite dev`. Bez
    niego w `preview` front woła /api pod portem podglądu, gdzie nic nie
    odpowiada: logowanie kończy się niczym, a dashboard nigdy się nie renderuje.
    """
    cfg = (KORZEN / "web" / "vite.config.js").read_text()
    assert re.search(r"preview:\s*\{[^}]*proxy", cfg), \
        "vite.config.js nie ustawia proxy dla `preview` — test dymny nie dosięgnie API"
    assert re.search(r"server:\s*\{[^}]*proxy", cfg), \
        "vite.config.js nie ustawia proxy dla `server`"
    # Jeden obiekt, nie dwie kopie: rozjazd byłby niewidoczny do następnej awarii CI.
    assert len(re.findall(r"'/api':\s*\{\s*target", cfg)) == 1, \
        "proxy zdefiniowane dwa razy — kopie się rozjadą"


# --- podbijanie tagow nie moze wskazac produkcji na obraz bez poswiadczen ---
def test_podbijanie_tagow_wylaczone_dopoki_brak_poswiadczen_do_rejestru():
    """Krok `update-git` podbija tag obrazu w charts/trader/values.yaml, a ArgoCD
    to synchronizuje — wskazywałby więc produkcję na obraz z prywatnego ghcr.io.

    Klaster nie ma do niego poświadczeń: `imagePullSecrets: []`. Obrazy powstają
    na VPS i są importowane wprost do containerd; `pullPolicy: IfNotPresent`
    sprawia, że kubelet nie szuka ich w rejestrze.

    Ten test wiąże jedno z drugim: dopóki chart nie ma `imagePullSecrets`,
    podbijanie tagów przez CI musi zostać wyłączone. Zdjęcie warunku bez
    dodania sekretu zatrzyma produkcję na ImagePullBackOff.
    """
    import yaml

    wf = yaml.safe_load((KORZEN / ".github/workflows/docker-publish.yml").read_text())
    warunek = wf["jobs"]["update-git"]["if"]
    values = (KORZEN / "charts/trader/values.yaml").read_text()

    ma_sekrety = re.search(r"^imagePullSecrets:\s*\[\s*\]\s*$", values, re.M) is None
    if not ma_sekrety:
        assert warunek is False, (
            "CI podbija tagi obrazów, a klaster nie ma poświadczeń do ghcr.io — "
            "pierwszy push kodu skończy się ImagePullBackOff")
