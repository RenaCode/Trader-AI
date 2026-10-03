"""Dwa miejsca w adapterze giełdy, w których BRAK ROZRÓŻNIENIA kosztuje pozycję.

1. `stan_zlecenia` brało `lista[0]` bez sprawdzenia, że zwrócone zlecenie ma
   NASZ `orderLinkId`. Na tym odczycie stoi cała idempotencja: `zloz_zlecenie`
   pyta o stan ZANIM cokolwiek wyśle i przy odpowiedzi niepustej uznaje, że
   zlecenie już istnieje. Gdyby giełda zignorowała filtr, przypisalibyśmy sobie
   cudze wypełnienie albo uznali za istniejące zlecenie, którego nie ma.

2. Wszystko poza 404/429/5xx/kluczem wpadało w ogólny `ExchangeError`, a proces
   po pięciu cyklach się kończył. „Giełda nas zablokowała" wyglądało więc
   dokładnie tak samo jak „padła baza" — a to dwie różne noce i dwa różne
   miejsca, do których trzeba pójść. Przy martwym kluczu czujka żywotności
   milczy, bo silnik żyje i bije, tylko giełda go nie słucha; otwarte pozycje
   zostają, bo WYJŚCIA są odrzucane tak samo jak wejścia.
"""
import asyncio
from decimal import Decimal as D

import httpx
import pytest

from services.exchanges.base import (
    AuthError, BladIpKlucza, ExchangeError, KontoZamrozone,
)
from services.exchanges.bybit import BybitAdapter


def bez_czekania(monkeypatch):
    """Ponawianie odczekuje sekundy — w teście nie ma na co czekać."""
    async def natychmiast(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", natychmiast)


def gielda(handler):
    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k


def zlecenie(link_id: str, *, status="Filled", qty="10", wyp="10", cena="100"):
    """Wiersz zlecenia w kształcie, w jakim oddaje go Bybit V5."""
    return {"orderLinkId": link_id, "orderId": f"gield-{link_id}",
            "orderStatus": status, "qty": qty, "cumExecQty": wyp,
            "avgPrice": cena, "cumExecFee": "0.01"}


def gielda_z_listami(realtime: list, history: list):
    """Giełda oddająca zadane listy na dwóch endpointach stanu zlecenia."""
    zapytania = []

    def handler(req):
        zapytania.append(str(req.url))
        lista = realtime if "/v5/order/realtime" in req.url.path else history
        return httpx.Response(200, json={"retCode": 0, "result": {"list": lista}})

    return gielda(handler), zapytania


# ===================================================================== ZNALEZISKO 1
# Tożsamość zlecenia sprawdzona, nie założona.

def test_cudze_zlecenie_z_listy_otwartych_KONCZY_SIE_BLEDEM():
    """Sedno. Giełda odpowiedziała czymś, o co nie pytaliśmy — filtr po
    `orderLinkId` został zignorowany. Bez tego sprawdzenia przypisalibyśmy sobie
    cudze wypełnienie: 10 sztuk po 100, których nikt u nas nie zamawiał."""
    k, _ = gielda_z_listami([zlecenie("tai-KTOS-INNY")], [])
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.stan_zlecenia("tai-nasze-42"))
    assert e.value.code == "tozsamosc-zlecenia"
    assert "tai-KTOS-INNY" in str(e.value), "komunikat ma pokazać, co przyszło"
    assert not e.value.retryable, "ponowienie nie zmieni odpowiedzi giełdy"


def test_cudze_zlecenie_z_HISTORII_tez_konczy_sie_bledem():
    """Sprawdzenie musi obejmować OBA endpointy. Historia jest tą ścieżką, którą
    idzie odczyt po timeoucie — zlecenie zdążyło się wypełnić i zniknąć z listy
    otwartych. Gdyby asercja stała tylko przy pierwszym endpointcie, dziura
    zostałaby dokładnie tam, gdzie odczyt jest najbardziej potrzebny."""
    k, _ = gielda_z_listami([], [zlecenie("tai-KTOS-INNY")])
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.stan_zlecenia("tai-nasze-42"))
    assert e.value.code == "tozsamosc-zlecenia"
    assert "/v5/order/history" in str(e.value), "komunikat ma wskazać endpoint"


def test_NASZE_zlecenie_jest_wybierane_z_listy_a_nie_pierwsze():
    """Kanarek do dwóch powyższych i sedno poprawki: nie chodzi o odrzucanie
    list mieszanych, tylko o WYBÓR po tożsamości. `lista[0]` oddałoby tu cudze
    zlecenie, choć nasze leży wiersz niżej."""
    k, _ = gielda_z_listami([zlecenie("tai-KTOS-INNY", qty="99", wyp="99"),
                             zlecenie("tai-nasze-42", qty="10", wyp="4")], [])
    stan = asyncio.run(k.stan_zlecenia("tai-nasze-42"))
    assert stan.id_wlasne == "tai-nasze-42"
    assert stan.id_gieldy == "gield-tai-nasze-42"
    assert stan.ilosc == D("10") and stan.wypelniono == D("4")
    assert stan.czesciowe, "wypełnienie częściowe musi przejść przez odczyt"


