"""Status instrumentow `equity` u Alpaki: odswiezacz, ktorego czujka dotad nie miala.

DLACZEGO TO JEST OSOBNY PLIK TESTOW

Do 2026-10-15 `services/ingest/instrumenty.py` pytal o status wylacznie Bybita.
Dla 20 handlowalnych akcji `status_sprawdzony_at` bylo puste ZAWSZE, a czujka
(`dozor.py --co instrumenty --engine equity`) meldowala przy kazdym przebiegu
BRAK POMIARU: delisting akcji przeszedlby niezauwazony.

Te testy pilnuja trzech rzeczy naraz, bo kazda z osobna da sie "naprawic" tak,
ze wyglada na zdrowie:

  1. ze pytamy REJESTRU AKTYWOW (`/v2/assets/{symbol}`), a nie endpointu
     danych - kurs przychodzi takze dla papieru, ktorym nie wolno handlowac;
  2. ze blad zapytania (brak klucza, 401, 5xx, zly host) jest GLOSNY, a nie
     cichym pustym przebiegiem albo - gorzej - "wycofaniem" calego uniwersum;
  3. ze czujka i chart wiedza o nowym odswiezaczu - inaczej albo dalej mowi
     BRAK POMIARU (cisza o awarii odswiezacza), albo alarmuje co 10 minut,
     bo odswiezacz jest w kodzie, a nikt go nie wola.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]


def _uruchom(coro):
    return asyncio.run(coro)


# --- adapter: rejestr aktywow ------------------------------------------------

def _alpaca(odpowiedzi: dict[str, tuple[int, dict]], zapytane: list[str]):
    """Adapter Alpaki na atrapie transportu HTTP. Klucz: sciezka zapytania."""
    from services.exchanges.alpaca import AlpacaAdapter

    def obsluz(req: httpx.Request) -> httpx.Response:
        zapytane.append(req.url.path)
        kod, cialo = odpowiedzi.get(req.url.path, (404, {"code": 40410000,
                                                         "message": "asset not found"}))
        return httpx.Response(kod, json=cialo)

    http = httpx.AsyncClient(transport=httpx.MockTransport(obsluz))
    return AlpacaAdapter("k", "s", mode="demo", host="https://paper.test", http=http)


def _aktywo(symbol, status="active", tradable=True):
    return {"symbol": symbol, "status": status, "tradable": tradable,
            "fractionable": True, "shortable": True}


def test_adapter_pyta_REJESTRU_AKTYWOW_i_sklada_jedno_slowo_statusu():
    zapytane: list[str] = []
    a = _alpaca({
        "/v2/assets/AAPL": (200, _aktywo("AAPL")),
        "/v2/assets/MSFT": (200, _aktywo("MSFT", tradable=False)),
        "/v2/assets/XOM": (200, _aktywo("XOM", status="inactive", tradable=False)),
    }, zapytane)
    out = _uruchom(a.filtry_instrumentow(symbole=["AAPL", "MSFT", "XOM"]))
    assert zapytane == ["/v2/assets/AAPL", "/v2/assets/MSFT", "/v2/assets/XOM"], (
        "status ma przyjsc z rejestru aktywow, nie z endpointu danych")
    assert {s: g["status"] for s, g in out.items()} == {
        "AAPL": "active", "MSFT": "active_not_tradable", "XOM": "inactive"}
    # Ten sam ksztalt co u Bybita - `podmien_odpowiedz` i `rozbieznosci` go znaja.
    assert {"krok_ilosci", "min_ilosc", "min_wartosc", "krok_ceny"} <= set(out["AAPL"])


def test_404_to_GIELDA_NIE_ZNA_a_nie_awaria():
    """Brak w slowniku znaczy dalej "gielda go nie zna" - i to jest wycofanie."""
    from services.ingest.instrumenty import ocen_dostepnosc

    a = _alpaca({"/v2/assets/AAPL": (200, _aktywo("AAPL"))}, [])
    out = _uruchom(a.filtry_instrumentow(symbole=["AAPL", "ZNIKNIETY"]))
    assert set(out) == {"AAPL"}
    w = ocen_dostepnosc([{"id": 2, "symbol": "ZNIKNIETY", "tradeable": True,
                          "status_gieldy": "active", "wylaczony_przez_status": None}],
                        out, {})
    assert [x["klasa"] for x in w] == ["wycofanie"] and w[0]["zdjac"]


@pytest.mark.parametrize("kod", [401, 403, 500])
def test_BLAD_zapytania_jest_GLOSNY_nie_brakiem_instrumentu(kod, monkeypatch):
    """401 (zly klucz / zle srodowisko), 403 i 5xx NIE moga skonczyc sie pustym
    slownikiem: pusty slownik to "gielda nikogo nie zna", czyli wycofanie."""
    from services.exchanges import alpaca
    from services.exchanges.base import ExchangeError

    async def bez_czekania(_s):
        return None
    monkeypatch.setattr(alpaca.asyncio, "sleep", bez_czekania)
    a = _alpaca({"/v2/assets/AAPL": (kod, {"message": "nie"})}, [])
    with pytest.raises(ExchangeError):
        _uruchom(a.filtry_instrumentow(symbole=["AAPL"]))


# --- slownik statusow ---------------------------------------------------------

def test_slowa_Alpaki_w_JEDNYM_slowniku():
    from services.ingest.instrumenty import klasyfikuj_status

    assert klasyfikuj_status("active") == "handel"
    assert klasyfikuj_status("active_not_tradable") == "zawieszenie"
    assert klasyfikuj_status("inactive") == "wycofanie"
    # Bybit bez zmian.
    assert klasyfikuj_status("Trading") == "handel"
    assert klasyfikuj_status("Delivering") == "wycofanie"


def test_nikogo_nie_zna_to_ZLE_PYTANIE():
    from services.ingest.instrumenty import nikogo_nie_zna

    w_bazie = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
    assert nikogo_nie_zna(w_bazie, {}) is True
    assert nikogo_nie_zna(w_bazie, {"MSFT": {"status": "active"}}) is False
    assert nikogo_nie_zna([], {}) is False


# --- main() na atrapie bazy ---------------------------------------------------
#
# ATRAPA ODDAJE TYLKO KOLUMNY, KTORE SELECT NAPRAWDE WYMIENIA. Atrapa oddajaca
# caly wiersz przepuscilaby SELECT bez `status_gieldy` albo
# `wylaczony_przez_status` - a wtedy `ocen_dostepnosc` czyta None i kazdy
# przebieg wyglada jak "zmiana stanu".

PELNE = {
    "AAPL": {"id": 1, "symbol": "AAPL", "lot_size": D(1), "tick_size": D("0.01"),
             "min_notional": D(1), "tradeable": True, "status_gieldy": "active",
             "wylaczony_przez_status": None, "min_ilosc_meta": None},
    "MSFT": {"id": 2, "symbol": "MSFT", "lot_size": D(1), "tick_size": D("0.01"),
             "min_notional": D(1), "tradeable": True, "status_gieldy": "active",
             "wylaczony_przez_status": None, "min_ilosc_meta": None},
}


def _kolumny_selecta(sql: str) -> set[str]:
    lista = re.search(r"SELECT(.*?)\bFROM\b", sql, re.S | re.I).group(1)
    out = set()
    for kol in lista.split(","):
        kol = kol.strip()
        alias = re.search(r"\bAS\s+(\w+)\s*$", kol, re.I)
        out.add(alias.group(1) if alias else kol.split(".")[-1])
    return out


def _stend(monkeypatch, z_alpaki, zmiany=None):
    from services.api import db, notifications
    from services.ingest import instrumenty

    zapisy: list[tuple[str, object]] = []
    zapytania: list[tuple[str, object]] = []
    stan = {s: dict(w) for s, w in PELNE.items()}
    for s, z in (zmiany or {}).items():
        stan[s].update(z)

    async def nic():
        return None

    async def fetch_one(sql, params=None):
        assert "ref.venue" in sql
        assert params == ("alpaca",), params
        return {"demo_host": "https://paper.test", "live_host": "https://live.test"}

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        zapytania.append((p, params))
        if "exec.position" in p:
            return []
        if 'app."user"' in p:
            return [{"id": 7}]
        if "ref.instrument" in p:
            kol = _kolumny_selecta(p)
            return [{k: v for k, v in w.items() if k in kol} for w in stan.values()]
        raise AssertionError("atrapa nie zna zapytania: " + p)

    async def execute(sql, params=None):
        zapisy.append((" ".join(sql.split()), params))
        if "tradeable = false" in sql:
            for w in stan.values():
                if w["id"] == params[1]:
                    w["tradeable"] = False

    wyslane = []

    async def notify(uid, kod, tytul, tresc, **kw):
        wyslane.append(kod)
        return 1

    pytania = []

    async def _z_alpaki(engine, mode, venue, symbole):
        pytania.append((engine, mode, venue, list(symbole)))
        if isinstance(z_alpaki, BaseException):
            raise z_alpaki
        return z_alpaki

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(db, "open_pool", nic)
    monkeypatch.setattr(db, "close_pool", nic)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(notifications, "notify", notify)
    monkeypatch.setattr(instrumenty, "_z_alpaki", _z_alpaki)
    monkeypatch.setattr(sys, "argv", ["instrumenty.py", "--engine", "equity"])
    return instrumenty, zapisy, zapytania, wyslane, pytania


def _g(status):
    return {"status": status, "krok_ilosci": D("0.000000001"), "min_ilosc": None,
            "min_wartosc": D(1), "krok_ceny": D("0.01")}


def test_main_equity_ZAPISUJE_date_sprawdzenia_i_zdejmuje_wycofany(monkeypatch):
    instrumenty, zapisy, zapytania, wyslane, pytania = _stend(
        monkeypatch, {"AAPL": _g("active"), "MSFT": _g("inactive")})
    kod = _uruchom(instrumenty.main())

    assert pytania == [("equity", "demo", "alpaca", ["AAPL", "MSFT"])]
    # Instrumenty TEJ gieldy - zapytanie filtruje po kodzie gieldy.
    sel = next((p, a) for p, a in zapytania if "lot_size" in p and "v.code" in p)
    assert sel[1] == ("equity", "alpaca"), sel
    sprawdzone = [a for s, a in zapisy if "status_sprawdzony_at = now()" in s]
    assert sorted(a["id"] for a in sprawdzone) == [1, 2], (
        "data sprawdzenia ma isc dla KAZDEGO instrumentu - inaczej czujka "
        "dalej widzi brak pomiaru")
    assert {a["id"]: a["st"] for a in sprawdzone} == {1: "active", 2: "inactive"}
    assert ("UPDATE ref.instrument SET tradeable = false, wylaczony_przez_status = %s"
            " WHERE id = %s", ("wycofanie", 2)) in zapisy
    assert "instrument.delisted" in wyslane
    assert kod == instrumenty.KOD_WYCOFANIE


def test_main_equity_NIE_przepisuje_regul_zlecen(monkeypatch):
    """Krok ilosci 0.000000001 z Alpaki (fractionable) nie moze nadpisac
    `lot_size = 1` - to po cichu wlaczyloby zlecenia ulamkowe."""
    instrumenty, zapisy, *_ = _stend(
        monkeypatch, {"AAPL": _g("active"), "MSFT": _g("active")})
    kod = _uruchom(instrumenty.main())
    assert kod == 0
    assert not [s for s, _ in zapisy if "lot_size" in s or "meta =" in s], zapisy


def test_main_equity_BLAD_ALPAKI_konczy_sie_bez_zapisu(monkeypatch):
    from services.exchanges.base import AuthError

    instrumenty, zapisy, *_ = _stend(monkeypatch, AuthError("401", code="401"))
    with pytest.raises(AuthError):
        _uruchom(instrumenty.main())
    assert zapisy == [], "nieudane pytanie nie moze zapisac daty sprawdzenia"


def test_main_equity_NIKOGO_NIE_ZNA_konczy_sie_bez_zapisu(monkeypatch):
    instrumenty, zapisy, _, wyslane, _ = _stend(monkeypatch, {})
    with pytest.raises(SystemExit) as e:
        _uruchom(instrumenty.main())
    assert e.value.code not in (0, None)
    assert zapisy == [] and wyslane == [], (
        "zle pytanie zdjeloby tradeable z calego uniwersum jako wycofanie")


def test_main_equity_po_ZAWIESZENIU_oddaje_tradeable(monkeypatch):
    """Ten test wymaga, zeby SELECT naprawde pobieral `tradeable`
    i `wylaczony_przez_status`: bez nich automat nie wie, ze to on zdjal flage,
    i instrument zostaje poza uniwersum na zawsze."""
    instrumenty, zapisy, *_ = _stend(
        monkeypatch, {"AAPL": _g("active"), "MSFT": _g("active")},
        zmiany={"MSFT": {"tradeable": False, "status_gieldy": "active_not_tradable",
                         "wylaczony_przez_status": "zawieszenie"}})
    assert _uruchom(instrumenty.main()) == 0
    assert ("UPDATE ref.instrument SET tradeable = true, wylaczony_przez_status = NULL"
            " WHERE id = %s", (2,)) in zapisy, zapisy


# --- czujka -------------------------------------------------------------------

def test_czujka_wie_o_odswiezaczu_equity():
    """Bez tego czujka mowi dalej BRAK POMIARU i MILCZY o awarii odswiezacza."""
    from services.engine.dozor import venue_bez_odswiezacza

    pytano = []

    def gielda(istnieje):
        async def fetch_all(sql, params=None):
            pytano.append(params)
            return [{"?column?": 1}] if istnieje else []
        return fetch_all

    assert _uruchom(venue_bez_odswiezacza(gielda(True), "equity")) is False
    assert pytano[-1] == ("equity", "alpaca")
    # Wpis jest, ale gieldy nie ma w ref.venue - dalej brak mechanizmu.
    assert _uruchom(venue_bez_odswiezacza(gielda(False), "equity")) is True
    # Silnik bez wpisu nie pyta bazy wcale.
    n = len(pytano)
    assert _uruchom(venue_bez_odswiezacza(gielda(True), "opcje")) is True
    assert len(pytano) == n


# --- chart --------------------------------------------------------------------

def test_values_wola_KAZDY_silnik_z_VENUE_ODSWIEZANE():
    from services.ingest.instrumenty import VENUE_ODSWIEZANE

    values = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8"))
    assert set(values["instrumentyCron"]["silniki"]) == set(VENUE_ODSWIEZANE), (
        "silnik w VENUE_ODSWIEZANE bez wywolania w chartcie = alarm 'nie "
        "sprawdzano' co 10 minut; wywolanie bez wpisu = parser odrzuci flage")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")
def test_zadanie_instrumenty_w_wyrenderowanym_chartcie():
    from services.ingest.instrumenty import VENUE_ODSWIEZANE

    wynik = subprocess.run(["helm", "template", "t", str(KORZEN / "charts/trader")],
                           capture_output=True, text=True, check=True).stdout
    cron = next(d for d in yaml.safe_load_all(wynik)
                if d and d.get("kind") == "CronJob"
                and d["metadata"]["name"] == "trader-instrumenty")
    kontener = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    skrypt = kontener["command"][-1]
    for engine in VENUE_ODSWIEZANE:
        assert f'instrumenty.py --engine "{engine}"' in skrypt, skrypt
    # Klucz Alpaki jest w bazie zaszyfrowany - bez SECRETS_KEY nie ma go czym
    # odszyfrowac, a zadanie equity konczyloby sie kodem 1 przy kazdym przebiegu.
    assert "SECRETS_KEY" in {e["name"] for e in kontener["env"]}
    # Wynik zadania nie moze brac kodu OSTATNIEGO toru.
    assert skrypt.rstrip().endswith("exit $KOD"), skrypt
