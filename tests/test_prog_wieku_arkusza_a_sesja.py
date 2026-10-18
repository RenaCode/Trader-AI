"""Próg wieku arkusza wobec kalendarza sesji — i premisa, która jest FAŁSZYWA.

PREMISA, KTÓRĄ TEN PLIK OBALA (migracja 0060, sekcja "ZALEZNOSC, KTORA MUSI BYC
ZAMKNIETA")

    "Poza sesja KAZDY arkusz akcji jest starszy niz 60 s, wiec dopoki prog
     wieku nie liczy sie z kalendarza sesji, tor equity odmawia kazdego wejscia
     (powod `brak_ceny_wysylki`)."

Obie połowy tego zdania są nieprawdziwe, każda z innego powodu, i obie dawały
się sprawdzić bez wdrożenia czegokolwiek:

  1. POZA SESJĄ EGZEKUCJA NIE JEST W OGÓLE WOŁANA. `loop.run_once` ma bramkę
     sesji (BRAMKA 1) PRZED decyzjami, a `zloz_oczekujace` stoi na samym KOŃCU
     przebiegu. Cykl poza sesją kończy się na `return stats` w bramce, więc
     `arkusz_wysylki` nie jest wołany ani razu i do `journal.pominiecie` nie
     trafia ŻADEN wiersz. Mierzy to `test_poza_sesja_petla_nie_dotyka_gieldy`
     na prawdziwej bazie i adapterze, który wybucha przy każdym kontakcie.

  2. GDYBY BYŁA WOŁANA, WYNIKIEM NIE BYŁABY ODMOWA — BYŁOBY ZLECENIE.
     `brak_ceny_wysylki` wymaga, żeby zawiodły OBA odczyty ceny. Próg wieku
     z 0059 pilnuje tylko JEDNEGO z nich (`arkusz_wysylki`). Drugi,
     `adapter.cena_odniesienia`, oddaje goły `Decimal` BEZ ZNACZNIKA CZASU
     (`alpaca.py`: `/v2/stocks/trades/latest`), więc jego wieku nie da się
     sprawdzić — i poza sesją ten odczyt SIĘ UDAJE, oddając cenę zamknięcia.
     `sprawdz_zlecenie` podstawia ją jako "cenę z chwili wysyłki", a zlecenie
     idzie na giełdę.

     Kierunek jest więc ODWROTNY do opisanego: nie "bezpieczna odmowa", tylko
     ciche wysłanie ceny sprzed godzin. Próg wieku fail-open przez nieosłonięte
     źródło zapasowe.

DLACZEGO TO NIE JEST TYLKO TEORIA, MIMO BRAMKI Z PUNKTU 1

Stan sesji czyta się RAZ, na początku cyklu (`loop.py`, `stan_sesji` przed
`run_once`), a `zloz_oczekujace` wysyła na jego KOŃCU. Sesja zamknięta
w międzyczasie nie jest przez nikogo zauważona: cykl wystartowany 15:59 czasu
Nowego Jorku przechodzi bramkę jako "otwarta" i wysyła zlecenia po 16:00, gdy
arkusz jest już zamrożony. Alpaca przyjmuje wtedy zlecenie `day` i KOLEJKUJE JE
DO NASTĘPNEGO OTWARCIA (`alpaca.py`, nagłówek modułu: "Zlecenie `day` poza
sesja czeka do otwarcia") — czyli wypełnia je nazajutrz na otwarciu, po cenie
z wczorajszego zamknięcia, przez lukę nocną, bez decyzji, która by to popierała.

CZEGO TEN PLIK NIE SPRAWDZA

Nie sprawdza, że Alpaca faktycznie kolejkuje zlecenie `day` złożone po
zamknięciu — to twierdzenie o giełdzie, a testy tu nie składają zleceń.
Nie mierzy też, jak długo trwa cykl equity na produkcji, czyli jak szeroka
naprawdę jest ta szczelina.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from decimal import Decimal as D
from pathlib import Path

import pytest

from services.engine.gielda_exec import WIEK_ARKUSZA_MAX_S, zloz_plan
from services.engine.sesja import StanSesji, stan_sesji
from services.engine.zlecenia import Filtry
from services.exchanges.base import BookLevel, OrderBook, ZlozoneZlecenie

KORZEN = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get("TEST_DB_URL")

# Reguly instrumentu equity. Akcje USA: tick 0,01 USD, ilosc calkowita.
AAPL = Filtry(krok_ilosci=D("1"), min_ilosc=D("1"),
              min_wartosc=D("1"), krok_ceny=D("0.01"))

# ATR dzienny AAPL w PROCENTACH ceny - rzad wielkosci z rynku, nie liczba
# okragla. Prog zwloki to 1,5x ATR, wiec przy 1,6% granica stoi na 2,4%:
# ceny w tym tescie mieszcza sie w pasmie i NIE sa przez nie odrzucane.
# To jest celowe - test ma pokazac, co robi bramka SESJI, a nie bramka pasma.
ATR_AAPL = 1.6

# Swieca decyzyjna: 15:00-16:00 czasu Nowego Jorku = 19:00-20:00 UTC.
BAR = "2026-09-25T19:00:00+00:00"

# Plan powstal na zamknieciu swiecy 15:00, zlecenie idzie ~75 minut pozniej,
# czyli PO 16:00 - gdy arkusz jest zamrozony, a ostatnia transakcja to
# transakcja zamykajaca sesje.
LIMIT_Z_PLANU = "232.10"
OSTATNIA_TRANSAKCJA = "232.45"


class Kursor:
    """Atrapa kursora: zapamietuje SQL i pilnuje liczby parametrow."""

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

    def powody_pominiec(self):
        """Wartosci `powod` z INSERT-ow do `journal.pominiecie`."""
        return [p[4] for s, p in self.sql if "journal.pominiecie" in s]


def arkusz(bid, ask, *, wiek_s):
    """Arkusz o zadanym WIEKU wobec `now()` - tak samo, jak liczy go `zloz_plan`.

    Staly znacznik czasu starzalby sie z kazda godzina od napisania testu.
    """
    teraz = dt.datetime.now(dt.timezone.utc)
    return OrderBook(event_time=teraz - dt.timedelta(seconds=wiek_s),
                     bids=[BookLevel(D(bid), D("100"))],
                     asks=[BookLevel(D(ask), D("100"))])


class Gielda:
    """Adapter-atrapa dla toru equity."""

    def __init__(self, *, ksiega=None, odniesienie=None):
        self._ksiega = ksiega
        self._odniesienie = odniesienie
        self.zlozone = []

    async def cena_odniesienia(self, symbol, kategoria=None):
        return self._odniesienie

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        return self._ksiega

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return None

    async def zloz_zlecenie(self, **kw):
        self.zlozone.append(kw)
        return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy="G1",
                               status="new")

    async def anuluj_zlecenie(self, **kw):
        return True


def plan_equity(*, rodzaj="wejscie", strona="buy"):
    trigger = dict(rodzaj=rodzaj, bar=BAR)
    if rodzaj == "wyjscie":
        trigger["position_id"] = 32
    return dict(id=77, decision_id=1077, instrument_id=41,
                side=strona, qty=D("10"), limit_price=D(LIMIT_Z_PLANU),
                symbol="AAPL", quote_ccy="USD", base_ccy="AAPL",
                atr_pct=ATR_AAPL, trigger=trigger)


def zloz(p, gielda, sesja):
    cur = Kursor([dict(id=32, qty=D("10"), avg_price=D("230"))])
    ok, msg = asyncio.run(zloz_plan(cur, gielda, p, AAPL, engine="equity",
                                    mode="demo", kategoria="stock",
                                    sesja=sesja))
    return cur, ok, msg


def gielda_po_zamknieciu():
    """Stan, ktory Alpaca oddaje po 16:00: arkusz ZAMROZONY, a ostatnia
    transakcja ODCZYTYWALNA i bez znacznika czasu."""
    return Gielda(ksiega=arkusz("232.44", "232.46",
                                wiek_s=WIEK_ARKUSZA_MAX_S + 60),
                  odniesienie=D(OSTATNIA_TRANSAKCJA))


# ============================================ 1. premisa: bramka sesji w petli

@pytest.mark.skipif(not TEST_DB_URL,
                    reason="ustaw TEST_DB_URL na jednorazową bazę z migracjami")
def test_poza_sesja_petla_nie_dotyka_gieldy():
    """PREMISA 1 OBALONA NA PRAWDZIWEJ BAZIE.

    Adapter wybucha przy KAŻDYM sięgnięciu po atrybut, więc test pada, gdyby
    `run_once` dotknął giełdy choć raz. Przechodzi, bo bramka sesji kończy cykl
    przed decyzjami — a to znaczy, że `brak_ceny_wysylki` poza sesją nie ma
    jak powstać.
    """
    import psycopg
    from psycopg.rows import dict_row

    from services.engine.loop import run_once

    class Wybuch:
        def __getattr__(self, nazwa):
            raise AssertionError(
                "poza sesją sięgnięto do giełdy po `" + nazwa + "` — bramka "
                "sesji w `run_once` przestała chronić egzekucję")

    # LICZYMY PRZYROST, NIE STAN. `journal.pominiecie` jest append-only
    # (wyzwalacz `journal.forbid_mutation`), wiec wyczyszczenie jej przed
    # przebiegiem nie jest mozliwe - i nie powinno byc.
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        with conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM journal.pominiecie")
            przed = c.fetchone()["n"]
        s = asyncio.run(run_once(conn, "equity", mode="demo",
                                 kind_kosztow="stock", limit=True,
                                 verbose=False, gielda=Wybuch(),
                                 wymagaj_modelu=True,
                                 sesja=StanSesji(False, "zegar giełdy")))
        assert s.get("poza_sesja") is True
        with conn.cursor() as c:
            c.execute("SELECT count(*) AS n FROM journal.pominiecie")
            assert c.fetchone()["n"] == przed, (
                "poza sesją powstał wiersz pominięcia — premisa 0060 mówi "
                "o `brak_ceny_wysylki`, ale ta ścieżka nie jest osiągalna")


# ==================== 2. premisa: prog wieku oslania tylko JEDNO zrodlo ceny

def test_cena_odniesienia_nie_ma_znacznika_czasu_wiec_wieku_nie_da_sie_sprawdzic():
    """Źródło zapasowe jest strukturalnie nieaudytowalne pod kątem wieku.

    Nie asercja o stylu — asercja o tym, że próg wieku z 0059 NIE MA JAK objąć
    drugiego źródła ceny. Gdyby ktoś dopisał tu znacznik czasu, ten test
    przypomni, że bramkę wieku wolno wtedy rozszerzyć.
    """
    import inspect

    from services.exchanges.alpaca import AlpacaAdapter
    from services.exchanges.bybit import BybitAdapter

    for adapter in (AlpacaAdapter, BybitAdapter):
        zwrot = str(inspect.signature(adapter.cena_odniesienia).return_annotation)
        assert "Decimal" in zwrot and "datetime" not in zwrot, (
            adapter.__name__ + ".cena_odniesienia oddaje " + zwrot
            + " — jeśli doszedł znacznik czasu, próg wieku arkusza powinien "
            "objąć także to źródło")


def test_stary_arkusz_sam_nie_wstrzymuje_wejscia_bo_jest_zrodlo_zapasowe():
    """PREMISA 2: przeterminowany arkusz NIE daje `brak_ceny_wysylki`.

    OPIS DZISIEJSZEGO ZACHOWANIA, nie postulat — i właśnie dlatego premisa
    "poza sesją odmawiamy każdego wejścia" jest fałszywa. `sesja=None` znaczy
    tu "wołający nie pytał o sesję", czyli stan sprzed tej zmiany.
    """
    cur, ok, msg = zloz(plan_equity(), gielda_po_zamknieciu(), sesja=None)
    assert "brak_ceny_wysylki" not in cur.powody_pominiec(), msg
    assert ok is True, (
        "bez wiedzy o sesji stary arkusz jest obchodzony przez "
        "`cena_odniesienia` i zlecenie IDZIE: " + msg)


# ============================== 3. KANARKI: sesja rozstrzyga o WYSYLCE wejscia

def test_wejscie_nie_idzie_na_gielde_gdy_rynek_jest_ZAMKNIETY():
    """KANAREK. Rynek zamknięty to stan NORMALNY, a nie brak ceny.

    Bez tej bramki zlecenie idzie po cenie zamknięcia i Alpaca kolejkuje je do
    następnego otwarcia — pozycja otwarta nazajutrz przez lukę nocną, bez
    decyzji, która by ją popierała.
    """
    g = gielda_po_zamknieciu()
    cur, ok, msg = zloz(plan_equity(), g,
                        sesja=StanSesji(False, "zegar giełdy"))
    assert ok is False, "wejście poszło na giełdę przy rynku ZAMKNIĘTYM"
    assert g.zlozone == [], "zlecenie trafiło na giełdę poza sesją"
    assert cur.powody_pominiec() == ["rynek_zamkniety"], (
        "poza sesją powód musi być WŁASNY, a nie `brak_ceny_wysylki` — "
        "inaczej czujka alarmuje co noc na stan normalny: " + msg)


def test_powod_poza_sesja_nie_brzmi_jak_awaria_gieldy():
    """Treść odmowy czyta człowiek o 03:00. Musi mówić, że tak ma być."""
    _, _, msg = zloz(plan_equity(), gielda_po_zamknieciu(),
                     sesja=StanSesji(False, "zegar giełdy"))
    assert "NIE JEST AWARIĄ" in msg.upper(), msg


def test_wejscie_nie_idzie_na_gielde_gdy_stan_sesji_jest_NIEZNANY():
    """KANAREK. Brak wiedzy wstrzymuje wejście — nie przepuszcza go.

    Zegar giełdy, który nie odpowiedział, nie daje domysłu "pewnie otwarte";
    ta sama reguła, którą `sesja.wolno_decydowac` stosuje w pętli.
    """
    g = gielda_po_zamknieciu()
    cur, ok, msg = zloz(plan_equity(), g,
                        sesja=StanSesji(None, "zegar giełdy nie odpowiedział",
                                        blad="ReadTimeout"))
    assert ok is False, "wejście poszło na giełdę przy NIEZNANYM stanie sesji"
    assert g.zlozone == [], "zlecenie trafiło na giełdę bez wiedzy o sesji"
    assert cur.powody_pominiec() == ["sesja_nieznana"], (
        "nieznany stan sesji to AWARIA odczytu i musi być odróżnialny od "
        "rynku zamkniętego, bo jedno wymaga reakcji, a drugie nie: " + msg)


# ================================================ 4. czego bramka NIE rusza

def test_wyjscie_wychodzi_takze_poza_sesja():
    """Wejścia wolno wstrzymać, wyjść NIE.

    Odmowa wyjścia zostawia pozycję otwartą wbrew decyzji — kosztuje pieniądze,
    a nie okazję. Poza sesją zlecenie `day` czeka do otwarcia i dla wyjścia
    jest to skutek pożądany, a nie uboczny.
    """
    g = gielda_po_zamknieciu()
    _, ok, msg = zloz(plan_equity(rodzaj="wyjscie", strona="sell"), g,
                      sesja=StanSesji(False, "zegar giełdy"))
    assert ok is True, "bramka sesji zatrzymała WYJŚCIE: " + msg
    assert len(g.zlozone) == 1


def test_wyjscie_wychodzi_takze_przy_nieznanym_stanie_sesji():
    """To samo dla trzeciego stanu: brak wiedzy nie więzi pozycji."""
    g = gielda_po_zamknieciu()
    _, ok, msg = zloz(plan_equity(rodzaj="wyjscie", strona="sell"), g,
                      sesja=StanSesji(None, "zegar giełdy nie odpowiedział"))
    assert ok is True, "nieznany stan sesji zatrzymał WYJŚCIE: " + msg
    assert len(g.zlozone) == 1


def test_krypto_nie_dostaje_luzniejszego_progu_ani_zadnego_zapytania():
    """Rynek ciągły nie ma prawa skorzystać z tej bramki.

    `is_24_7` rozstrzyga PRZED sięgnięciem po zegar, więc tor krypto nie płaci
    za bramkę ani jednym zapytaniem i nie dostaje podniesionego progu wieku.
    """
    class KursorVenue:
        def execute(self, sql, params=()):
            pass

        def fetchone(self):
            return dict(code="bybit", is_24_7=True)

    class BezZegara:
        def __getattr__(self, nazwa):
            raise AssertionError("rynek ciągły pytany o " + nazwa)

    s = asyncio.run(stan_sesji(KursorVenue(), "crypto", BezZegara()))
    assert s.otwarta is True and s.wolno_decydowac is True
    assert WIEK_ARKUSZA_MAX_S == 60, (
        "próg wieku arkusza został zmieniony — dla rynku ciągłego to ciche "
        "zezwolenie na starą cenę")

    g = Gielda(ksiega=arkusz("232.44", "232.46", wiek_s=0.2),
               odniesienie=D(OSTATNIA_TRANSAKCJA))
    _, ok, msg = zloz(plan_equity(), g, sesja=s)
    assert ok is True, "rynek ciągły dostał odmowę z bramki sesji: " + msg


# ======================================= 5. bramka jest WPIETA, a nie tylko jest


# ======================== 6. PUNKT INTEGRACJI: obiekt sesji naprawde dochodzi

# WADA, KTORA TA SEKCJA PILNUJE, I DLACZEGO POPRZEDNIE TESTY JEJ NIE LAPALY
#
# Bramka w `zloz_plan` brzmi `sesja is not None and not sesja.wolno_decydowac`,
# wiec `sesja=None` CICHO POMIJA JA CALA i wejscia wychodza - dokladnie stare,
# zepsute zachowanie. Testy wyzej pilnuja `zloz_plan` GDY dostanie obiekt sesji;
# ani jeden nie pilnowal, ze `run_once` ten obiekt w ogole podaje. Zmiana
# `sesja=sesja_wysylki` na `sesja=None` w `loop.run_once` przechodzila caly
# zestaw 2163 testow na zielono.
#
# Komentarz w kodzie mowi "petla decyzyjna przekazuje stan zawsze", ale to jest
# ZALOZENIE O WOLAJACYM, nie gwarancja. Ten sam ksztalt obalil premise migracji
# 0060: cos bylo prawda o jednym miejscu i nieprawda o zlozeniu.

@pytest.mark.skipif(not TEST_DB_URL,
                    reason="ustaw TEST_DB_URL na jednorazową bazę z migracjami")
@pytest.mark.parametrize("otwarta_przy_wysylce", [False, True, None])
def test_run_once_PRZEKAZUJE_obiekt_sesji_do_wysylki(otwarta_przy_wysylce,
                                                    monkeypatch):
    """`zloz_oczekujace` musi dostać obiekt sesji, a nie `None`.

    Test przebiega PRAWDZIWY `run_once` na prawdziwej bazie i podstawia atrapę
    pod samą wysyłkę, żeby zapisać, co do niej doszło. Sprawdza trzy rzeczy,
    bo każda z nich osobno daje się spełnić przypadkiem:

      * że obiekt sesji doszedł W OGÓLE (`sesja is not None`) — to łapie
        `sesja=None`, czyli ciche pominięcie bramki,
      * że opisuje stan Z CHWILI WYSYŁKI, a nie z początku cyklu — bramka
        widzi `otwarta=True`, a zegar przy wysyłce mówi co innego,
      * że `None` W ŚRODKU obiektu (stan NIEZNANY) jest odróżnialny od braku
        obiektu — to dwie różne rzeczy i tylko jedna z nich wyłącza bramkę.

    Parametr `True` nie jest ozdobą: bez niego test przechodziłby także dla
    kodu, który na sztywno podstawia sesję zamkniętą.
    """
    import psycopg
    from psycopg.rows import dict_row

    from services.engine import gielda_exec
    from services.engine.loop import run_once
    from services.exchanges.alpaca import Zegar

    class GieldaZZegarem:
        """Adapter equity. Zegar oddaje stan Z CHWILI WYSYLKI."""

        def __init__(self, otwarta):
            self._otwarta = otwarta
            if otwarta is None:
                # BRAK ZEGARA, a nie zegar oddajacy None: tak wyglada adapter
                # bez polaczenia. `stan_sesji` robi z tego `otwarta=None`.
                self.zegar = None

        async def zegar(self):
            t = dt.datetime.now(dt.timezone.utc)
            return Zegar(teraz=t, otwarta=self._otwarta,
                         nastepne_otwarcie=t, nastepne_zamkniecie=t)

        async def cena_odniesienia(self, symbol, kategoria=None):
            return D(OSTATNIA_TRANSAKCJA)

        async def fetch_order_book(self, symbol, depth=50, *,
                                   ksiega_egzekucji=True):
            return None

        async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
            return None

        async def zlecenia_otwarte(self, **kw):
            return []

        async def pozycje(self, **kw):
            return []

    doszlo = dict()

    async def atrapa_wysylki(cur, adapter, **kw):
        doszlo.update(kw)
        return dict(zlozone=0, odrzucone=0)

    monkeypatch.setattr(gielda_exec, "zloz_oczekujace", atrapa_wysylki)

    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        asyncio.run(run_once(
            conn, "equity", mode="demo", kind_kosztow="stock", limit=True,
            verbose=False, gielda=GieldaZZegarem(otwarta_przy_wysylce),
            wymagaj_modelu=False,
            # BRAMKA 1 WIDZI SESJE OTWARTA. Gdyby test podawal tu stan
            # zamkniety, cykl skonczylby sie na bramce i nigdy nie dotarl do
            # wysylki - czyli nie sprawdzilby niczego o przekazaniu.
            sesja=StanSesji(True, "zegar giełdy")))

    assert "sesja" in doszlo, (
        "`zloz_oczekujace` zostało zawołane BEZ argumentu `sesja` — bramka "
        "w `zloz_plan` stoi na `sesja is not None`, więc jej brak cicho "
        "przepuszcza wejścia")
    assert doszlo["sesja"] is not None, (
        "`zloz_oczekujace` dostało `sesja=None` — bramka sesji jest wtedy "
        "pomijana w całości i wejścia wychodzą poza sesją, czyli wraca "
        "zachowanie sprzed poprawki")
    assert doszlo["sesja"].otwarta is otwarta_przy_wysylce, (
        "stan sesji nie pochodzi z CHWILI WYSYŁKI: zegar giełdy mówi "
        + str(otwarta_przy_wysylce) + ", a do wysyłki doszło "
        + str(doszlo["sesja"].otwarta) + " — czytany jest stan z początku "
        "cyklu, czyli szczelina 15:59 wobec 16:00 jest wciąż otwarta")


# ------------------------------------------------------- otwarcie sesji (2026-10-18)

def test_otwarcie_sesji_US_wedlug_czasu_letniego():
    """09:30 Nowego Jorku: 13:30 UTC w czasie letnim, 14:30 w zimowym.
    2026: czas letni od 8 marca (druga niedziela) do 1 listopada."""
    from datetime import date

    from services.engine.sesja import otwarcie_sesji_us

    assert otwarcie_sesji_us(date(2026, 3, 6)).hour == 14
    assert otwarcie_sesji_us(date(2026, 3, 9)).hour == 13
    assert otwarcie_sesji_us(date(2026, 10, 16)).hour == 13
    assert otwarcie_sesji_us(date(2026, 10, 30)).hour == 13
    assert otwarcie_sesji_us(date(2026, 11, 2)).hour == 14
    assert otwarcie_sesji_us(date(2027, 3, 15)).hour == 13
    assert otwarcie_sesji_us(date(2026, 10, 16)).minute == 30


def test_swieca_przed_otwarciem_tylko_przy_OTWARTEJ_sesji_Alpaki():
    from datetime import datetime, timezone

    from services.engine.sesja import StanSesji

    teraz = datetime(2026, 12, 1, 14, 35, tzinfo=timezone.utc)   # zima: otwarcie 14:30
    koniec_1400 = datetime(2026, 12, 1, 14, 0, tzinfo=timezone.utc)
    koniec_1500 = datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)
    alpaca = StanSesji(True, "zegar giełdy", venue="alpaca")
    assert alpaca.swieca_przed_otwarciem(koniec_1400, teraz)
    assert not alpaca.swieca_przed_otwarciem(koniec_1500, teraz)
    assert not StanSesji(True, "rynek ciągły (is_24_7)", venue="bybit") \
        .swieca_przed_otwarciem(koniec_1400, teraz)
    assert not StanSesji(None, "zegar", venue="alpaca") \
        .swieca_przed_otwarciem(koniec_1400, teraz)
