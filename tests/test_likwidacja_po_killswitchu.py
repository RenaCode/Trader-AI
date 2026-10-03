"""Kill-switch LIKWIDUJE otwarte pozycje — zleceniem RYNKOWYM.

CO BYLO ZLE, ZMIERZONE

Kill-switch robil dwie rzeczy: wstrzymywal WEJSCIA i zdejmowal niewypelnione
wejscia z arkusza. Otwarta ekspozycja ZOSTAWALA (2 401,84 = 2,4% kapitalu
2026-09-27), a wyjscia szly `PostOnly`, czyli mogly sie nie wypelnic - 3 z 6
zlecen z limitem odrzucone tego dnia. Bezpiecznik zatrzymywal dokladanie
ryzyka, a nie ryzyko.

CZTERY RZECZY, KTORE TEN PLIK PILNUJE - I DLACZEGO KAZDA Z OSOBNA

  1. PROBA POZAROWA NIE ZAMYKA POZYCJI. Drill chodzi codziennie 03:21 UTC
     i uruchamia PRAWDZIWY kill-switch. Bez rozroznienia likwidacja sprzedawalaby
     cala ekspozycje raz na dobe, za pelny poslizg i za nic.
  2. DRUGI PRZEBIEG NIE SKLADA DRUGIEGO ZLECENIA. Cykl chodzi co 300 s, a
     zdarzenie zostaje czynne do RECZNEGO zwolnienia, wiec likwidacja widzi ten
     sam kill-switch dziesiatki razy. Dwa razy sprzedana pozycja dluga to
     pozycja KROTKA - objaw nie wyglada na awarie, tylko na strategie.
  3. NORMALNY HANDEL NIE STAJE SIE TAKEREM. `POSLIZG_MAKER = 0` jest poprawne
     tylko przy `PostOnly`; projekt zmierzyl, ze przy takerze Sharpe jest
     ujemny. Sciezka rynkowa istnieje wylacznie dla likwidacji.
  4. Z ZAPISU DA SIE POLICZYC POSLIZG TAKERA. `POSLIZG_TAKER = 4 pb` ma dzis
     n=0. Dla zlecen z limitem pomiar jest NIEMOZLIWY strukturalnie: wszystkie
     35 wypelnionych zlecen ma `limit_price` rowne `fill.price` co do ostatniej
     cyfry. Dla rynkowego ta rownosc nie zachodzi - ale tylko jesli cena
     odniesienia sprzed wysylki zostala ZAPISANA.

CZEGO TEN PLIK NIE ROBI: NIE SKLADA ZLECENIA NA ZADNEJ GIELDZIE, takze na
koncie demo. Adaptery sa odpytywane przez `httpx.MockTransport`, a warstwa
wyzej dostaje atrape adaptera. Pierwsze prawdziwe uruchomienie tej sciezki
nalezy do wlasciciela.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from services.engine.likwidacja import (
    ocen_zdarzenie, stan_z_gieldy)
from services.exchanges.base import (
    StanZlecenia)

KORZEN = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get("TEST_DB_URL")
TERAZ = dt.datetime(2026, 9, 27, 3, 25, tzinfo=dt.timezone.utc)


# ================================================ 1. PROBA POZAROWA a likwidacja
#
# Wszystkie asercje ponizej patrza na WARTOSC zwrocona przez `ocen_zdarzenie`,
# a nie na tresc SQL-a. Test czytajacy zapytanie po napisie sprawdza, ze napis
# tam jest; ten sprawdza, co warunek ROBI.

def _zdarzenie(*, proba_do=None, minuty_po_podniesieniu=5, limit="min_equity"):
    """Wiersz taki, jaki oddaje `SQL_ZDARZENIE`. Czas liczony wobec TERAZ."""
    podniesienie = TERAZ - dt.timedelta(minutes=minuty_po_podniesieniu)
    return dict(id=4242, reason="Kapitał 99913.31 spadł do progu 100413.00",
                limit_name=limit, triggered_at=podniesienie + dt.timedelta(minutes=1),
                limit_updated_at=podniesienie, proba_do=proba_do)


def test_proba_W_TOKU_nie_wyzwala_likwidacji():
    """Drill 03:21 UTC podnosi prog i silnik SAM strzela. Gdyby likwidacja
    ruszala na „kill-switch aktywny", zamykalaby cala ekspozycje co dobe."""
    z = _zdarzenie(proba_do=TERAZ + dt.timedelta(minutes=20))
    id_zdarzenia, powod = ocen_zdarzenie(z)
    assert id_zdarzenia is None, powod
    assert "PROBE POZAROWA" in powod


