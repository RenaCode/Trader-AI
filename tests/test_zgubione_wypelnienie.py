"""Kill-switch a zlecenie, którego stanu NIE ZNAMY.

DLACZEGO TEN PLIK ISTNIEJE OSOBNO

`anuluj_wejscia` ma nad sobą piętnaście linii komentarza opisujących dokładnie
jedną awarię: zapisanie zlecenia jako anulowanego, gdy ono naprawdę weszło,
GUBI WYPEŁNIENIE BEZPOWROTNIE — rozstrzyganie szuka planów `pending`, więc plan
`cancelled` nie zostanie zaksięgowany już nigdy. Aktywo leży na giełdzie,
dziennik go nie widzi, risk kernel nie liczy z niego ekspozycji, a po zwolnieniu
kill-switcha wciąż dodatni sygnał daje DRUGIE wejście.

Obsługa błędu stojąca tuż pod tym komentarzem przewracała jego rozumowanie:
nieudany ODCZYT stanu dawał `stan = None`, a `None` wpadał w gałąź „nic nie
weszło" — czyli w dokładnie ten zapis, przed którym komentarz ostrzega. Kod
przeczył własnemu komentarzowi, więc testy trzymają tu obie strony naraz:
wykonanie i zdanie, które je tłumaczy.

CZEGO TE TESTY NIE UDAJĄ. Atrapa nie wykonuje `WHERE`, więc nie sprawdzamy nią,
że filtr po trybie coś odsiewa — sprawdzamy, że instrukcja go NIESIE i że
wiąże pod niego tryb. Zachowanie bazy jest cudzą odpowiedzialnością; naszą jest
nie wysłać instrukcji, która trafia w cudzy wiersz.
"""
import asyncio

from services.engine.gielda_exec import anuluj_wejscia
from services.exchanges.base import ExchangeError, StanZlecenia
from tests.atrapa_bazy import Kursor
from decimal import Decimal as D

# Wiersz TAKI, JAKI ODDAJE ZAPYTANIE Z `anuluj_wejscia` — trzy kolumny, nie
# więcej. Atrapa nie wykonuje SELECT-a, więc podłożenie tu kolumny, której
# zapytanie nie pobiera, dałoby test przechodzący z fałszywego powodu: kod
# sięgałby po coś, czego na produkcji w wierszu nie ma.
ZLECENIE = {"client_order_id": "tai-p77", "symbol": "SOLUSDC",
            "planned_order_id": 77}


def _sprawdz_zgodnosc_wiersza(cur):
    """Każda kolumna podłożonego wiersza MUSI występować w zapytaniu."""
    (sql, _), = [(s, p) for s, p in cur.sql if "SELECT o.client_order_id" in s]
    for kolumna in ZLECENIE:
        assert kolumna in sql, (
            f"atrapa oddaje kolumnę {kolumna!r}, której SELECT nie pobiera — "
            f"test przeszedłby z błędnego powodu")


class Gielda:
    """Giełda, której ODCZYT STANU PADA na błędzie przejściowym.

    `anuluj_zlecenie` oddaje `False`, bo tak właśnie zachowuje się adapter Bybit
    przy kodzie 110001 — „zlecenia nie ma" (services/exchanges/bybit.py:863-866).
    Dla zlecenia JUŻ WYPEŁNIONEGO odpowiedź jest identyczna jak dla zlecenia,
    którego nigdy nie było, więc nie niesie żadnej informacji.
    """

    def __init__(self, *, blad=None, stan=None):
        self._blad = blad or ExchangeError("Bybit: transport: ReadTimeout",
                                           retryable=True)
        self._stan = stan
        self.odczyty, self.anulowania = 0, 0

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        self.odczyty += 1
        if self._stan is not None:
            return self._stan
        raise self._blad

    async def anuluj_zlecenie(self, **kw):
        self.anulowania += 1
        return False


class GieldaBezZlecenia(Gielda):
    """Giełda, która ODPOWIADA — i mówi, że takiego zlecenia nie ma.

    To jest inny stan niż nieudany odczyt i musi prowadzić do innej decyzji:
    tu wiemy, że nic nie weszło, więc anulowanie i zapis są na miejscu.
    """

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        self.odczyty += 1
        return None


def _anuluj(gielda=None, wiersze=(ZLECENIE,), **kw):
    cur = Kursor(list(wiersze))
    g = gielda or Gielda()
    zdjete, wypelnione = asyncio.run(anuluj_wejscia(
        cur, g, kategoria="spot", powod="kill-switch",
        engine="crypto", mode="demo", verbose=False, **kw))
    return cur, g, zdjete, wypelnione