def test_brak_zlecenia_na_obu_endpointach_to_None_a_nie_blad():
    """Pusta lista znaczy „giełda go nie zna" i to jest NORMALNA odpowiedź —
    właśnie ona pozwala złożyć zlecenie. Gdyby i ona kończyła się błędem,
    idempotencja zablokowałaby handel zamiast go chronić."""
    k, zapytania = gielda_z_listami([], [])
    assert asyncio.run(k.stan_zlecenia("tai-nasze-42")) is None
    # TRZY odczyty od 2026-10-03: trzeci to `/v5/order/realtime` z `openOnly=1`,
    # ktory domyka luke "history may delay" (audyt E3). "Nie zna" wolno
    # powiedziec dopiero po wszystkich trzech.
    assert len(zapytania) == 3, "wszystkie trzy odczyty muszą zostać wykonane"
    assert "openOnly=1" in zapytania[-1]


def test_cudze_zlecenie_NIE_MOZE_zablokowac_zlozenia_ani_wyslac_drugiego():
    """Konsekwencja, dla której to sprawdzenie istnieje.

    `zloz_zlecenie` pyta o stan przed wysłaniem czegokolwiek. Przy cudzym
    wierszu bez asercji zwróciłoby `juz_istnialo=True` dla zlecenia, którego
    NIGDY nie złożyło — plan wyglądałby na wykonany, a na giełdzie nie byłoby
    nic. Poprawna reakcja to błąd PRZED POST-em: żadnego zlecenia nie wysłano.
    """
    posty = []

    def handler(req):
        if req.method == "POST":
            posty.append(str(req.url))
            return httpx.Response(200, json={"retCode": 0, "result": {"orderId": "x"}})
        return httpx.Response(200, json={
            "retCode": 0, "result": {"list": [zlecenie("tai-KTOS-INNY")]}})

    k = gielda(handler)
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.zloz_zlecenie(symbol="SOLUSDC", strona="buy", ilosc=D("10"),
                                    cena_limitu=D("100"), id_wlasne="tai-nasze-42"))
    assert e.value.code == "tozsamosc-zlecenia"
    assert posty == [], "zlecenie NIE mogło zostać wysłane na podstawie cudzego odczytu"


# ===================================================================== ZNALEZISKO 2
# Błędy giełdy rozróżnione tam, gdzie zmieniają reakcję.

def blad_z_kodem(kod: int, msg: str = "komunikat gieldy", *, metoda="GET"):
    """Podnosi błąd, który adapter dostaje przy `retCode` != 0 (HTTP 200).

    W trybie POST błąd zwracamy WYŁĄCZNIE na POST-cie: `zloz_zlecenie` zaczyna
    od odczytu stanu, więc kod wstawiony też w GET zatrzymałby sprawdzenie przed
    ścieżką zapisu — i test przechodziłby, nie sprawdzając niczego (tak właśnie
    pierwsza wersja tego testu przeżyła kanarka na klasyfikacji w `_post`).
    """
    def handler(req):
        if metoda == "POST" and req.method != "POST":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        return httpx.Response(200, json={"retCode": kod, "retMsg": msg})

    k = gielda(handler)
    with pytest.raises(ExchangeError) as e:
        if metoda == "POST":
            asyncio.run(k.zloz_zlecenie(symbol="SOLUSDC", strona="buy", ilosc=D("10"),
                                        cena_limitu=D("100"), id_wlasne="tai-1"))
        else:
            asyncio.run(k._get("/v5/account/info"))
    return e.value


def test_niezgodnosc_IP_ma_WLASNA_klase_i_mowi_gdzie_isc():
    """10010: klucz i podpis są poprawne, tylko adres wyjściowy nie jest na
    liście przypiętej do klucza. Sklejone z ogólnym błędem klucza posyła
    człowieka o trzeciej w nocy do Vaulta po sekret, którego nikt nie zepsuł."""
    e = blad_z_kodem(10010, "Unmatched IP")
    assert isinstance(e, BladIpKlucza)
    assert e.code == "10010"
    tresc = str(e)
    assert "IP" in tresc, "bez słowa IP komunikat nie prowadzi do przyczyny"
    assert "PANELU GIELDY" in tresc.upper(), "ma wskazać, gdzie iść"
    assert "pozycj" in tresc, "ma powiedzieć, że nie da się zamknąć pozycji"