def test_proba_KTORA_ZGINELA_nadal_nie_wyzwala_likwidacji():
    """KANAREK NA WARUNEK Z `dozor.py`. Czujka pyta `proba_do > now()`, co po
    28 minutach przestaje byc prawda — a proba zabita SIGKILL-em zostawia
    podniesiony prog i CZYNNE zdarzenie. Warunek z czujki uznalby je wtedy za
    prawdziwy kill-switch i sprzedal cala ekspozycje po artefakcie proby.

    Test stawia `proba_do` GODZINE W PRZESZLOSCI i sprawdza dwie rzeczy naraz:
    ze likwidacja nadal nie rusza ORAZ ze warunek czujki dalby tu odpowiedz
    PRZECIWNA. Bez drugiej asercji ten test przechodzilby takze dla kodu, ktory
    warunku czujki nigdy nie porzucil.
    """
    przeszle = TERAZ - dt.timedelta(hours=1)
    z = _zdarzenie(proba_do=przeszle, minuty_po_podniesieniu=90)
    id_zdarzenia, powod = ocen_zdarzenie(z)
    assert id_zdarzenia is None, powod
    assert "proba_do" in powod and "przeszlosci" in powod

    # Warunek z `dozor.py` - przepisany tutaj DOSLOWNIE, zeby bylo widac, ze to
    # dwie rozne odpowiedzi na te same dane.
    warunek_czujki = (z["proba_do"] > TERAZ
                      and z["triggered_at"] >= z["limit_updated_at"])
    assert warunek_czujki is False, (
        "dane testowe nie odtwarzaja stanu, o ktory chodzi — warunek czujki "
        "musi tu dawac odpowiedz PRZECIWNA niz warunek likwidacji")


def test_prawdziwy_killswitch_WYZWALA_likwidacje():
    """Kanarek w druga strone: bez znacznika proby likwidacja MUSI ruszyc.
    Test, ktory sprawdza wylacznie „nie likwiduj", przeszedlby dla funkcji
    zwracajacej None ZAWSZE — czyli dla wylaczonego bezpiecznika."""
    z = _zdarzenie(proba_do=None)
    id_zdarzenia, powod = ocen_zdarzenie(z)
    assert id_zdarzenia == 4242
    assert "spadł do progu" in powod


def test_zdarzenie_SPRZED_podniesienia_progu_jest_prawdziwe():
    """Trwajaca proba nie unieważnia kill-switcha, ktory zadzialal PRZED nia.

    Bez dolnej granicy czasu (`triggered_at >= updated_at`) sam `proba_do`
    wyciszalby zdarzenie starsze od proby — czyli prawdziwe zatrzymanie sprzed
    trzech dni przestawalo by likwidowac pozycje o 03:21, raz na dobe, na czas
    trwania drilla.
    """
    z = _zdarzenie(proba_do=TERAZ + dt.timedelta(minutes=20))
    z["triggered_at"] = z["limit_updated_at"] - dt.timedelta(days=3)
    assert ocen_zdarzenie(z)[0] == 4242


def test_brak_zdarzenia_to_nie_to_samo_co_artefakt_proby():
    """Dwa rozne powody niezlikwidowania niczego. Z zewnatrz oba wygladaja jak
    zero zamknietych pozycji, a wymagaja przeciwnych reakcji: pierwszy zadnej,
    drugi wyczyszczenia znacznika."""
    assert ocen_zdarzenie(None) == (None, "brak czynnego kill-switcha")
    assert ocen_zdarzenie(dict()) == (None, "brak czynnego kill-switcha")