# ------------------------------------------------ nieudany odczyt stanu

def test_nieudany_odczyt_NIE_oznacza_planu_jako_anulowanego():
    """To jest ten jeden zapis, który gubi wypełnienie bezpowrotnie.

    Plan przestaje być `pending`, a rozstrzyganie szuka wyłącznie planów
    `pending` — więc wypełnienie, które mogło wejść, nie zostanie zaksięgowane
    już NIGDY. Na giełdzie pozycja, w dzienniku nic.

    Szukamy ZAPISU, nie wzmianki o tabeli: zapytanie odczytujące dołącza
    `journal.planned_order` przez LEFT JOIN, więc dopasowanie po samej nazwie
    tabeli trafiałoby w SELECT i test przechodziłby zawsze.
    """
    cur, _, _, _ = _anuluj()
    _sprawdz_zgodnosc_wiersza(cur)

    assert not any("UPDATE journal.planned_order" in s for s, _ in cur.sql), (
        "plan oznaczony mimo NIEZNANEGO stanu zlecenia — rozstrzyganie już go "
        "nie dotknie")


def test_nieudany_odczyt_NIE_zamyka_wiersza_zlecenia():
    """`exec.order` musi zostać na `new`, bo to jedyna nitka do tego zlecenia.

    Zapytanie na górze `anuluj_wejscia` wybiera zlecenia po statusie
    ('new','partial','unknown'). Przestawienie go na `cancelled` wypisuje
    zlecenie z tej listy, więc następny cykl nawet nie spróbuje ponowić.
    """
    cur, _, _, _ = _anuluj()

    assert not any("UPDATE exec.order SET status=" in s for s, _ in cur.sql), (
        "status zlecenia zmieniony mimo nieznanego stanu")


def test_nieudany_odczyt_NIE_anuluje_na_slepo():
    """Anulowanie nie doniosłoby niczego, a przy wypełnieniu CZĘŚCIOWYM zdjęłoby
    resztę na podstawie zgadywania.

    Bybit na zlecenie już wypełnione oddaje 110001, które adapter zamienia na
    `return False` bez wyjątku — odpowiedź nie do odróżnienia od „nie było czego
    zdejmować". Z takiej odpowiedzi nie wynika nic, czego byśmy nie wiedzieli.
    """
    _, g, _, _ = _anuluj()

    assert g.odczyty == 1
    assert g.anulowania == 0, "anulowanie na podstawie stanu, którego nie znamy"


def test_nieudany_odczyt_ZOSTAWIA_slad_w_bazie():
    """Stan nieznany nie jest stanem normalnym i nie może być ciszą.

    Ślad idzie tym samym wzorcem, co przy nieudanym anulowaniu niżej w tej samej
    funkcji: `reject_reason` BEZ zmiany statusu. Sam komunikat na ekranie
    nikogo nie obudzi, a `raw` cyklu nikt nie ogląda.
    """
    cur, _, _, _ = _anuluj()

    powody = [p for s, p in cur.sql
              if "UPDATE exec.order SET reject_reason" in s]
    assert len(powody) == 1, "nieznany stan zlecenia nie zostawił żadnego śladu"
    assert any(isinstance(x, str) and "nie udalo sie odczytac" in x
               for x in powody[0])


def test_nieudany_odczyt_NIE_jest_liczony_jako_sukces():
    """Obie liczby idą do komunikatu kill-switcha. Zlecenie o nieznanym stanie
    w którejkolwiek z nich opisywałoby jako rozstrzygnięte coś, czego właśnie
    nie rozstrzygnęliśmy — a raport mówiłby wtedy „zdjęto 1 zleceń"."""
    _, _, zdjete, wypelnione = _anuluj()

    assert (zdjete, wypelnione) == (0, 0)


def test_ekran_PODAJE_MIANOWNIK_a_nie_sama_liczbe_zdjetych():
    """„Zdjęto 3" przy pięciu zleceniach czyta się jak komplet.

    `run_once` wypisuje komunikat kill-switcha z samego `zdjete` i nie ma skąd
    wiedzieć, ilu zleceń ta liczba nie obejmuje. Bez tej linii operator widzi
    „zdjęto 3" tam, gdzie zdjęto 3 z 5 — a dwa żywe zlecenia zostały w arkuszu
    giełdy i nadal mogą się wypełnić.
    """
    import io
    from contextlib import redirect_stdout

    wiersze = [dict(ZLECENIE, client_order_id=f"tai-p{i}") for i in range(5)]
    cur = Kursor(list(wiersze))
    # Trzy pierwsze zlecenia giełda zna (zdejmiemy je), dwa ostatnie padają
    # na odczycie — czyli dokładnie przypadek „3 z 5".
    class Mieszana(Gielda):
        async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
            self.odczyty += 1
            if id_wlasne in ("tai-p0", "tai-p1", "tai-p2"):
                return None                   # gielda nie zna — nic nie weszlo
            raise self._blad                  # odczyt padl — nie wiemy nic

    ekran = io.StringIO()
    with redirect_stdout(ekran):
        zdjete, wypelnione = asyncio.run(anuluj_wejscia(
            cur, Mieszana(), kategoria="spot", powod="kill-switch",
            engine="crypto", mode="demo", verbose=True))
    out = ekran.getvalue()

    assert (zdjete, wypelnione) == (3, 0)
    assert "2 z 5" in out, (
        f"komunikat nie podaje, ilu zleceń liczba zdjętych NIE obejmuje: {out!r}")
    assert "NIE MA ich w liczbie zdjętych" in out


