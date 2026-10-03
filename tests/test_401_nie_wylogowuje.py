"""401 spoza logowania wyrzuca uzytkownika z aplikacji — i to w najgorszym momencie.

Front obsluguje 401 centralnie w `request()` (web/src/utils/api.js): kazde 401
poza sciezkami `/api/auth/` znaczy "sesja wygasla" i przerzuca uzytkownika na
ekran logowania. To jest sluszne dla wygasnietej sesji i BLEDNE dla odmowy
potwierdzenia przy sesji WAZNEJ.

Przypadek, ktory to wykryl: `POST /api/account/sessions/revoke-others` oddawalo
401 przy zlym hasle. Ta operacja sluzy do wylogowania cudzych urzadzen, gdy
podejrzewasz, ze ktos ma dostep do konta — jedyna operacja obronna w systemie.
Literowka w hasle wyrzucala wlasciciela na ekran logowania zamiast pokazac
"Haslo jest nieprawidlowe", czyli psula sie dokladnie w chwili uzycia.

Strazniki nizej pilnuja OBU stron umowy: ze API nie oddaje 401 tam, gdzie sesja
jest wazna, i ze front nadal traktuje 401 jako wygasniecie sesji (bo gdyby
przestal, pierwszy straznik przestalby cokolwiek znaczyc).
"""
from pathlib import Path
import re

API = Path("services/api")


def test_zadne_401_nie_powstaje_poza_routerem_logowania():
    winne = []
    for plik in API.rglob("*.py"):
        if plik.name == "auth.py":      # logowanie i MFA: tam 401 znaczy 401
            continue
        for nr, linia in enumerate(plik.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"HTTPException\(\s*401", linia):
                winne.append(f"{plik}:{nr}")
    assert not winne, (
        "401 poza logowaniem wyrzuci uzytkownika na ekran logowania, zabierajac "
        "mu komunikat sprzed oczu. Sesja jest wazna, odmawiasz samego dzialania "
        f"— uzyj 403: {winne}")


def test_odmowa_hasla_przy_wylogowaniu_urzadzen_to_403():
    t = (API / "routers" / "account.py").read_text(encoding="utf-8")
    # Od 2026-10-03 haslo sprawdza wspolne `potwierdz_haslo` (licznik prob
    # i audyt), a kod odmowy podaje wolajacy.
    assert 'status=403, komunikat="Hasło jest nieprawidłowe"' in t


def test_front_nadal_traktuje_401_jako_wygasniecie_sesji():
    """Bez tego straznik wyzej broni umowy, ktorej druga strona juz nie ma."""
    t = Path("web/src/utils/api.js").read_text(encoding="utf-8")
    assert "isLoginFlow" in t and "401" in t
    assert "sessionExpired" in t