def test_zamrozone_konto_to_NIE_blad_klucza():
    """Klucz działa, podpis przechodzi — zablokowane jest KONTO. Gdyby to był
    `AuthError`, UI i alarm mówiłyby „popraw klucz", a poprawianie klucza nie
    odmrozi konta. Rozróżnienie decyduje, do kogo iść: do wsparcia giełdy."""
    e = blad_z_kodem(10027, "Trading Banned")
    assert isinstance(e, KontoZamrozone)
    assert not isinstance(e, AuthError), "zamrożone konto to nie problem poświadczeń"
    assert e.code == "10027"
    assert "KONTO" in str(e).upper() and "PANELU GIELDY" in str(e).upper()


def test_zamrozone_konto_i_niezgodnosc_IP_to_ROZNE_klasy():
    """Kanarek do dwóch powyższych: obie sytuacje kończą handel, ale prowadzą
    do DWÓCH RÓŻNYCH miejsc — konfiguracji sieci klastra i panelu giełdy.
    Wspólna klasa zabrałaby dokładnie tę informację, po którą sięga się nocą."""
    ip = blad_z_kodem(10010)
    zamrozone = blad_z_kodem(10024, "Compliance rules triggered")
    assert not isinstance(zamrozone, BladIpKlucza)
    assert not isinstance(ip, KontoZamrozone)
    assert "IP" not in str(zamrozone), (
        "komunikat o zamrożonym koncie nie może kierować na adres wyjściowy")


def test_awaria_wymagajaca_CZLOWIEKA_jest_odrozniona_od_zwyklego_bledu():
    """Sedno drugiego znaleziska. Zwykły błąd giełdy mija albo dotyczy jednego
    zapytania; martwy klucz i zamrożone konto nie miną same i nie objawią się
    ani w czujce żywotności (silnik żyje i bije), ani w ponowieniach."""
    for kod in (10010, 10024, 10027, 10003, 33004):
        e = blad_z_kodem(kod)
        assert e.wymaga_czlowieka, f"kod {kod} wymaga człowieka, a nie ponowienia"
        assert not e.retryable, f"kod {kod} ponawiany w kółko tylko opóźnia alarm"

    zwykly = blad_z_kodem(110001, "order not exists")
    assert not zwykly.wymaga_czlowieka, (
        "zwykły błąd giełdy nie może budzić nikogo w nocy")
    assert type(zwykly) is ExchangeError


def test_martwy_klucz_zatrzymuje_takze_WYJSCIE_i_jest_tak_samo_nazwany(monkeypatch):
    """Ta sama klasyfikacja na ścieżce POST, czyli tam, gdzie składa się i
    ODWOŁUJE zlecenia. Rozjazd list kodów między odczytem a zapisem znaczyłby,
    że ta sama awaria brzmi inaczej zależnie od tego, co ją napotkało — a przy
    zamykaniu pozycji brzmi najważniej."""
    bez_czekania(monkeypatch)
    for kod, klasa in ((10010, BladIpKlucza), (10027, KontoZamrozone)):
        e = blad_z_kodem(kod, metoda="POST")
        assert isinstance(e, klasa), f"POST klasyfikuje {kod} inaczej niż GET"


def test_bledy_wymagajace_czlowieka_NIE_sa_ponawiane(monkeypatch):
    """Trzy próby przy martwym kluczu to trzy razy dłuższa cisza przed alarmem
    i zjedzony limit zapytań — a odpowiedź i tak będzie ta sama."""
    bez_czekania(monkeypatch)
    licznik = {"n": 0}

    def handler(_req):
        licznik["n"] += 1
        return httpx.Response(200, json={"retCode": 10010, "retMsg": "Unmatched IP"})

    k = gielda(handler)
    with pytest.raises(BladIpKlucza):
        asyncio.run(k._get("/v5/account/info"))
    assert licznik["n"] == 1, f"jedna próba wystarczy, było {licznik['n']}"


def test_limit_zapytan_w_retCode_jest_stanem_PRZEJSCIOWYM(monkeypatch):
    """10006 siedziało w rodzinie błędów uwierzytelnienia, więc chwilowe
    zdławienie wyglądało jak zły klucz: nie było ponawiane, mimo że wystarczyło
    odczekać sekundę, i kierowało szukanie na poświadczenia."""
    bez_czekania(monkeypatch)
    licznik = {"n": 0}

    def handler(_req):
        licznik["n"] += 1
        if licznik["n"] < 3:
            return httpx.Response(200, json={"retCode": 10006, "retMsg": "too many visits"})
        return httpx.Response(200, json={"retCode": 0, "result": {"ok": 1}})

    k = gielda(handler)
    assert asyncio.run(k._get("/v5/account/info")) == {"ok": 1}
    assert licznik["n"] == 3, "limit zapytań ma być ponawiany, nie brany za zły klucz"
