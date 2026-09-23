"""`sync-agents.sh` musi USUWAC, inaczej `--sprawdz` nigdy sie nie zgodzi.

DLACZEGO TEN PLIK ISTNIEJE

Sciezka kopiujaca wylacznie DODAWALA i NADPISYWALA (`cp "$ZRODLO"/*.md`).
Sciezka sprawdzajaca porownuje katalogi w OBU kierunkach, wiec plik obecny
w celu i nieobecny w zrodle byl dla niej rozjazdem. Te dwie sciezki nie mogly
sie zgodzic: `--sprawdz` zwracal 1, synchronizacja tego nie naprawiala,
a kolejny `--sprawdz` znowu zwracal 1. Naglowek modulu obiecywal co innego:
"rozjazd jest do wykrycia jednym `diff`".

Skutek nie byl kosmetyczny. Definicje agenta usuwa sie z repozytorium wlasnie
wtedy, gdy okazuje sie, ze ma za szerokie uprawnienia narzedziowe. Kopia
w `~/workspace/.claude/agents/` zostawala, Claude Code dalej ja widzial i dalej
dalo sie ja wywolac - usuniecie z repozytorium nie mialo zadnego skutku,
a meldunek mowil "Skopiowano N definicji".

Drugi brak w tym samym miejscu: `cp` nadpisywal pliki w celu bez pytania
i bez kopii, wiec reczna zmiana ginela nieodwracalnie - mimo ze skrypt ma
mechanizm (`--sprawdz`) pozwalajacy ja wykryc przed nadpisaniem. Ostrzezenie
nie jest zabezpieczeniem: kopia musi powstac, a gdy nie moze - przebieg ma sie
przerwac.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
SKRYPT = KORZEN / "scripts/sync-agents.sh"

W_REPOZYTORIUM = {
    "analityk.md": "definicja analityka, wersja z repozytorium\n",
    "strateg.md": "definicja stratega, wersja z repozytorium\n",
    "trener.md": "definicja trenera, wersja z repozytorium\n",
}
USUNIETY_Z_REPOZYTORIUM = "za-szerokie-uprawnienia.md"


@pytest.fixture(autouse=True)
def _wymagany_bash():
    if not shutil.which("bash"):
        pytest.skip("bez basha nie ma czym uruchomic skryptu")


@pytest.fixture
def stend(tmp_path):
    """Repozytorium z trzema definicjami i cel po rozjezdzie."""
    zrodlo = tmp_path / "repo/.claude/agents"
    zrodlo.mkdir(parents=True)
    (tmp_path / "repo/scripts").mkdir(parents=True)
    kopia_skryptu = tmp_path / "repo/scripts/sync-agents.sh"
    shutil.copy(SKRYPT, kopia_skryptu)
    for nazwa, tresc in W_REPOZYTORIUM.items():
        (zrodlo / nazwa).write_text(tresc, encoding="utf-8")

    cel = tmp_path / "cel"
    cel.mkdir()
    for nazwa, tresc in W_REPOZYTORIUM.items():
        (cel / nazwa).write_text(tresc, encoding="utf-8")
    # definicja usunieta z repozytorium, ktorej kopia zostala
    (cel / USUNIETY_Z_REPOZYTORIUM).write_text(
        "agent z za szerokimi uprawnieniami narzedziowymi\n", encoding="utf-8")
    # reczna zmiana w celu, ktorej nikt nie zapisal w repozytorium
    (cel / "strateg.md").write_text("reczna zmiana w kopii\n", encoding="utf-8")
    return kopia_skryptu, zrodlo, cel


def uruchom(skrypt: Path, *argumenty: str):
    return subprocess.run(["bash", str(skrypt), *argumenty], capture_output=True,
                          text=True, env=dict(os.environ), timeout=60)


def test_synchronizacja_USUWA_definicje_zdjeta_z_repozytorium(stend):
    skrypt, _, cel = stend
    w = uruchom(skrypt, str(cel))
    assert w.returncode == 0, w.stdout + w.stderr
    assert not (cel / USUNIETY_Z_REPOZYTORIUM).exists(), (
        "kopia zostala - Claude Code dalej widzi agenta, ktorego zdjeto "
        "z repozytorium wlasnie po to, zeby przestal byc wywolywalny")


def test_po_synchronizacji_sprawdz_przechodzi(stend):
    # To jest cala obietnica naglowka modulu: rozjazd jest do wykrycia jednym
    # `diff`, a synchronizacja go usuwa. Przed poprawka `--sprawdz` zwracal 1
    # takze BEZPOSREDNIO po udanej synchronizacji.
    skrypt, _, cel = stend
    uruchom(skrypt, str(cel))
    w = uruchom(skrypt, "--sprawdz", str(cel))
    assert w.returncode == 0, w.stdout + w.stderr


def test_sprawdz_PRZED_synchronizacja_nadal_widzi_rozjazd(stend):
    # Bez tego test wyzej przechodzilby takze wtedy, gdyby `--sprawdz`
    # przestalo cokolwiek wykrywac.
    skrypt, _, cel = stend
    assert uruchom(skrypt, "--sprawdz", str(cel)).returncode == 1


def test_nadpisywana_tresc_zostaje_w_kopii_obok(stend):
    skrypt, _, cel = stend
    uruchom(skrypt, str(cel))
    kopie = sorted(cel.parent.glob(f"{cel.name}.kopia-*"))
    assert kopie, "nadpisana tresc przepadla bez sladu"
    zachowane = kopie[-1]
    assert (zachowane / "strateg.md").read_text(encoding="utf-8") == "reczna zmiana w kopii\n"
    assert (zachowane / USUNIETY_Z_REPOZYTORIUM).exists()


def test_przebieg_bez_zmian_nie_robi_kopii(stend):
    # Kopia przy kazdym przebiegu zasmiecalaby katalog nadrzedny i po kilku
    # dniach nikt by nie wiedzial, ktora jest ta wazna.
    skrypt, _, cel = stend
    uruchom(skrypt, str(cel))
    for sciezka in cel.parent.glob(f"{cel.name}.kopia-*"):
        shutil.rmtree(sciezka)
    uruchom(skrypt, str(cel))
    assert not list(cel.parent.glob(f"{cel.name}.kopia-*"))


def test_puste_zrodlo_NIE_czysci_celu(stend):
    # Pusty katalog w repozytorium znaczy raczej "cos jest nie tak ze sciezka"
    # niz "skasuj wszystkich agentow".
    skrypt, zrodlo, cel = stend
    for p in zrodlo.iterdir():
        p.unlink()
    w = uruchom(skrypt, str(cel))
    assert w.returncode != 0
    assert "BLAD" in w.stdout + w.stderr
    assert (cel / "analityk.md").exists()


def test_gdy_kopii_zrobic_sie_NIE_DA_przebieg_sie_przerywa(stend):
    # Brak kopii musi PRZERWAC operacje nieodwracalna, a nie tylko wypisac
    # ostrzezenie. Katalog nadrzedny celu tylko do odczytu = `mkdir` kopii
    # nie przejdzie.
    if os.geteuid() == 0:
        pytest.skip("root ignoruje prawa katalogu - tego stendu nie da sie tak zbudowac")
    skrypt, _, cel = stend
    rodzic = cel.parent
    tryb = rodzic.stat().st_mode
    os.chmod(rodzic, 0o555)
    try:
        w = uruchom(skrypt, str(cel))
    finally:
        os.chmod(rodzic, tryb)
    assert w.returncode != 0, w.stdout + w.stderr
    assert (cel / USUNIETY_Z_REPOZYTORIUM).exists(), "usunieto mimo braku kopii"
    assert (cel / "strateg.md").read_text(encoding="utf-8") == "reczna zmiana w kopii\n", \
        "nadpisano mimo braku kopii"
