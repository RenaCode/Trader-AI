"""Odrzucenie przez PostOnly musi dać się PRZYPISAĆ: arkusz i JEGO WIEK w `raw`.

CO BYŁO ZŁE — ZMIERZONE NA PRODUKCJI, NIE ZAŁOŻONE

Poprawka „cena limitu z chwili wysyłki" jest na produkcji od 2026-09-26
07:47:39Z. Zlecenia z limitem złożone PO tej chwili:

    59  09-26 09:19  filled
    60  09-26 09:19  filled
    61  09-26 13:19  rejected  EC_PostOnlyWillTakeLiquidity
    62  09-26 17:25  rejected  EC_PostOnlyWillTakeLiquidity
    63  09-27 05:11  filled
    64  09-27 05:11  rejected  EC_PostOnlyWillTakeLiquidity

Trzy na sześć. `exec.order.raw` zleceń 61/62/64 ma `powod_odmowy_gieldy`
i `venue_order_id`, a NIE MA ani bidu, ani asku, ani wieku odczytu — choć
`gielda_exec.arkusz_wysylki` jedno i drugie zwraca. Bez tych liczb dwie
przyczyny o PRZECIWNYCH lekarstwach dają identyczny wiersz:

  * arkusz był STARY (`WIEK_ARKUSZA_MAX_S = 60` dopuszcza odczyt do minuty)
    -> lekarstwo: zaciśnij próg wieku;
  * rynek ODJECHAŁ między odczytem a przyjęciem zlecenia
    -> lekarstwo: jeden tick marginesu to za mało.

Zgadywanie między nimi jest gorsze niż brak działania, bo każde z lekarstw
psuje to, co drugie naprawia.

CZEGO TE TESTY NIE ROBIĄ

Nie sprawdzają obecności klucza. Klucz da się zaspokoić pustym słownikiem,
a pusty słownik jest dokładnie tym stanem, który tę wadę zrodził. Każda asercja
niżej porównuje WARTOŚĆ: bid i ask z arkusza podanego na wejściu, wiek policzony
ze znacznika arkusza i z zegara odczytu, cenę RÓWNĄ tej, która naprawdę poszła
do `adapter.zloz_zlecenie`.

DLACZEGO WIEK NIE MOŻE POCHODZIĆ Z `submitted_at`

`exec.order.submitted_at` to `now()`, czyli znacznik TRANSAKCJI cyklu. Zlecenia
63 i 64 mają go identyczny co do mikrosekundy (2026-09-27 05:11:28.059192+00),
choć dotyczą dwóch różnych instrumentów i dwóch różnych odczytów arkusza.
Dlatego blok `raw.arkusz_wysylki` niesie WŁASNY `odczyt_at` i test niżej
sprawdza, że `wiek_s` zgadza się z różnicą `odczyt_at - arkusz_at`, a nie
z czymkolwiek, co baza podstawia sama.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal as D

import pytest

from services.engine.gielda_exec import WIEK_ARKUSZA_MAX_S, zloz_plan
from services.exchanges.base import (
    BookLevel, ExchangeError, OdmowaGieldy, OrderBook, ZlozoneZlecenie)
from services.engine.zlecenia import Filtry, PROG_ZWLOKI_BEZ_ATR

# Reguly gieldy WZIETE Z `ref.instrument` NA PRODUKCJI (tick_size / lot_size /
# min_notional, zmierzone 2026-09-27), a nie wymyslone: przy wymyslonym kroku
# ceny "cena o tick od bidu" bylaby inna liczba niz na produkcji i test
# mierzylby fikcje.
REGULY = {
    "SOLUSDC": Filtry(krok_ilosci=D("0.001"), min_ilosc=D("0.001"),
                      min_wartosc=D("5"), krok_ceny=D("0.01")),
    "ADAUSDC": Filtry(krok_ilosci=D("0.1"), min_ilosc=D("0.1"),
                      min_wartosc=D("5"), krok_ceny=D("0.0001")),
    "LINKUSDC": Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
                       min_wartosc=D("5"), krok_ceny=D("0.001")),
    "NEARUSDC": Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
                       min_wartosc=D("5"), krok_ceny=D("0.001")),
}

BAR = "2026-09-26T12:00:00+00:00"


class Kursor:
    """Atrapa kursora: zapamietuje SQL, pilnuje liczby parametrow."""

    def __init__(self, wiersze=None):
        self.sql = []
        self._wiersze = list(wiersze or [])

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        assert jedno.count("%s") == (0 if params is None else len(params)), jedno
        self.sql.append((jedno, params))

    def fetchone(self):
        return self._wiersze.pop(0) if self._wiersze else None

    def fetchall(self):
        w, self._wiersze = self._wiersze, []
        return w

    def zapytania(self, fragment):
        return [(s, p) for s, p in self.sql if fragment in s]


def arkusz(bid, ask, *, wiek_s=0.0):
    """Arkusz z chwili wysylki. `wiek_s` liczony wobec `now()` - tak samo jak
    liczy go `zloz_plan`. Staly znacznik starzalby sie z kazda godzina od
    napisania testu i przy progu 60 s test padalby sam z siebie."""
    teraz = dt.datetime.now(dt.timezone.utc)
    return OrderBook(event_time=teraz - dt.timedelta(seconds=wiek_s),
                     bids=[BookLevel(D(bid), D("100"))],
                     asks=[BookLevel(D(ask), D("100"))])


class Gielda:
    def __init__(self, *, ksiega=None, blad_ksiegi=None, odniesienie=None,
                 odmowa=None, blad_zlozenia=None):
        self._ksiega = ksiega
        self._blad_ksiegi = blad_ksiegi
        self._odniesienie = odniesienie
        self._odmowa = odmowa
        self._blad_zlozenia = blad_zlozenia
        self.zlozone = []
        self.pytania_o_arkusz = []

    async def cena_odniesienia(self, symbol, kategoria=None):
        return self._odniesienie

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        self.pytania_o_arkusz.append(dict(
            symbol=symbol, depth=depth, ksiega_egzekucji=ksiega_egzekucji))
        if self._blad_ksiegi:
            raise self._blad_ksiegi
        return self._ksiega

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return None

    async def zloz_zlecenie(self, **kw):
        self.zlozone.append(kw)
        if self._odmowa is not None:
            raise self._odmowa
        if self._blad_zlozenia is not None:
            raise self._blad_zlozenia
        return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy="G1",
                               status="new")

    async def anuluj_zlecenie(self, **kw):
        return True


def plan(*, pid, symbol, strona, cena_planu, qty, rodzaj="wejscie", atr=None):
    trigger = dict(rodzaj=rodzaj, bar=BAR)
    if rodzaj == "wyjscie":
        trigger["position_id"] = 32
    return dict(id=pid, decision_id=1000 + pid, instrument_id=3,
                side=strona, qty=D(qty), limit_price=D(cena_planu),
                symbol=symbol, quote_ccy="USDC", base_ccy=symbol[:-4],
                atr_pct=atr, trigger=trigger)


def zloz(p, *, gielda):
    cur = Kursor([dict(id=32, qty=D(p["qty"]), avg_price=D("115"))])
    ok, msg = asyncio.run(zloz_plan(cur, gielda, p, REGULY[p["symbol"]],
                                    engine="crypto", mode="demo",
                                    kategoria="spot"))
    return cur, ok, msg


def blok(cur):
    """Blok `raw.arkusz_wysylki` z JEDYNEGO wiersza `exec.order`.

    Rozpakowanie idzie przez `json.loads` tego, co poszlo do `%s` - czyli przez
    dokladnie te dane, ktore dostanie baza. Zajrzenie do slownika sprzed
    `json.dumps` przepusciloby wartosc nieserializowalna (`Decimal`, `datetime`),
    ktora na produkcji wywraca cala transakcje cyklu.
    """
    zapisy = cur.zapytania("INSERT INTO exec.order")
    assert len(zapisy) == 1, f"spodziewany JEDEN wiersz exec.order, jest {len(zapisy)}"
    raw = json.loads(zapisy[0][1][-1])
    assert "arkusz_wysylki" in raw, (
        f"`raw` bez bloku `arkusz_wysylki` - dokladnie stan zlecen 61/62/64, "
        f"w ktorym odrzucenia nie da sie przypisac. Jest: {sorted(raw)}")
    return raw["arkusz_wysylki"]


# =================================== 1. szesc zlecen z produkcji po poprawce
#
# `cena_wyslana` przepisana z `exec.order.limit_price`, `cena_planu`
# z `journal.planned_order.limit_price`. Bid i ask odtworzone z
# `market.book_snapshot` (dla tradeable=true to ksiega EGZEKUCJI, ta sama,
# ktora czyta `arkusz_wysylki`) - migawka najblizsza `submitted_at`.
#
# nr, symbol, strona, cena_planu, qty, bid, ask, cena_wyslana, rodzaj
PRODUKCJA = [
    (59, "ADAUSDC",  "sell", "0.2559", "5551.5429", "0.2536", "0.2537", "0.2538", "wyjscie"),
    (60, "SOLUSDC",  "buy",  "120.76", "7.521613",  "120.04", "120.05", "120.03", "wejscie"),
    (61, "ADAUSDC",  "buy",  "0.2580", "3960.5814", "0.2551", "0.2552", "0.2550", "wejscie"),
    (62, "ADAUSDC",  "buy",  "0.2601", "4897.0012", "0.2564", "0.2565", "0.2563", "wejscie"),
    (63, "NEARUSDC", "buy",  "5.054",  "274.703205", "5.117", "5.130",  "5.116",  "wejscie"),
    (64, "LINKUSDC", "buy",  "14.127", "81.112055", "14.092", "14.093", "14.091", "wejscie"),
]


@pytest.mark.parametrize("nr,symbol,strona,cena_planu,qty,bid,ask,wyslana,rodzaj",
                         PRODUKCJA)
def test_kazde_zlecenie_z_produkcji_zapisuje_BID_ASK_I_WIEK(
        nr, symbol, strona, cena_planu, qty, bid, ask, wyslana, rodzaj):
    """SEDNO. Blok trafia do `raw` przy KAZDYM zleceniu, nie tylko odrzuconym.

    Zapis wylacznie przy odmowie dawalby zbior bez grupy odniesienia: nie da sie
    powiedziec, czym arkusz zlecen 61/62/64 roznil sie od arkusza 59/60/63,
    jesli dla tych drugich nie ma ani jednej liczby.

    Asercje sa na WARTOSCIACH, a nie na obecnosci kluczy, i bid nie rowna sie
    askowi w zadnym wierszu - inaczej pomylenie stron przeszloby niezauwazone.
    """
    assert bid != ask, f"dane testu {nr}: bid == ask, spread zerowy nie odtwarza niczego"
    g = Gielda(ksiega=arkusz(bid, ask, wiek_s=3.0), odniesienie=D(bid))
    cur, ok, msg = zloz(plan(pid=nr, symbol=symbol, strona=strona,
                             cena_planu=cena_planu, qty=qty, rodzaj=rodzaj),
                        gielda=g)
    assert ok is True, msg

    b = blok(cur)
    assert b["bid"] == bid, f"zlecenie {nr}: w raw bid {b['bid']}, a w arkuszu {bid}"
    assert b["ask"] == ask, f"zlecenie {nr}: w raw ask {b['ask']}, a w arkuszu {ask}"
    assert b["zrodlo_ceny"] == "arkusz"
    assert 2.0 < b["wiek_s"] < 5.0, (
        f"zlecenie {nr}: wiek {b['wiek_s']} s przy arkuszu podanym jako 3 s stary")

    # WARTOSC POWIAZANA Z RZECZYWISTOSCIA, a nie przepisana obok niej:
    # `cena_wyslana` w raw musi byc TA SAMA liczba, ktora poszla do adaptera.
    assert b["cena_wyslana"] == str(g.zlozone[0]["cena_limitu"])
    # WARTOSC, NIE NAPIS: `cena_makera` obcina ogon zer (`bez_ogona`), wiec
    # 0,2550 zapisuje sie jako "0.255". Kolumna `exec.order.limit_price` jest
    # numeryczna, wiec porownanie napisow mierzyloby formatowanie, nie cene.
    assert D(b["cena_wyslana"]) == D(wyslana), (
        f"zlecenie {nr}: z arkusza {bid}/{ask} przy kroku "
        f"{REGULY[symbol].krok_ceny} wyszlo {b['cena_wyslana']}, a produkcja "
        f"wyslala {wyslana}")
    assert D(b["cena_planu"]) == D(cena_planu)


def test_dane_z_produkcji_ZGADZAJA_SIE_z_zapisanym_statusem():
    """Kontrola danych, nie kodu. Gdyby ceny w tabeli wyzej byly przepisane
    z bledem, caly plik mierzylby fikcje i PRZECHODZILBY.

    Mowi wprost, co te liczby znacza: kazda cena wyslana lezala po stronie
    PASYWNEJ odtworzonego arkusza (o tick od bidu przy kupnie, o tick nad
    askiem przy sprzedazy), czyli PostOnly nie mial jej prawa odrzucic w chwili
    ODCZYTU. Trzy z nich odrzucil mimo tego - i to jest cala zagadka.
    """
    for nr, symbol, strona, cena_planu, _q, bid, ask, wyslana, _r in PRODUKCJA:
        krok = REGULY[symbol].krok_ceny
        if strona == "buy":
            assert D(wyslana) < D(ask), f"{nr}: kupno {wyslana} nie ponizej asku {ask}"
            assert D(wyslana) == D(bid) - krok, f"{nr}: kupno nie o tick od bidu"
        else:
            assert D(wyslana) > D(bid), f"{nr}: sprzedaz {wyslana} nie powyzej bidu {bid}"
            assert D(wyslana) == D(ask) + krok, f"{nr}: sprzedaz nie o tick nad askiem"
        # Dryf miedzy cena planu a wyslana miesci sie w progu bez ATR (3%),
        # wiec bramka `ocena_zwloki` niczego tu nie wstrzymuje - tak samo jak
        # na produkcji, gdzie wszystkie szesc zlecen na gielde poszlo.
        dryf = abs(D(wyslana) - D(cena_planu)) / D(cena_planu)
        assert dryf < PROG_ZWLOKI_BEZ_ATR, f"{nr}: dryf {dryf} poza progiem testu"


# ============================== 2. kanarek: miara REAGUJE na wejscie

def test_dwa_rozne_arkusze_daja_dwa_rozne_wpisy_w_raw():
    """Identyczny blok przy wyraznie roznym arkuszu znaczylby, ze arkusz do
    zapisu nie dochodzi - ten sam ksztalt wady, ktora ta poprawka zamyka.
    Cena planu jest w obu przebiegach TA SAMA, wiec kazda roznica pochodzi
    wylacznie z arkusza."""
    def zapis(bid, ask, wiek_s):
        g = Gielda(ksiega=arkusz(bid, ask, wiek_s=wiek_s), odniesienie=D(bid))
        cur, ok, msg = zloz(plan(pid=61, symbol="ADAUSDC", strona="buy",
                                 cena_planu="0.2580", qty="3960.5814"), gielda=g)
        assert ok is True, msg
        return blok(cur)

    swiezy = zapis("0.2551", "0.2552", 1.0)
    stary = zapis("0.2520", "0.2522", 45.0)

    assert (swiezy["bid"], swiezy["ask"]) == ("0.2551", "0.2552")
    assert (stary["bid"], stary["ask"]) == ("0.2520", "0.2522")
    assert swiezy["cena_wyslana"] != stary["cena_wyslana"]
    # WIEK TEZ MUSI REAGOWAC. Zapisanie stalej (np. zawsze 0) przeszloby
    # kazdy test pytajacy tylko o obecnosc klucza.
    assert stary["wiek_s"] - swiezy["wiek_s"] > 40, (
        f"wiek nie reaguje na znacznik arkusza: {swiezy['wiek_s']} wobec "
        f"{stary['wiek_s']} przy roznicy 44 s na wejsciu")


def test_wiek_s_ZGADZA_SIE_z_roznica_odczyt_at_minus_arkusz_at():
    """PUNKT, W KTORYM WIEK STAJE SIE SPRAWDZALNY BEZ ZAUFANIA.

    `submitted_at` to `now()`, czyli znacznik TRANSAKCJI: zlecenia 63 i 64 maja
    go identyczny co do mikrosekundy, wiec wiek policzony z niego opisywalby
    dwa rozne odczyty jedna liczba. Blok niesie wiec WLASNY `odczyt_at`, a ten
    test sprawdza, ze `wiek_s` jest z niego policzony - i ze `arkusz_at` to
    znacznik OFERTY, a nie powtorzony zegar odczytu.
    """
    g = Gielda(ksiega=arkusz("14.092", "14.093", wiek_s=27.5),
               odniesienie=D("14.092"))
    cur, ok, msg = zloz(plan(pid=64, symbol="LINKUSDC", strona="buy",
                             cena_planu="14.127", qty="81.112055"), gielda=g)
    assert ok is True, msg
    b = blok(cur)

    odczyt = dt.datetime.fromisoformat(b["odczyt_at"])
    arkusz_at = dt.datetime.fromisoformat(b["arkusz_at"])
    assert odczyt.tzinfo is not None and arkusz_at.tzinfo is not None
    assert arkusz_at < odczyt, (
        "znacznik arkusza nie moze byc pozniejszy od chwili odczytu")
    assert abs((odczyt - arkusz_at).total_seconds() - b["wiek_s"]) < 0.01, (
        f"wiek_s {b['wiek_s']} nie wynika z odczyt_at - arkusz_at "
        f"({(odczyt - arkusz_at).total_seconds()})")
    assert 27.0 < b["wiek_s"] < 29.0

    # `submitted_at` NIE JEST parametrem - baza podstawia `now()`. Gdyby kiedys
    # stal sie parametrem, wiek nadal nie moze z niego pochodzic.
    sql, _ = cur.zapytania("INSERT INTO exec.order")[0]
    assert "submitted_at" in sql and "now()" in sql


# ============== 3. dwie przyczyny o PRZECIWNYCH lekarstwach, dwa rozne slady

def test_arkusz_ZA_STARY_zostawia_ZNACZNIK_I_WIEK_mimo_odrzucenia_odczytu():
    """PRZYCZYNA PIERWSZA: "arkusz byl stary".

    Gdy `arkusz_wysylki` odrzuca odczyt po `WIEK_ARKUSZA_MAX_S`, oferty znikaja,
    ale znacznik i wiek SA ZNANE - i wlasnie one rozstrzygaja, ze to ta
    przyczyna, a nie ruch rynku. Zgubienie ich tutaj zostawialoby najwazniejszy
    przypadek bez sladu: cena idzie wtedy z `cena_odniesienia` (`lastPrice`),
    ktora na zleceniu 54 lezala JEDENASCIE tickow nad askiem.
    """
    wiek = WIEK_ARKUSZA_MAX_S * 2
    g = Gielda(ksiega=arkusz("0.2551", "0.2552", wiek_s=wiek),
               odniesienie=D("0.2551"))
    cur, ok, msg = zloz(plan(pid=61, symbol="ADAUSDC", strona="buy",
                             cena_planu="0.2580", qty="3960.5814"), gielda=g)
    assert ok is True, msg
    b = blok(cur)

    assert b["wiek_s"] > WIEK_ARKUSZA_MAX_S, (
        f"wiek {b['wiek_s']} s nie przekracza progu {WIEK_ARKUSZA_MAX_S} s, "
        f"choc arkusz podano jako {wiek} s stary")
    assert b["bid"] == "0.2551" and b["ask"] == "0.2552"
    assert b["powod_braku"] and str(WIEK_ARKUSZA_MAX_S) in b["powod_braku"]
    # ZRODLO CENY MUSI POKAZAC ZMIANE. Arkusz odrzucony -> cena z `lastPrice`.
    assert b["zrodlo_ceny"] == "cena_odniesienia", (
        "arkusz odrzucony po progu wieku, a raw twierdzi, ze cena z niego "
        "pochodzi - to jest opis przyczyny wskazujacy nie to zrodlo, ktore "
        "zadzialalo")


def test_arkusz_NIEODCZYTANY_zapisuje_brak_jako_brak_a_nie_jako_zero():
    """Arkusz nieodczytany to nie arkusz o zerowym bidzie. Zapisanie `0`
    dawaloby spread rowny calej cenie i liczylo sie w statystyce jako pomiar."""
    g = Gielda(blad_ksiegi=ExchangeError("503 z hosta egzekucji"),
               odniesienie=D("0.2551"))
    cur, ok, msg = zloz(plan(pid=61, symbol="ADAUSDC", strona="buy",
                             cena_planu="0.2580", qty="3960.5814"), gielda=g)
    assert ok is True, msg
    b = blok(cur)

    assert b["bid"] is None and b["ask"] is None
    assert b["arkusz_at"] is None and b["wiek_s"] is None
    assert "NIEODCZYTANY" in b["powod_braku"]
    assert b["zrodlo_ceny"] == "cena_odniesienia"
    assert b["cena_wyslana"] == str(g.zlozone[0]["cena_limitu"])


def test_zrodlo_ceny_ROZNICUJE_arkusz_od_lastPrice():
    """Kanarek na sam rozdzial zrodel: te same dane wejsciowe poza jednym -
    czy arkusz sie odczytal - musza dac dwie rozne nazwy zrodla ORAZ dwie
    rozne ceny. Jedna nazwa na oba przypadki znaczylaby, ze pole nie mierzy
    niczego."""
    def zapis(g):
        cur, ok, msg = zloz(plan(pid=62, symbol="ADAUSDC", strona="buy",
                                 cena_planu="0.2601", qty="4897.0012"), gielda=g)
        assert ok is True, msg
        return blok(cur)

    # Arkusz: bid 0.2564 -> kupno o tick nizej. lastPrice 0.2570 (11 tickow
    # nad askiem, tak jak zmierzono na zleceniu 54) -> kupno 0.2569.
    z_arkusza = zapis(Gielda(ksiega=arkusz("0.2564", "0.2565"),
                             odniesienie=D("0.2570")))
    z_lastprice = zapis(Gielda(blad_ksiegi=ExchangeError("brak arkusza"),
                               odniesienie=D("0.2570")))

    assert z_arkusza["zrodlo_ceny"] == "arkusz"
    assert z_lastprice["zrodlo_ceny"] == "cena_odniesienia"
    assert z_arkusza["cena_wyslana"] == "0.2563"
    assert z_lastprice["cena_wyslana"] == "0.2569"


# ================= 4. pozostale sciezki zapisu wiersza `exec.order`

def test_ODMOWA_GIELDY_tez_dostaje_blok():
    """Sciezka `odmowa_gieldy`: gielda odpowiedziala `retCode` i zlecenia NIE MA.
    Bez bloku ten wiersz jest dokladnie tym, co dzis mamy dla 61/62/64 -
    powod odmowy bez arkusza, czyli bez mozliwosci przypisania."""
    g = Gielda(ksiega=arkusz("14.092", "14.093", wiek_s=0.4),
               odniesienie=D("14.092"),
               odmowa=OdmowaGieldy("PostOnly will take liquidity",
                                   code="EC_PostOnlyWillTakeLiquidity"))
    cur, ok, msg = zloz(plan(pid=64, symbol="LINKUSDC", strona="buy",
                             cena_planu="14.127", qty="81.112055"), gielda=g)
    assert ok is False
    b = blok(cur)
    assert (b["bid"], b["ask"]) == ("14.092", "14.093")
    assert b["wiek_s"] < 2.0
    assert b["zrodlo_ceny"] == "arkusz"
    assert b["cena_wyslana"] == "14.091" == str(g.zlozone[0]["cena_limitu"])


def test_ODPOWIEDZ_NIE_DOTARLA_tez_dostaje_blok():
    """Status `unknown` znaczy "zlecenie moglo wejsc" - wiec arkusz, po ktorym
    poszlo, jest potrzebny tym bardziej, nie mniej."""
    g = Gielda(ksiega=arkusz("14.092", "14.093"), odniesienie=D("14.092"),
               blad_zlozenia=ExchangeError("przekroczony czas"))
    cur, ok, _ = zloz(plan(pid=64, symbol="LINKUSDC", strona="buy",
                           cena_planu="14.127", qty="81.112055"), gielda=g)
    assert ok is False
    b = blok(cur)
    assert (b["bid"], b["ask"]) == ("14.092", "14.093")
    assert b["cena_wyslana"] == "14.091"


def test_ODMOWA_PRZED_ZLOZENIEM_tez_dostaje_blok():
    """Wejscie wstrzymane progiem dryfu. Zlecenie nie poszlo, ale pytanie
    "jaki byl wtedy arkusz" zadaje sie tu tak samo - i tylko blok na nie
    odpowiada."""
    # Cena planu 0,30 wobec arkusza 0,2551/0,2552 to dryf ~15%, czyli daleko
    # poza progiem bez ATR (3%).
    g = Gielda(ksiega=arkusz("0.2551", "0.2552", wiek_s=5.0),
               odniesienie=D("0.2551"))
    cur, ok, msg = zloz(plan(pid=61, symbol="ADAUSDC", strona="buy",
                             cena_planu="0.30", qty="3960.5814"), gielda=g)
    assert ok is False, msg
    assert g.zlozone == [], "zlecenie mialo NIE pojsc na gielde"
    b = blok(cur)
    assert (b["bid"], b["ask"]) == ("0.2551", "0.2552")
    assert 4.0 < b["wiek_s"] < 7.0
    assert b["cena_planu"] == "0.30"


def test_blok_NIE_dodaje_ANI_JEDNEGO_zapytania_do_gieldy():
    """Warunek zadania i jedyna bezpieczna droga: `slad` powstaje z odpowiedzi,
    ktora i tak odebralismy. Dodatkowy odczyt arkusza po to, zeby go zapisac,
    opisywalby CHWILE PO wyslaniu zlecenia, a nie chwile wysylki - czyli
    zapisywalby liczbe wygladajaca poprawnie i odpowiadajaca na inne pytanie.
    """
    g = Gielda(ksiega=arkusz("14.092", "14.093"), odniesienie=D("14.092"))
    cur, ok, msg = zloz(plan(pid=64, symbol="LINKUSDC", strona="buy",
                             cena_planu="14.127", qty="81.112055"), gielda=g)
    assert ok is True, msg
    assert g.pytania_o_arkusz == [
        dict(symbol="LINKUSDC", depth=1, ksiega_egzekucji=True)]
    assert blok(cur)["bid"] == "14.092"


def test_blok_ma_PELNY_zestaw_pol_i_jest_SERIALIZOWALNY():
    """Blok siedzi pod JEDNYM kluczem, bo `raw` dostaje pozniej `raw || meta`
    z `rozstrzygnij_plan` - rozsypany po korzeniu dalby sie temu dopiskowi
    czesciowo nadpisac. `json.dumps` ma go przyjac bez `default=str`: inaczej
    `Decimal` w wartosci wywraca cala transakcje cyklu na produkcji.

    Zestaw pol porownany na ROWNOSC, a nie na zawieranie: pole, ktore kiedys
    z bloku wypadnie, ma tu zapalic czerwone, a nie zniknac po cichu."""
    g = Gielda(ksiega=arkusz("5.117", "5.130"), odniesienie=D("5.117"))
    cur, ok, msg = zloz(plan(pid=63, symbol="NEARUSDC", strona="buy",
                             cena_planu="5.054", qty="274.703205"), gielda=g)
    assert ok is True, msg
    raw = json.loads(cur.zapytania("INSERT INTO exec.order")[0][1][-1])
    assert raw["execution"] == "gielda"
    assert set(raw["arkusz_wysylki"]) == {
        "odczyt_at", "arkusz_at", "wiek_s", "bid", "ask", "powod_braku",
        "zrodlo_ceny", "cena_rynkowa", "cena_planu", "cena_wyslana",
        # Wiek odczytu NASZYM zegarem w chwili POST-a (2026-10-03, audyt E1).
        "wiek_przy_wysylce_s"}