def test_ekran_MILCZY_gdy_wszystkie_stany_sa_znane():
    """Linia o nieznanych stanach ma znaczyć awarię, a nie być tłem. Wypisywana
    zawsze — także z zerem — przestałaby cokolwiek sygnalizować."""
    import io
    from contextlib import redirect_stdout

    ekran = io.StringIO()
    with redirect_stdout(ekran):
        asyncio.run(anuluj_wejscia(
            Kursor([dict(ZLECENIE)]), GieldaBezZlecenia(), kategoria="spot",
            powod="kill-switch", engine="crypto", mode="demo", verbose=True))

    assert "NIE ROZSTRZYGN" not in ekran.getvalue()


# ------------------------------------------- ścieżki, które muszą zostać całe

def test_gielda_NIEZNAJACA_zlecenia_nadal_prowadzi_do_anulowania():
    """`stan is None` z SAMEJ GIEŁDY znaczy co innego niż nieudany odczyt:
    zlecenia nie ma w arkuszu, czyli nie weszło. Ta gałąź ma zostać nietknięta —
    inaczej poprawka zamieniłaby kill-switch w funkcję, która nie zdejmuje nic,
    a test „nie oznacza planu" przechodziłby z najgorszego możliwego powodu.
    """
    cur, g, zdjete, wypelnione = _anuluj(gielda=GieldaBezZlecenia())

    assert (zdjete, wypelnione) == (1, 0)
    assert g.anulowania == 1
    assert any("UPDATE journal.planned_order SET status='cancelled'" in s
               for s, _ in cur.sql)


def test_wypelnione_zlecenie_nadal_ZOSTAWIA_plan_do_zaksiegowania():
    """Druga gałąź, której poprawka nie może ruszyć: giełda POWIEDZIAŁA, że
    zlecenie weszło. Plan zostaje `pending`, żeby rozstrzyganie je zaksięgowało."""
    stan = StanZlecenia(id_wlasne="tai-p77", id_gieldy="G1", status="filled",
                        ilosc=D("10"), wypelniono=D("10"),
                        srednia_cena=D("99.5"), prowizja=D("0.31"))
    cur, _, zdjete, wypelnione = _anuluj(gielda=Gielda(stan=stan))

    assert (zdjete, wypelnione) == (0, 1)
    assert not any("UPDATE journal.planned_order" in s for s, _ in cur.sql)


# --------------------------------------------- klucz unikalny to (mode, id)

def _sprawdz_filtr_trybu(cur, fragment):
    """`UNIQUE (mode, client_order_id)` — sam identyfikator NIE jest kluczem.

    `tai-p77` z demo i `tai-p77` z live to dwa różne wiersze i oba pasują do
    `WHERE client_order_id=%s`. Dziś tryb jest jeden, więc bez skutku; po
    dołożeniu `live` kill-switch na demo dopisywałby powód i status zleceniom
    produkcyjnym. Ta sama asymetria, co przy planach bez `engine`/`mode`.
    """
    (sql, params), = [(s, p) for s, p in cur.sql if fragment in s]
    assert "mode::text=%s" in sql or "mode::text = %s" in sql, (
        f"{fragment} trafia we WSZYSTKIE tryby o tym client_order_id")
    assert "demo" in params, "filtr po trybie jest w tekście, ale nic pod niego nie wiąże"


def test_slad_po_nieznanym_stanie_filtruje_PO_TRYBIE():
    _sprawdz_filtr_trybu(_anuluj()[0], "UPDATE exec.order SET reject_reason")


def test_zamkniecie_zlecenia_filtruje_PO_TRYBIE():
    _sprawdz_filtr_trybu(_anuluj(gielda=GieldaBezZlecenia())[0],
                         "UPDATE exec.order SET status='cancelled'")
