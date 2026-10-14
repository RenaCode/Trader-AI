"""SSO: tozsamosc z naglowkow Authelii (renacode-infra docs/sso-tozsamosc.md).

Traefik (middleware `default-authelia-sso`, forwardAuth) przepuszcza zadanie
do panelu dopiero po 2FA w Authelii i dokleja `Remote-User`, `Remote-Groups`,
`Remote-Email`, `Remote-Name`. Wczesniej middleware `default-authelia-naglowki`
kasuje te same naglowki podane przez klienta. Obecnosc `Remote-User` znaczy
wiec: ta osoba przeszla 2FA i nalezy do grupy dopuszczonej do hosta.

ZAUFANIE WYLACZNIE PRZY `SSO_NAGLOWKI=1`. Zmienna ustawia ten sam przelacznik
chartu (`sso.enabled`), ktory dopina oba middleware'y - jedna wartosc
w values, wiec zaufanie i usuwanie naglowkow od klienta zawsze ida razem. Bez
middleware'u (SSO wylaczone) `Remote-User` od klienta dochodzi do aplikacji
nietkniety; aplikacja, ktora by mu wtedy ufala, wpuszczalaby kazdego, kto zna
login wlasciciela.

CZEGO TU NIE MA, CELOWO:
  - wariantow `Remote_User` / `X-Remote-User` - czytamy jeden, dokladny naglowek;
  - rol z `Remote-Groups`: o wejsciu na host decyduje Authelia, o roli
    w aplikacji (`owner`/`viewer`) baza. Grupa jest tu tylko druga, niezalezna
    bramka (`SSO_GRUPA`) na wypadek reguly Authelii poluzowanej dla calej
    domeny: do Tradera wchodza wylacznie grupy z `SSO_GRUPA` (pelny dostep
    wedlug roli konta) i `SSO_GRUPA_PODGLAD` (tylko podglad), a inne konta
    Authelii - nie;
  - podgladu tylko z roli konta: sesja wpuszczona WYLACZNIE przez grupe
    `SSO_GRUPA_PODGLAD` ma role obcieta do `viewer` przy kazdym zadaniu
    (`tylko_podglad`), nawet gdy konto w bazie ma `owner` - pomylka przy
    zakladaniu konta albo reczny UPDATE nie daje jej zapisu;
  - zakladania kont: brak konta = 403, nie nowe puste konto (kontrakt, pkt 3).
"""
from __future__ import annotations

import os
import re

from fastapi import HTTPException, Request

NAGLOWEK_UZYTKOWNIKA = "remote-user"
NAGLOWEK_GRUP = "remote-groups"

# Kontrakt, pkt 2: znaki spoza tego zbioru = 400. Dlugosc jak `LoginBody`.
_LOGIN = re.compile(r"[a-z0-9._-]{1,128}")


def wlaczone() -> bool:
    """Czytane przy kazdym zadaniu, nie przy imporcie - test przelacza flage
    w jednym procesie, a wartosc zapamietana przy imporcie dalaby wynik
    z poprzedniego testu."""
    return os.environ.get("SSO_NAGLOWKI") == "1"


def _lista(zmienna: str) -> frozenset[str]:
    return frozenset(g.strip() for g in os.environ.get(zmienna, "").split(",")
                     if g.strip())


def grupy_podgladu() -> frozenset[str]:
    """Grupy tylko do podgladu (chart: klucz `SSO_GRUPA_PODGLAD`, po przecinku).
    Brak klucza = nikt nie wchodzi w tym trybie."""
    return _lista("SSO_GRUPA_PODGLAD")


def dopuszczone_grupy() -> frozenset[str]:
    """Grupy z Sekretu aplikacji (chart: klucz `SSO_GRUPA`, nazwy po przecinku).

    BEZ WARTOSCI DOMYSLNEJ W KODZIE. Repozytorium jest publiczne, a nazwy kont
    i grup Authelii sa konfiguracja wlasciciela, nie kodu. Brak klucza przy
    wlaczonej fladze = odmowa dla kazdego (`login_z_naglowka`), nie
    wpuszczenie kazdego - droga awaryjna (haslo aplikacji bez naglowka)
    dziala dalej. Puste pozycje (`a,,b`, sam przecinek) sa pomijane, wiec
    literowka nie otwiera drogi grupie o pustej nazwie.
    """
    return _lista("SSO_GRUPA")


def adres_wylogowania() -> str | None:
    """Portal Authelii z `rd` na ten host; ustawia chart (`sso.wylogujUrl`)."""
    return os.environ.get("SSO_WYLOGUJ_URL") or None


def _jedna_wartosc(request: Request, naglowek: str) -> str | None:
    wartosci = request.headers.getlist(naglowek)
    if len(wartosci) > 1:
        # Dwa `Remote-User` to albo blad posrednika, albo proba przemytu
        # drugiej tozsamosci obok tej od Authelii. Ktora jest prawdziwa - nie
        # wiadomo, wiec zadna.
        raise HTTPException(status_code=400,
                            detail=f"Naglowek {naglowek} wystapil wiecej niz raz")
    return wartosci[0] if wartosci else None


def login_z_naglowka(request: Request) -> str | None:
    """Login z `Remote-User` albo None (flaga wylaczona albo brak naglowka).

    None przy WYLACZONEJ fladze niezaleznie od tego, co przyslal klient - to
    jest cala ochrona przed podrobionym naglowkiem na hoscie bez Authelii.
    Przy wlaczonej: 400 za podwojny albo niepoprawny login, 403 bez grupy.
    """
    if not wlaczone():
        return None
    login = _jedna_wartosc(request, NAGLOWEK_UZYTKOWNIKA)
    if login is None:
        return None
    if not _LOGIN.fullmatch(login):
        raise HTTPException(status_code=400, detail="Niepoprawny naglowek Remote-User")
    dopuszczone = dopuszczone_grupy() | grupy_podgladu()
    if not dopuszczone:
        raise HTTPException(
            status_code=403,
            detail="SSO jest włączone, ale grupa Authelii (klucz SSO_GRUPA w Sekrecie "
                   "aplikacji) nie jest ustawiona — logowanie przez SSO zablokowane.")
    if not dopuszczone & _grupy(request):
        raise HTTPException(
            status_code=403,
            detail=f"Użytkownik „{login}” nie należy do żadnej z grup "
                   f"„{', '.join(sorted(dopuszczone))}” w Authelii — Trader jest "
                   "tylko dla nich.")
    return login


def _grupy(request: Request) -> set[str]:
    return {g.strip() for g in (_jedna_wartosc(request, NAGLOWEK_GRUP) or "").split(",")}


def tylko_podglad(request: Request) -> bool:
    """Czy te zadanie wpuscila WYLACZNIE grupa podgladu. Wolac po
    `login_z_naglowka` (ta odrzucila juz zadanie bez zadnej dopuszczonej
    grupy)."""
    grupy = _grupy(request)
    return not (dopuszczone_grupy() & grupy) and bool(grupy_podgladu() & grupy)