def test_proba_zwalnia_zdarzenie_PRZED_skasowaniem_znacznika():
    """KOLEJNOSC DWOCH ZAPISOW W `finally` PROBY JEST CZESCIA TEJ OSLONY.

    Gdyby prog (razem z `proba_do = NULL`) wracal PRZED zwolnieniem zdarzenia,
    miedzy tymi dwoma zapytaniami istnialoby okno, w ktorym zdarzenie proby jest
    czynne, a znacznika juz nie ma — i cykl silnika trafiony w to okno
    sprzedalby cala ekspozycje. Okno bylo milisekundowe; „milisekundy sie nie
    trafia" to premisa tego samego rodzaju, co „drill nie ma czego zamykac".

    Patrzymy na KOLEJNOSC w zrodle, bo to jedyne miejsce, w ktorym ta wada
    istnieje — samo wywolanie obu zapisow niczego nie rozstrzyga.
    """
    zrodlo = (KORZEN / "services/engine/proba_killswitcha.py").read_text(
        encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    finally_blok = kod.split("    finally:")[1]
    zwolnienie = finally_blok.index("UPDATE risk.killswitch_event SET released_at")
    przywrocenie = finally_blok.index("proba_do = NULL")
    assert zwolnienie < przywrocenie, (
        "próba kasuje `proba_do` ZANIM zwolni swoje zdarzenie — w tym oknie "
        "artefakt próby jest nieodróżnialny od prawdziwego kill-switcha, "
        "a likwidacja sprzeda po nim całą ekspozycję")


# ============================================ 2. stan dziennika z ODPOWIEDZI

def _stan(*, wypelniono, ilosc, status, cena="114.39", prowizja="0"):
    return StanZlecenia(id_wlasne="x", id_gieldy="G1", status=status,
                        ilosc=D(ilosc), wypelniono=D(wypelniono),
                        srednia_cena=D(cena), prowizja=D(prowizja),
                        prowizja_w="USDC")


def test_czesciowe_IOC_nie_jest_odrzuceniem():
    """KANAREK NA ROZSTRZYGANIE PO STATUSIE. Zlecenie rynkowe jest IOC: wypelnia,
    ile moze, i reszte ANULUJE — wiec gielda oddaje `cancelled` zarowno przy
    zerze, jak i przy 90%. Kod patrzacy na status wpisalby pozycje zamknieta
    prawie w calosci jako `odrzucona`, czyli zaraportowalby ekspozycje, ktorej
    nie ma, i odwrotnie."""
    assert stan_z_gieldy(_stan(wypelniono="8", ilosc="8.87",
                               status="cancelled"), D("8.87")) == "czesciowa"
    assert stan_z_gieldy(_stan(wypelniono="0", ilosc="8.87",
                               status="cancelled"), D("8.87")) == "odrzucona"
    assert stan_z_gieldy(_stan(wypelniono="8.87", ilosc="8.87",
                               status="cancelled"), D("8.87")) == "wypelniona"
    assert stan_z_gieldy(None, D("8.87")) == "nieznana"


# ================================================ 3. adapter: sciezka rynkowa

def _bybit(handler):
    from services.exchanges.bybit import BybitAdapter
    a = BybitAdapter("klucz", "sekret", mode="demo")
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def test_bybit_wysyla_Market_bez_ceny_i_bez_timeInForce():
    """PATRZYMY NA CIALO ZAPYTANIA, ktore naprawde poszlo, a nie na zrodlo.

    `PostOnly` na zleceniu rynkowym jest sprzecznoscia, ktora gielda odrzuca:
    likwidacja nie wyszlaby wcale, a z zewnatrz wygladaloby to na dzialajacy
    bezpiecznik. `price` z kolei nie jest bledem gieldy, tylko naszym — cena
    wyladowalaby w `exec.order.limit_price`.
    """
    wyslane = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        if "order/create" in req.url.path:
            wyslane.append(json.loads(req.content))
            return httpx.Response(200, json=dict(
                retCode=0, result=dict(orderId="G77")))
        return httpx.Response(200, json=dict(retCode=0, result=dict(list=[])))

    a = _bybit(handler)
    z = asyncio.run(a.zloz_zlecenie(symbol="SOLUSDC", strona="sell",
                                    ilosc=D("8.87"), cena_limitu=None,
                                    id_wlasne="tai-likw-1-2", rynkowe=True))
    asyncio.run(a.close())
    assert z.id_gieldy == "G77"
    body = wyslane[0]
    assert body["orderType"] == "Market"
    assert body["side"] == "Sell"
    assert body["qty"] == "8.87"
    assert "price" not in body, body
    assert "timeInForce" not in body, body
    assert body["marketUnit"] == "baseCoin", (
        "bez jawnej jednostki `qty` przy KUPNIE rynkowym na spocie Bybita "
        "znaczy KWOTĘ w walucie kwotowanej, a przy sprzedaży ILOŚĆ — odkup "
        "1000 sztuk zamówiłby 1000 USDC")
    assert body["orderLinkId"] == "tai-likw-1-2"


def test_bybit_nadal_wysyla_PostOnly_gdy_rynkowe_nie_jest_podane():
    """KANAREK: domysl musi zostac makerem. Test poprzedni przechodzilby takze
    dla adaptera, ktory wysyla `Market` ZAWSZE."""
    wyslane = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        if "order/create" in req.url.path:
            wyslane.append(json.loads(req.content))
            return httpx.Response(200, json=dict(
                retCode=0, result=dict(orderId="G78")))
        return httpx.Response(200, json=dict(retCode=0, result=dict(list=[])))

    a = _bybit(handler)
    asyncio.run(a.zloz_zlecenie(symbol="SOLUSDC", strona="sell",
                                ilosc=D("8.87"), cena_limitu=D("114.44"),
                                id_wlasne="tai-p77"))
    asyncio.run(a.close())
    assert wyslane[0]["orderType"] == "Limit"
    assert wyslane[0]["timeInForce"] == "PostOnly"
    assert wyslane[0]["price"] == "114.44"
    assert "marketUnit" not in wyslane[0]


def test_cena_limitu_przy_zleceniu_rynkowym_JEST_BLEDEM():
    """Przyjecie ceny i zignorowanie jej konczy sie wierszem `order_type='market'`
    z wypelnionym `limit_price` — a `poslizg.cena_odniesienia` wzielaby wlasnie
    ta liczbe za cene, ktora gielda dostala. Zapis nieprawdy o egzekucji."""
    a = _bybit(lambda req: httpx.Response(200, json=dict(retCode=0, result=dict())))
    with pytest.raises(ValueError, match="RYNKOWE"):
        asyncio.run(a.zloz_zlecenie(symbol="SOLUSDC", strona="sell",
                                    ilosc=D("1"), cena_limitu=D("114.44"),
                                    id_wlasne="x", rynkowe=True))
    with pytest.raises(ValueError, match="bez ceny limitu"):
        asyncio.run(a.zloz_zlecenie(symbol="SOLUSDC", strona="sell",
                                    ilosc=D("1"), cena_limitu=None,
                                    id_wlasne="x"))
    asyncio.run(a.close())


def test_alpaca_ma_WLASNA_sciezke_rynkowa():
    """Adaptery nie dziela ciala zapytania (inne API), wiec sciezke rynkowa
    trzeba bylo dolozyc DWA RAZY. Wspolny jest kontrakt, nie kod."""
    from services.exchanges.alpaca import AlpacaAdapter
    wyslane = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        if req.method == "POST" and req.url.path == "/v2/orders":
            wyslane.append(json.loads(req.content))
            return httpx.Response(200, json=dict(id="A1", status="accepted"))
        return httpx.Response(404, json=dict(message="order not found"))

    a = AlpacaAdapter("k", "s", mode="demo")
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(a.zloz_zlecenie(symbol="AAPL", strona="sell", ilosc=D("10"),
                                cena_limitu=None, id_wlasne="tai-likw-1-9",
                                rynkowe=True))
    asyncio.run(a.close())
    assert wyslane[0]["type"] == "market"
    assert "limit_price" not in wyslane[0], wyslane[0]
