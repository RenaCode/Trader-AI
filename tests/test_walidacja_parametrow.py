"""Wartosci enumowe maja byc odrzucane NA WEJSCIU, a nie przez Postgresa.

DLACZEGO TO JEST OSOBNY TEST

`ref.engine` i `ref.trade_mode` to enumy, a psycopg wysyla `str` jako `unknown` -
literal rzutuje wiec dopiero Postgres, juz w srodku zapytania. Przy nieznanej
wartosci konczy sie to `InvalidTextRepresentation`, czyli 500: komunikat
"Internal Server Error" na cos, co jest zwyklym bledem wywolujacego. To samo
dotyczy `notify.channel_kind[]` - `{"channels": ["telegram"]}` to naturalna
pomylka, bo do migracji 0027 ta wartosc w enumie BYLA.

Test pilnuje dwoch rzeczy naraz, i druga jest wazniejsza:

  1. odpowiedzia ma byc 422 (a nie 500 i nie przypadkowe 404),
  2. zapytanie NIE MA w ogole dojsc do bazy.

Samo sprawdzenie kodu odpowiedzi byloby slabe: 422 da sie uzyskac takze wtedy,
gdy walidacja odrzuca cos innego niz trzeba. Dlatego atrapa bazy tutaj nie
zwraca wierszy - ona WYBUCHA przy pierwszym uzyciu. Dzieki temu "zielony" znaczy
"granica zadzialala przed baza", a nie "cos zwrocilo 422".

Kanarek `test_poprawne_wartosci_DOCHODZA_do_bazy` pilnuje tej atrapy od drugiej
strony: gdyby wybuchala zawsze albo gdyby walidacja odrzucala rowniez poprawne
silniki, testy powyzej przechodzilyby z zupelnie zlego powodu.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api.auth import current_user, require_csrf
from services.api.main import app
from services.api.routers import learning, moves, notifications, overview, risk


class ZapytanieDoBazy(AssertionError):
    """Zapytanie ruszylo mimo nieznanej wartosci - czyli granica nie zadzialala.

    Dziedziczy z AssertionError celowo: to nie jest awaria atrapy, tylko
    niezdany test.
    """


# Moduly, ktore zwiazaly `fetch_one`/`fetch_all` u siebie przez `from ..db import`.
# Podmiana w `services.api.db` nie doszlaby do nich - nazwy sa juz zwiazane.
MODULY = (overview, moves, risk, learning, notifications)


@pytest.fixture
def klient(monkeypatch):
    """Klient z zalogowanym uzytkownikiem i baza, ktora nie chce byc uzywana."""
    async def wybuch(sql, params=None):
        raise ZapytanieDoBazy(
            f"zapytanie poszlo do bazy zamiast zostac odrzucone na wejsciu: "
            f"{' '.join(sql.split())[:120]!r} params={params!r}")

    for modul in MODULY:
        for nazwa in ("fetch_one", "fetch_all", "execute"):
            if hasattr(modul, nazwa):
                monkeypatch.setattr(modul, nazwa, wybuch)

    # Autoryzacja i CSRF sa niezalezne od tego, co testujemy, a w FastAPI
    # rozwiazuja sie PRZED walidacja ciala - bez podmiany dostalibysmy 401/403
    # i nigdy nie dowiedzielibysmy sie, co robi walidacja.
    app.dependency_overrides[current_user] = lambda: {"id": 1, "login": "test",
                                                      "role": "owner"}
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# Po jednym endpoincie z kazdego routera, ktory bierze silnik ze sciezki.
SCIEZKI = [
    "/api/engines/{e}/overview",
    "/api/engines/{e}/equity-curve",
    "/api/engines/{e}/positions",
    "/api/engines/{e}/decisions",
    "/api/engines/{e}/planned",
    "/api/engines/{e}/trades",
    "/api/engines/{e}/risk/limits",
    "/api/engines/{e}/risk/violations",
    "/api/engines/{e}/risk/killswitch",
    "/api/engines/{e}/risk/dozorca",
    "/api/engines/{e}/learning/state",
    "/api/engines/{e}/learning/walk-forward",
    "/api/engines/{e}/learning/calibration",
    "/api/engines/{e}/learning/drift",
    "/api/engines/{e}/learning/signal-performance",
    "/api/engines/{e}/learning/postmortems",
]


@pytest.mark.parametrize("sciezka", SCIEZKI)
def test_nieznany_silnik_daje_422_i_nie_dotyka_bazy(klient, sciezka):
    r = klient.get(sciezka.format(e="nieistnieje"))
    assert r.status_code == 422, (
        f"{sciezka}: nieznany silnik dal {r.status_code}, a ma dac 422")


# Endpointy z parametrem `mode`. Pozostale go nie maja - filtruja tylko po silniku.
SCIEZKI_Z_TRYBEM = [
    "/api/engines/crypto/overview",
    "/api/engines/crypto/equity-curve",
    "/api/engines/crypto/positions",
    "/api/engines/crypto/trades",
    "/api/engines/crypto/risk/limits",
]


@pytest.mark.parametrize("sciezka", SCIEZKI_Z_TRYBEM)
def test_nieznany_tryb_daje_422_i_nie_dotyka_bazy(klient, sciezka):
    r = klient.get(sciezka, params={"mode": "xyz"})
    assert r.status_code == 422, (
        f"{sciezka}: nieznany tryb dal {r.status_code}, a ma dac 422")


def test_kanal_telegram_daje_422_i_nie_dotyka_bazy(klient):
    """`telegram` wypadl z enuma w migracji 0027 i nadal jest naturalna pomylka."""
    r = klient.put("/api/notifications/preferences",
                   json={"event_code": "postmortem.ready", "enabled": True,
                         "channels": ["telegram"]})
    assert r.status_code == 422, (
        f"kanal spoza enuma dal {r.status_code}, a ma dac 422")


def test_kanal_z_enuma_przechodzi_walidacje(klient):
    """Kanarek do poprzedniego: lista kanalow ma byc zawezona, a nie zamknieta.

    Gdyby walidacja odrzucala wszystko, test powyzej bylby zielony bez powodu.
    """
    with pytest.raises(ZapytanieDoBazy):
        klient.put("/api/notifications/preferences",
                   json={"event_code": "postmortem.ready", "enabled": True,
                         "channels": ["inapp", "whatsapp", "webhook"]})


@pytest.mark.parametrize("engine", ["crypto", "equity"])
@pytest.mark.parametrize("mode", ["demo", "live"])
def test_poprawne_wartosci_DOCHODZA_do_bazy(klient, engine, mode):
    """Kanarek do atrapy: dopuszczone wartosci maja isc dalej, az do zapytania.

    Bez tego caly plik dalby sie "naprawic" przez odrzucanie wszystkiego.
    """
    with pytest.raises(ZapytanieDoBazy):
        klient.get(f"/api/engines/{engine}/overview", params={"mode": mode})
