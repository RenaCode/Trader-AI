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
    domeny - rodzina ma konta w Authelii dla Kidwatcha, nie dla tradera;
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


def wymagana_grupa() -> str:
    return os.environ.get("SSO_GRUPA", "admins")


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
    grupy = _jedna_wartosc(request, NAGLOWEK_GRUP) or ""
    if wymagana_grupa() not in {g.strip() for g in grupy.split(",")}:
        raise HTTPException(
            status_code=403,
            detail=f"Użytkownik „{login}” nie należy do grupy „{wymagana_grupa()}” "
                   "w Authelii — Trader jest tylko dla niej.")
    return login
