"""Blok nginx z wlasnym `add_header` MUSI miec komplet naglowkow bezpieczenstwa.

DLACZEGO TEN PLIK ISTNIEJE

W nginx dyrektywy `add_header` z poziomu nadrzednego sa dziedziczone TYLKO
wtedy, gdy na poziomie biezacym nie ma ani jednej wlasnej `add_header`. Jedna
wlasna zrywa dziedziczenie calego kompletu - i nic tego nie zglasza.

`location /assets/` mial wlasny `add_header Cache-Control "public, immutable"`,
wiec dla `/assets/*` znikalo wszystkie piec naglowkow z poziomu `server`: CSP,
HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options` i `Referrer-Policy`.
A `/assets/` to wlasnie miejsce, z ktorego Vite serwuje CALY kod JS i CSS
aplikacji: odpowiedz z `/assets/index-<hash>.js` szla bez `nosniff`, a
`frame-ancestors 'none'` nie chronilo zadnego zasobu z tej sciezki. Komentarz
przy CSP opisywal polityke jako obowiazujaca dla calej aplikacji.

Nie kosztowalo to pieniedzy - psulo zabezpieczenie, ktore konfiguracja
deklaruje jako wlaczone. Sprawdzenie jest tutaj, a nie w glowie osoby
dopisujacej nastepny `location`, bo nastepny raz wyglada dokladnie tak samo.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
NGINX = (KORZEN / "docker/nginx.conf").read_text(encoding="utf-8")
WLACZANY = "/etc/nginx/naglowki-bezpieczenstwa.conf"
PLIK_NAGLOWKOW = (KORZEN / "docker/naglowki-bezpieczenstwa.conf").read_text(encoding="utf-8")

# Komplet, ktory ma obowiazywac wszedzie. Nazwy, a nie cale linie - o tresc
# polityki spiera sie ktos inny, tu chodzi o to, zeby nie znikala po cichu.
KOMPLET = ("Strict-Transport-Security", "X-Content-Type-Options",
           "X-Frame-Options", "Referrer-Policy", "Content-Security-Policy")


def _bez_komentarzy(tekst: str) -> str:
    return "\n".join(w.split("#")[0] for w in tekst.splitlines())


def _bloki(tekst: str, naglowek: str = "(plik)") -> list[tuple[str, str]]:
    """[(naglowek bloku, jego WLASNE linie bez blokow zagniezdzonych)].

    Wlasne linie, bo nginx zrywa dziedziczenie na poziomie bloku: `add_header`
    w zagniezdzonym `location` nie ma nic wspolnego z blokiem nadrzednym.
    """
    out, wlasne, i, n = [], [], 0, len(tekst)
    poczatek_ciala = 0
    while i < n:
        znak = tekst[i]
        if znak == "{":
            glebokosc, j = 1, i + 1
            while j < n and glebokosc:
                glebokosc += (tekst[j] == "{") - (tekst[j] == "}")
                j += 1
            wewnetrzny = tekst[poczatek_ciala:i].strip().splitlines()
            nazwa = wewnetrzny[-1].strip() if wewnetrzny else "(bez nazwy)"
            out.extend(_bloki(tekst[i + 1:j - 1], nazwa))
            wlasne.append(tekst[poczatek_ciala:i].rsplit("\n", 1)[0]
                          if "\n" in tekst[poczatek_ciala:i] else "")
            i = j
            poczatek_ciala = i
            continue
        i += 1
    wlasne.append(tekst[poczatek_ciala:])
    out.append((naglowek, "\n".join(wlasne)))
    return out


BLOKI = _bloki(_bez_komentarzy(NGINX))
Z_WLASNYM_ADD_HEADER = [(n, t) for n, t in BLOKI if "add_header" in t]


def test_stend_widzi_blok_assets_i_jego_wlasny_add_header():
    # Bez tego reszta pliku sprawdzalaby pusty zbior i milczala.
    nazwy = [n for n, _ in Z_WLASNYM_ADD_HEADER]
    assert any("location /assets/" in n for n in nazwy), nazwy
    assert len(Z_WLASNYM_ADD_HEADER) >= 1


@pytest.mark.parametrize("nazwa,tresc", Z_WLASNYM_ADD_HEADER,
                         ids=[n for n, _ in Z_WLASNYM_ADD_HEADER])
def test_blok_z_wlasnym_add_header_wlacza_komplet_naglowkow(nazwa, tresc):
    brakujace = [h for h in KOMPLET if h not in tresc]
    if not brakujace:
        return
    assert WLACZANY in tresc, (
        f"blok {nazwa!r} ma wlasny add_header, wiec NIE DZIEDZICZY naglowkow "
        f"z poziomu nadrzednego - brakuje {brakujace}, a `include {WLACZANY}` "
        f"nie ma")


def test_blok_server_takze_wlacza_komplet():
    serwer = [t for n, t in BLOKI if n.startswith("server")]
    assert serwer, "nie znalazlem bloku server"
    assert WLACZANY in serwer[0]


@pytest.mark.parametrize("naglowek", KOMPLET)
def test_wlaczany_plik_definiuje_caly_komplet(naglowek):
    assert f"add_header {naglowek} " in PLIK_NAGLOWKOW


def test_wlaczany_plik_trafia_do_obrazu():
    # Bez tego COPY nginx nie wstanie w ogole - `include` nieistniejacego pliku
    # jest bledem konfiguracji. Lepiej niz wstac bez CSP, ale i tak awaria.
    web = (KORZEN / "docker/web.Dockerfile").read_text(encoding="utf-8")
    assert "docker/naglowki-bezpieczenstwa.conf" in web
    assert WLACZANY in web


def test_naglowki_sa_w_JEDNYM_miejscu_a_nie_powtorzone_recznie():
    # Powtorzenie kompletu w kazdym bloku byloby tym samym bledem odlozonym
    # w czasie: dopisany naglowek trafialby do jednego miejsca z dwoch.
    for naglowek in KOMPLET:
        assert f"add_header {naglowek} " not in _bez_komentarzy(NGINX), (
            f"{naglowek} wpisany wprost w nginx.conf - ma byc wylacznie "
            f"w docker/naglowki-bezpieczenstwa.conf")


def test_index_html_bez_pamieci_podrecznej():
    """index.html zawsze z serwera (2026-10-14, SSO): strona z pamieci
    przegladarki omija forwardAuth i pokazuje logowanie aplikacji zamiast
    portalu. `expires epoch`, nie add_header - inaczej blok zerwalby
    dziedziczenie naglowkow bezpieczenstwa."""
    tresc = (KORZEN / "docker" / "nginx.conf").read_text(encoding="utf-8")
    blok = re.search(r"location = /index\.html \{(.*?)\}", tresc, re.S)
    assert blok, "brak bloku location = /index.html"
    assert "expires epoch;" in blok.group(1)
    assert "add_header" not in blok.group(1)
