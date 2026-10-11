"""Repozytorium jest publiczne od 2026-10 - straznik tresci, ktorej w nim nie chcemy.

Przeglad z 2026-10-11 znalazl w HEAD `ssh root@<adres serwera>` w instrukcji
agenta i prywatny adres e-mail w naglowku User-Agent. Oba zastapione (alias
hosta z `~/.ssh/config`, skrzynka projektu). Ten test pilnuje, zeby nie wrocily
przy nastepnej zmianie - historia gita i tak je pamieta, wiec liczy sie to,
czego NIE dopisujemy.

Swiadomie dopuszczone:
  * adres serwera w `Settings.jsx` - instrukcja dla wlasciciela, na jaki adres
    ograniczyc klucz gieldy; ten sam adres zwraca publiczny DNS domeny panelu,
  * adresy z zakresow dokumentacyjnych i znane adresy przykladowe w testach.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]

DOPUSZCZONE_PLIKI_IP = {"web/src/components/Settings.jsx"}
# example.com - przyklad publicznego adresu w testach webhooka.
DOPUSZCZONE_IP = {"93.184.216.34"}

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def _pliki() -> list[Path]:
    wynik = subprocess.run(["git", "ls-files"], cwd=KORZEN, capture_output=True,
                           text=True, check=True)
    return [KORZEN / p for p in wynik.stdout.splitlines()
            if not p.endswith(("package-lock.json", ".png", ".jpg", ".ico", ".woff2"))]


def _tekst(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return ""


def test_brak_ssh_na_konto_root_po_adresie():
    zle = [f"{p.relative_to(KORZEN)}:{n}"
           for p in _pliki() for n, linia in enumerate(_tekst(p).splitlines(), 1)
           if re.search(r"ssh\s+root@\d", linia)]
    assert not zle, f"logowanie root po adresie w publicznym repo: {zle}"


def test_brak_prywatnej_skrzynki_gmail():
    zle = [f"{p.relative_to(KORZEN)}:{n}"
           for p in _pliki() for n, linia in enumerate(_tekst(p).splitlines(), 1)
           if re.search(r"[\w.+-]+@gmail\.com", linia)]
    assert not zle, f"prywatny adres e-mail w publicznym repo: {zle}"


def test_brak_publicznych_adresow_ip_poza_dopuszczonymi():
    zle = []
    for p in _pliki():
        sciezka = str(p.relative_to(KORZEN))
        if sciezka in DOPUSZCZONE_PLIKI_IP:
            continue
        for n, linia in enumerate(_tekst(p).splitlines(), 1):
            for adres in IPV4.findall(linia):
                try:
                    ip = ipaddress.ip_address(adres)
                except ValueError:
                    continue
                if ip.is_global and adres not in DOPUSZCZONE_IP:
                    zle.append(f"{sciezka}:{n} {adres}")
    assert not zle, f"publiczny adres IP w repo: {zle}"


def test_KANAREK_wykrywacz_widzi_znany_wzorzec():
    """Cisza straznika nic nie znaczy, dopoki nie zlapie wzorca, ktory byl."""
    assert re.search(r"ssh\s+root@\d", "ssh root@" + "203.0.113.7 'kubectl'")
    assert IPV4.findall("x " + "8.8." + "4.4 y") == ["8.8." + "4.4"]
    assert ipaddress.ip_address("8.8." + "4.4").is_global
    assert not ipaddress.ip_address("203.0.113.7").is_global
