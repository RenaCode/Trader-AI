"""Egzekucja na giełdzie — to giełda rozstrzyga, nie nasze założenie o świecy."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from services.engine.gielda_exec import id_zlecenia, rozstrzygnij_plan, zloz_plan
from services.engine.horizons import get as get_horizon
from services.engine.zlecenia import Filtry
from services.exchanges.base import (
    BookLevel, ExchangeError, OrderBook, StanZlecenia, ZlozoneZlecenie)

F = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
           min_wartosc=D("5"), krok_ceny=D("0.01"))
BEZ_REGUL = Filtry()

# ŚWIECA DECYZYJNA JAKO PRAWDZIWY ZNACZNIK CZASU, a nie "x". Odmowa wysłania
# wejścia zapisuje wiersz w `journal.pominiecie`, którego kluczem jest
# (instrument, ŚWIECA, powód) — czyli ta sama wielkość, po której
# `decided_for_bar` rozstrzyga, czy decyzja dla świecy zapadła. Atrapa z "x"
# przepuszczałaby kod, który tej daty w ogóle nie czyta.
BAR = "2026-09-25T08:00:00+00:00"

PLAN = {"id": 42, "decision_id": 7, "instrument_id": 3, "side": "buy",
        "qty": D("10"), "limit_price": D("100"), "symbol": "SOLUSDC",
        "quote_ccy": "USDC", "trigger": {"rodzaj": "wejscie", "bar": BAR}}


# Ograniczenia z db/migrations/0005_decisions_and_execution.sql i 0032.
# NIE JEST TO EMULATOR POSTGRESA — to dokładnie te dwa rodzaje ograniczeń, na
# których dziś przeszły do produkcji dwa błędy: zapis `status='rejected'` łamiący
# CHECK oraz drugi INSERT łamiący UNIQUE (mode, client_order_id).
STATUSY_PLANU = ("pending", "triggered", "cancelled", "expired", "rejected")

# `exec.order.status` nie ma ograniczenia w bazie (kolumna jest tekstowa), więc
# tu go nie udajemy. Wpisywanie ograniczeń, których baza nie ma, robi z atrapy
# drugie źródło prawdy, a to rozjeżdża się przy pierwszej migracji.


class Kursor:
    """Atrapa kursora: zapamiętuje SQL, oddaje z góry zadane wiersze.

    ATRAPA BEZ OGRANICZEŃ PRZEPUSZCZA INSTRUKCJE, KTÓRE BAZA ODRZUCA. Test
    dopasowujący sam tekst SQL-a przechodzi wtedy także dla zapytania
    niewykonalnego — i tak właśnie dwa błędy doszły dziś do produkcji, mimo
    pełnego zestawu testów:

      * `UPDATE journal.planned_order SET status='rejected'` — CHECK dopuszczał
        wtedy cztery inne stany, więc odmowa złożenia wywracała całą transakcję
        cyklu, a odmowa jest ścieżką NORMALNĄ, nie awaryjną,
      * drugi `INSERT INTO exec.order` z tym samym `client_order_id` — łamał
        UNIQUE (mode, client_order_id) przy pierwszym wypełnieniu.

    Sprawdzamy więc trzy rzeczy dające się sprawdzić bez bazy: zgodność liczby
    parametrów, dozwolone wartości `status` na `journal.planned_order`, oraz
    unikalność `client_order_id` przy wstawianiu do `exec.order`.
    """

    def __init__(self, wiersze=None, mode="demo"):
        self.sql = []
        self._wiersze = list(wiersze or [])
        self._mode = mode
        self._klucze = set()

    def _sprawdz_status_planu(self, jedno):
        if "journal.planned_order" not in jedno or "SET status=" not in jedno:
            return
        for dozwolony in STATUSY_PLANU:
            if f"SET status='{dozwolony}'" in jedno:
                return
        wartosc = jedno.split("SET status=")[1].split(",")[0].strip()
        raise AssertionError(
            f"journal.planned_order.status = {wartosc} łamie CHECK "
            f"(dozwolone: {', '.join(STATUSY_PLANU)}) — baza odrzuciłaby całą "
            f"transakcję cyklu")

    def _sprawdz_unikalnosc(self, jedno, params):
        if "INSERT INTO exec.order" not in jedno or not params:
            return
        # client_order_id rozpoznajemy po ksztalcie: to jedyny parametr tekstowy
        # zaczynajacy sie od prefiksu naszych identyfikatorow.
        for p in params:
            if isinstance(p, str) and p.startswith("tai-"):
                if (self._mode, p) in self._klucze:
                    raise AssertionError(
                        f"drugi INSERT INTO exec.order z client_order_id={p!r} "
                        f"łamie UNIQUE (mode, client_order_id) — pierwsze "
                        f"wypełnienie zabiłoby silnik, a pozycja zostałaby "
                        f"na giełdzie")
                self._klucze.add((self._mode, p))

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        oczekiwane = jedno.count("%s")
        podane = 0 if params is None else len(params)
        assert oczekiwane == podane, (
            f"zapytanie ma {oczekiwane} miejsc na parametry, a dostało {podane}: "
            f"{jedno}")
        self._sprawdz_status_planu(jedno)
        self._sprawdz_unikalnosc(jedno, params)
        self.sql.append((jedno, params))

    def fetchone(self):
        return self._wiersze.pop(0) if self._wiersze else None

    def fetchall(self):
        w, self._wiersze = self._wiersze, []
        return w

    def zawiera(self, fragment):
        return any(fragment in s for s, _ in self.sql)

    def zapytania(self, fragment):
        """Wszystkie (sql, params) pasujące do fragmentu — do sprawdzania WARTOŚCI."""
        return [(s, p) for s, p in self.sql if fragment in s]


def arkusz(bid="100", ask="100.01", *, wiek_s=0.0) -> OrderBook:
    """Arkusz księgi EGZEKUCJI z chwili wysyłki — jedno poziom po każdej stronie.

    `wiek_s` liczony wobec `now()`, bo `zloz_plan` tak właśnie mierzy wiek
    odczytu. Stały znacznik czasu w atrapie starzałby się z każdą minutą od
    napisania testu i test padałby „sam z siebie" — a przy progu 60 s stałoby
    się to tego samego dnia.
    """
    import datetime as _dt
    teraz = _dt.datetime.now(_dt.timezone.utc)
    return OrderBook(event_time=teraz - _dt.timedelta(seconds=wiek_s),
                     bids=[BookLevel(D(bid), D("10"))],
                     asks=[BookLevel(D(ask), D("10"))])


class Gielda:
    def __init__(self, *, zlozenie=None, stan=None, blad_zlozenia=None,
                 blad_anulowania=None, odniesienie=None, rejestr=(),
                 ksiega=None, blad_ksiegi=None):
        self._rejestr = rejestr
        self.pytano_o_wypelnienia = []
        self._odniesienie = odniesienie
        # ATRAPA MUSI MIEC `fetch_order_book`, BO PRODUKCJA JA WOLA. Atrapa bez
        # metody, ktora kod produkcyjny wywoluje, opisuje INNY adapter niz ten
        # w produkcji - a `AttributeError` w tescie wyglada jak blad testu,
        # nie jak brak w atrapie. Domyslnie arkusz stoi tam, gdzie limit
        # w `PLAN` (100), zeby podmiana ceny nie ruszala tych testow, ktore
        # o cene nie pytaja.
        self._ksiega = arkusz() if ksiega is None else ksiega
        self._blad_ksiegi = blad_ksiegi
        self.pytano_o_arkusz = []
        self._zlozenie = zlozenie
        self._stan = stan
        self._blad_zlozenia = blad_zlozenia
        self._blad_anulowania = blad_anulowania
        self.zlozone, self.anulowane = [], []

    async def zloz_zlecenie(self, **kw):
        if self._blad_zlozenia:
            raise self._blad_zlozenia
        self.zlozone.append(kw)
        return self._zlozenie or ZlozoneZlecenie(
            id_wlasne=kw["id_wlasne"], id_gieldy="G1", status="new")

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return self._stan

    async def wypelnienia(self, *, od_ms=None, symbol=None):
        """Rejestr wykonan (`/v5/execution/list`) — JEDYNE miejsce, w ktorym
        Bybit podaje `feeCurrency`. Domyslnie pusty, czyli waluta prowizji
        pozostaje nieznana i zapis spada na walute kwotowana: dokladnie
        zachowanie sprzed poprawki."""
        self.pytano_o_wypelnienia.append({"od_ms": od_ms, "symbol": symbol})
        return list(self._rejestr)

    async def cena_odniesienia(self, symbol, kategoria=None):
        """Cena z ksiegi PODMIOTU. None znaczy „nie da sie odczytac"."""
        return self._odniesienie

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        """Arkusz. `ksiega_egzekucji` ZAPAMIETUJEMY, bo to nie szczegol.

        Host publiczny i host egzekucji to dwie rozne ksiegi (zmierzony rozjazd
        zamkniec: mediana 5,9 pb, p90 28,4 pb). Odczyt z niewlasciwej odpowiada
        na inne pytanie niz zadane, a odpowiada POPRAWNIE wygladajaca liczba.
        """
        self.pytano_o_arkusz.append({"symbol": symbol, "depth": depth,
                                     "ksiega_egzekucji": ksiega_egzekucji})
        if self._blad_ksiegi:
            raise self._blad_ksiegi
        return self._ksiega

    async def anuluj_zlecenie(self, **kw):
        if self._blad_anulowania:
            raise self._blad_anulowania
        self.anulowane.append(kw)
        return True


def _zloz(plan=PLAN, f=F, gielda=None):
    cur, g = Kursor(), gielda or Gielda()
    ok, msg = asyncio.run(zloz_plan(cur, g, plan, f, engine="crypto",
                                    mode="demo", kategoria="spot"))
    return cur, g, ok, msg


def test_identyfikator_zlecenia_pochodzi_Z_PLANU_a_nie_z_czasu():
    """Identyfikator z czasu albo losowy znosi całą idempotencję: przy każdej
    próbie powstaje nowe zlecenie, a dwa zlecenia to podwójna pozycja."""
    assert id_zlecenia(42) == "tai-p42"
    assert id_zlecenia(42) == id_zlecenia(42)


def test_brak_regul_BLOKUJE_zlecenie_i_gielda_nie_jest_pytana():
    """Zlecenie bez reguł zostałoby odrzucone przez giełdę jako błąd parametru,
    a w nocnym logu wygląda to jak brak sygnału."""
    cur, g, ok, msg = _zloz(f=BEZ_REGUL)
    assert ok is False
    # „NIE JEST PYTANA" ZNACZY: NIC NIE SKLADAMY. Od 2026-09-23 odmowa pyta
    # gieldę o `stan_zlecenia` naszego identyfikatora — po to, żeby nie porzucić
    # zlecenia, które poprzedni cykl zdążył wysłać przed swoją śmiercią
    # (znalezisko 7). To jest ODCZYT; zlecenia nadal nie powstaje żadnego.
    assert g.zlozone == []
    assert "nie podała reguł" in msg
    assert cur.zawiera("status='rejected'")


def test_odmowa_zlozenia_jest_ZAPISANA_a_nie_pominieta():
    """Bez zapisu jedyną różnicą między „giełda odrzuciła" a „nikt nie
    próbował" jest cisza."""
    cur, _, _, msg = _zloz(f=BEZ_REGUL)
    (sql, params), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.order" in s]
    assert "'rejected'" in sql
    # POWOD MUSI BYC W WIERSZU, nie tylko w komunikacie na ekranie. Log poda
    # znika, wiersz zostaje - a bez powodu wiersz mowi tylko "nie poszlo".
    assert msg in params


def test_blad_odpowiedzi_NIE_oznacza_planu_jako_odrzuconego():
    """Zlecenie mogło wejść mimo braku odpowiedzi. Oznaczenie go teraz
    odrzuconym byłoby zapisaniem nieprawdy o stanie, którego nie znamy —
    a następny cykl zapyta giełdę po naszym identyfikatorze."""
    cur, _, ok, msg = _zloz(gielda=Gielda(blad_zlozenia=ExchangeError("timeout")))
    assert ok is False
    assert "odpowiedź nie dotarła" in msg

    # PLAN MA ZOSTAC NIETKNIETY, zeby nastepny cykl go rozstrzygnal.
    assert not any("journal.planned_order" in sql for sql, _ in cur.sql)

    # A WIERSZ ZLECENIA MA MOWIC "NIE WIEM", nie "odrzucone". Sprawdzamy sam
    # literal statusu w SQL-u, bo to on jest zapisywany - pierwsza wersja tego
    # testu szukala wzorca "status='rejected'" ze znakiem rownosci i przechodzila
    # takze wtedy, gdy wstawka zapisywala 'rejected' jako wartosc.
    (sql, _), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.order" in s]
    assert "'unknown'" in sql
    assert "'rejected'" not in sql


def test_zlecenie_odnalezione_u_gieldy_nie_jest_skladane_drugi_raz():
    g = Gielda(zlozenie=ZlozoneZlecenie(id_wlasne="tai-p42", id_gieldy="G9",
                                        status="new", juz_istnialo=True))
    _, _, ok, msg = _zloz(gielda=g)
    assert ok is True
    assert "idempotencja" in msg


# ---------------------------------------------------------- rozstrzyganie

def _stan(wypelniono, ilosc=D("10"), status="filled"):
    return StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status=status,
                        ilosc=ilosc, wypelniono=wypelniono,
                        srednia_cena=D("100"), prowizja=D("0.25"))


SUBMITTED = __import__("datetime").datetime(
    2026, 9, 7, 8, 0, tzinfo=__import__("datetime").timezone.utc)


def _wiersz_zlecenia(oid, qty, submitted_at=SUBMITTED):
    """Wiersz `exec.order` w KSZTALCIE PRAWDZIWEGO SELECT-a: id, qty,
    submitted_at. `qty` to ilosc WYSLANA (podstawa ulamka wypelnienia),
    a `submitted_at` zaweza rejestr wykonan przy odczycie waluty prowizji."""
    return {"id": oid, "qty": qty, "submitted_at": submitted_at}


def _rozstrzygnij(stan, plan=PLAN, gielda=None, wiersze=None):
    # Pierwszy wiersz to ZLECENIE ZLOZONE WCZESNIEJ: jego `qty` jest podstawa
    # ulamka wypelnienia, bo to ona poszla na gielde. Ilosc z planu jest sprzed
    # zaokraglenia do kroku i uzycie jej robi z kazdego pelnego wypelnienia
    # czesciowe.
    cur = Kursor(wiersze or [_wiersz_zlecenia(1, D("10")), {"id": 1}])
    g = gielda or Gielda(stan=stan)
    rodzaj, msg = asyncio.run(rozstrzygnij_plan(
        cur, g, plan, F, engine="crypto", mode="demo", kategoria="spot",
        teraz=__import__("datetime").datetime(2026, 9, 7,
                                              tzinfo=__import__("datetime").timezone.utc),
        kind="spot"))
    return cur, g, rodzaj, msg


def test_gielda_nie_zna_zlecenia_TO_NIE_JEST_wypelnienie():
    cur, _, rodzaj, msg = _rozstrzygnij(None)
    assert rodzaj == "brak"
    assert "nie zna" in msg
    assert cur.zawiera("status='expired'")


def test_wypelnienie_czesciowe_zapisuje_TYLE_ILE_WESZLO_i_anuluje_reszte():
    cur, g, rodzaj, _ = _rozstrzygnij(_stan(D("4"), status="partial"))
    assert rodzaj == "czesciowe"
    assert len(g.anulowane) == 1
    (_, params), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.position" in s]
    assert D("4") in params            # ilosc faktyczna, nie planowana


def test_wypelnienie_pelne_NIE_anuluje_niczego():
    _, g, rodzaj, _ = _rozstrzygnij(_stan(D("10")))
    assert rodzaj == "pelne"
    assert g.anulowane == []


def test_nieudane_anulowanie_NIE_gubi_wypelnienia():
    """Wypełnienie już jest nasze. Udawanie, że rozstrzygnięcia nie było,
    zgubiłoby pozycję, którą naprawdę mamy — a otwartą resztę i tak wyłapie
    uzgodnienie z giełdą."""
    g = Gielda(stan=_stan(D("4"), status="partial"),
               blad_anulowania=ExchangeError("nie poszlo"))
    cur, _, rodzaj, msg = _rozstrzygnij(None, gielda=g)
    assert rodzaj == "czesciowe"
    assert "anulowanie reszty nie powiodło się" in msg
    assert cur.zawiera("INSERT INTO exec.position")


def test_cena_i_prowizja_pochodza_Z_GIELDY_a_nie_z_limitu():
    """Trzy liczby przestają być naszym założeniem: ilość, cena i prowizja."""
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=D("10"), wypelniono=D("10"),
                        srednia_cena=D("99.5"), prowizja=D("0.31"))
    cur, _, _, _ = _rozstrzygnij(stan)
    (_, params), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.fill" in s]
    assert D("99.5") in params
    assert D("0.31") in params


# ------------------------------------------- kill-switch a zlecenia na giełdzie

WYJSCIE = dict(PLAN, id=43, side="sell",
               trigger={"rodzaj": "wyjscie", "bar": "x", "position_id": 1})


def test_killswitch_zdejmuje_WEJSCIA_ale_zostawia_WYJSCIA():
    """Kill-switch ma wyprowadzać z ryzyka, a nie uwięzić w nim.

    Zdjęcie zlecenia sprzedaży zostawiłoby otwartą pozycję bez czegokolwiek,
    co ją zamknie — bezpiecznik zwiększyłby ekspozycję zamiast ją ściąć.
    """
    from services.engine.gielda_exec import anuluj_wejscia

    cur = Kursor()
    asyncio.run(anuluj_wejscia(cur, Gielda(), kategoria="spot", powod="test",
                               engine="crypto", mode="demo"))
    (sql, _), = [(s, p) for s, p in cur.sql if "SELECT o.client_order_id" in s]
    assert "'rodzaj', 'wejscie') <> 'wyjscie'" in sql


def test_wysylka_przy_killswitchu_obejmuje_TYLKO_wyjscia():
    """Plany wyjścia powstają także przy wstrzymanych wejściach. Niewysłanie
    ich uwięziłoby pozycje dokładnie wtedy, gdy ryzyko jest za duże."""
    from services.engine.gielda_exec import zloz_oczekujace

    cur = Kursor()
    asyncio.run(zloz_oczekujace(cur, Gielda(), engine="crypto", mode="demo",
                                kategoria="spot", h=get_horizon("4h"),
                                verbose=False, tylko_wyjscia=True))
    (sql, params), = [(s, p) for s, p in cur.sql if "journal.planned_order" in s]
    assert "p.trigger->>'rodzaj' = 'wyjscie'" in sql
    assert params == ("atr_pct__4h", "crypto", "demo", True)


def test_wysylka_bez_killswitcha_obejmuje_WSZYSTKIE_plany():
    from services.engine.gielda_exec import zloz_oczekujace

    cur = Kursor()
    asyncio.run(zloz_oczekujace(cur, Gielda(), engine="crypto", mode="demo",
                                kategoria="spot", h=get_horizon("4h"),
                                verbose=False))
    (_, params), = [(s, p) for s, p in cur.sql if "journal.planned_order" in s]
    assert params == ("atr_pct__4h", "crypto", "demo", False)


def test_nieudane_anulowanie_zostawia_SLAD_a_nie_cisze():
    """„Kill-switch zadziałał" musi znaczyć to, co znaczy. Zlecenie, którego
    nie udało się zdjąć, dalej leży w arkuszu."""
    from services.engine.gielda_exec import anuluj_wejscia

    cur = Kursor([{"client_order_id": "tai-p1", "symbol": "SOLUSDC",
                   "planned_order_id": 1}])
    g = Gielda(blad_anulowania=ExchangeError("nie poszlo"))
    zdjete, _ = asyncio.run(anuluj_wejscia(cur, g, kategoria="spot", powod="test",
                                           engine="crypto", mode="demo"))
    assert zdjete == 0
    assert any("anulowanie po kill-switchu nie powiodlo sie" in str(p)
               for _, p in cur.sql if p)


# ------------------------------------------------------- próba ścieżki egzekucji

def test_proba_egzekucji_ANULUJE_takze_po_bledzie():
    """Próba, która przy błędzie zostawia zlecenie w arkuszu, jest gorsza niż
    jej brak — zwłaszcza że jej celem jest sprawdzenie, czy anulowanie działa."""
    from services.engine.proba_egzekucji import Niezaliczona, przebieg
    from services.engine.zlecenia import Filtry

    f = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
               min_wartosc=D("5"), krok_ceny=D("0.01"))

    class GieldaBezOdczytu(Gielda):
        async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
            return None                       # gielda nie zna wlasnego id

    g = GieldaBezOdczytu()
    with pytest.raises(Niezaliczona):
        asyncio.run(przebieg(g, symbol="SOLUSDC", f=f,
                             cena_rynkowa=D("200"), znacznik="t"))
    assert len(g.anulowane) == 1              # mimo bledu


def test_proba_egzekucji_ODRZUCA_wypelnienie():
    """Wypełnienie utworzyłoby pozycję, której żadna decyzja nie popiera —
    zaśmieciłoby dziennik i uzgodnienie z giełdą."""
    from services.engine.proba_egzekucji import Niezaliczona, przebieg
    from services.engine.zlecenia import Filtry

    f = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
               min_wartosc=D("5"), krok_ceny=D("0.01"))
    g = Gielda(stan=StanZlecenia(id_wlasne="x", id_gieldy="G1", status="filled",
                                 ilosc=D("0.12"), wypelniono=D("0.12"),
                                 srednia_cena=D("50"), prowizja=D("0")))
    with pytest.raises(Niezaliczona) as exc:
        asyncio.run(przebieg(g, symbol="SOLUSDC", f=f,
                             cena_rynkowa=D("200"), znacznik="t"))
    assert "WYPEŁNIŁO SIĘ" in str(exc.value)
    assert len(g.anulowane) == 1


def test_proba_egzekucji_WYKRYWA_brak_idempotencji():
    """Drugie złożenie tego samego identyfikatora ma odnaleźć pierwsze.
    Utworzenie drugiego zlecenia to mechanizm powstawania podwójnej pozycji —
    i jedyna rzecz, której atrapa nie potwierdzi za prawdziwą giełdę."""
    from services.engine.proba_egzekucji import Niezaliczona, przebieg
    from services.engine.zlecenia import Filtry

    f = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
               min_wartosc=D("5"), krok_ceny=D("0.01"))
    g = Gielda(stan=StanZlecenia(id_wlasne="x", id_gieldy="G1", status="new",
                                 ilosc=D("0.12"), wypelniono=D("0"),
                                 srednia_cena=None, prowizja=D("0")))
    # zloz_zlecenie w atrapie zawsze zwraca juz_istnialo=False
    with pytest.raises(Niezaliczona) as exc:
        asyncio.run(przebieg(g, symbol="SOLUSDC", f=f,
                             cena_rynkowa=D("200"), znacznik="t"))
    assert "UTWORZYŁO DRUGIE" in str(exc.value)


def test_proba_egzekucji_stawia_limit_POZA_ZASIEGIEM():
    """Limit blisko rynku wypełniłby się i zrobił z próby transakcję."""
    from services.engine.proba_egzekucji import UDZIAL_CENY

    assert UDZIAL_CENY < D("0.5")


def test_jeden_plan_daje_JEDEN_wiersz_zlecenia():
    """Wiersz zlecenia powstaje przy ZŁOŻENIU, a rozstrzygnięcie przychodzi
    cykl później.

    Bez przekazania istniejącego identyfikatora `zapisz_wejscie` zakładałby
    drugi wiersz i jeden plan miałby w `exec.order` dwa zlecenia. Nie jest to
    szczegół księgowy: tabela przestaje znaczyć „jedno zlecenie", a czujka
    zdrowia egzekucji widzi dwa razy więcej przyjęć, niż było — czyli myli się
    dokładnie w tę stronę, w którą nie wolno.
    """
    cur = Kursor([_wiersz_zlecenia(501, D("10")), {"id": 1}])
    g = Gielda(stan=_stan(D("10")))
    asyncio.run(rozstrzygnij_plan(cur, g, PLAN, F, engine="crypto", mode="demo",
                                  kategoria="spot",
                                  teraz=__import__("datetime").datetime(
                                      2026, 9, 7,
                                      tzinfo=__import__("datetime").timezone.utc),
                                  kind="spot"))
    wstawienia = [s for s, _ in cur.sql if "INSERT INTO exec.order" in s]
    assert wstawienia == []                     # ani jednego nowego wiersza

    # ...a istniejacy zostal oznaczony jako wypelniony, z ilościa z gieldy.
    aktualizacje = [(s, p) for s, p in cur.sql
                    if "UPDATE exec.order SET status='filled'" in s]
    assert len(aktualizacje) == 1
    assert 501 in aktualizacje[0][1]


def test_rachunek_papierowy_NADAL_zaklada_wlasny_wiersz():
    """Symulacja nie składa wcześniej żadnego zlecenia, więc tam wiersz musi
    powstać przy zapisie wejścia — inaczej transakcja papierowa nie miałaby
    zlecenia w ogóle."""
    from services.engine.loop import zapisz_wejscie

    cur = Kursor([{"id": 77}])
    zapisz_wejscie(cur, "crypto", "demo", 3, 7, "buy", D("1"), D("100"), D("0.1"),
                   __import__("datetime").datetime(
                       2026, 9, 7, tzinfo=__import__("datetime").timezone.utc),
                   "USDC", {"execution": "internal_paper", "order": "limit"})
    assert any("INSERT INTO exec.order" in s for s, _ in cur.sql)


def test_pelne_wypelnienie_NIE_jest_zapisywane_jako_czesciowe():
    """Plan trzyma ilość SPRZED zaokrąglenia do kroku giełdy; na giełdę poszła
    ilość zaokrąglona w dół. Giełda nigdy nie odda więcej, niż zamówiono.

    Liczenie ułamka od ilości z planu sprawiało, że warunek „wypełniono ≥
    planowano" nie był spełniony NIGDY — każde pełne wypełnienie zapisywało się
    jako częściowe. Skutki: zbędne anulowanie przy każdym wejściu, wyjście
    z całej pozycji zostawiające pyłek (bo wiersz nie był zamykany) i
    systematycznie fałszywy `ulamek_wypelnienia` — jedyna deklarowana miara
    rozjazdu backtestu z produkcją.
    """
    plan = dict(PLAN, qty=D("10.567890"))       # przed zaokrągleniem
    wyslano = D("10.56")                        # po zaokrągleniu do kroku 0.01
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=wyslano, wypelniono=wyslano,
                        srednia_cena=D("100"), prowizja=D("0.1"))
    cur, g, rodzaj, _ = _rozstrzygnij(
        stan, plan=plan, wiersze=[_wiersz_zlecenia(1, wyslano), {"id": 1}])
    assert rodzaj == "pelne"
    assert g.anulowane == []                    # nie ma czego anulować


def test_killswitch_NIE_anuluje_zlecenia_ktore_sie_juz_wypelnilo():
    """Nasz status jest stary: wiersz siedzi na `new` przez całe dwie świece,
    bo nikt go nie odświeża między złożeniem a rozstrzygnięciem.

    Zapisanie takiego zlecenia jako anulowanego GUBI WYPEŁNIENIE BEZPOWROTNIE —
    rozstrzyganie szuka planów o statusie `pending`, więc już nigdy go nie
    zaksięguje. Trzymalibyśmy aktywo, którego dziennik nie widzi, a po
    zwolnieniu kill-switcha sygnał dałby DRUGIE wejście: na giełdzie dwie
    pozycje, w dzienniku jedna.
    """
    from services.engine.gielda_exec import anuluj_wejscia

    cur = Kursor([{"client_order_id": "tai-p1", "symbol": "SOLUSDC",
                   "planned_order_id": 1}])
    g = Gielda(stan=_stan(D("10"), status="filled"))
    zdjete, wypelnione = asyncio.run(
        anuluj_wejscia(cur, g, kategoria="spot", powod="test",
                       engine="crypto", mode="demo"))

    assert (zdjete, wypelnione) == (0, 1)
    # PLAN NIETKNIETY — inaczej rozstrzygniecie juz go nie znajdzie.
    #
    # Szukamy ZAPISU, nie wzmianki: sam SELECT dolacza journal.planned_order
    # przez LEFT JOIN, wiec dopasowanie po nazwie tabeli trafialoby w zapytanie
    # odczytujace i test przechodzilby zawsze.
    assert not any("UPDATE journal.planned_order" in s for s, _ in cur.sql)
    assert not any("UPDATE exec.order" in s for s, _ in cur.sql)


def test_killswitch_filtruje_po_silniku_i_trybie():
    """Dziś jest jeden silnik i jeden tryb, więc bez skutku. Po dołożeniu `live`
    kill-switch na demo wołałby anulowanie zleceń produkcyjnych adapterem demo."""
    from services.engine.gielda_exec import anuluj_wejscia

    cur = Kursor()
    asyncio.run(anuluj_wejscia(cur, Gielda(), kategoria="spot", powod="t",
                               engine="crypto", mode="demo"))
    (sql, params), = [(s, p) for s, p in cur.sql if "SELECT o.client_order_id" in s]
    assert "o.engine::text = %s AND o.mode::text = %s" in sql
    assert params == ("crypto", "demo")


# ------------------------------- zlecenia porzucone i wyjścia bez pozycji

def test_nieudane_anulowanie_ZOSTAWIA_plan_do_ponowienia():
    """Nic nie weszło, ale zlecenie NADAL LEŻY W ARKUSZU i nadal może się
    wypełnić po cenie, której żadna decyzja już nie popiera.

    Wygaszenie planu zrywałoby jedyną nitkę prowadzącą do tego zlecenia:
    rozstrzyganie szuka planów `pending`, więc plan `expired` nie zostałby już
    nigdy dotknięty, a jedyne inne miejsce ponawiające anulowanie rusza dopiero
    po kill-switchu.

    Skutek jest KUMULATYWNY: następna świeca widzi brak pozycji, sygnał wciąż
    trzyma, powstaje nowy plan i nowe zlecenie — i tak co świecę. Skok ceny
    wypełnia wszystkie naraz.
    """
    g = Gielda(stan=_stan(D("0"), status="new"),
               blad_anulowania=ExchangeError("timeout"))
    cur, _, rodzaj, powod = _rozstrzygnij(None, gielda=g)

    assert rodzaj == "nie_zdjete"
    assert "ZOSTAJE W ARKUSZU" in powod
    # PLAN NIE MOZE WYGASNAC — inaczej nikt do tego zlecenia nie wroci.
    assert not any("status='expired'" in s for s, _ in cur.sql)


def test_brak_wypelnienia_i_udane_anulowanie_ZAMYKA_plan():
    """Druga granica tej samej reguły: gdy zlecenie faktycznie zdjęto, plan MA
    wygasnąć. Inaczej silnik wracałby do niego w nieskończoność."""
    g = Gielda(stan=_stan(D("0"), status="new"))
    cur, _, rodzaj, _ = _rozstrzygnij(None, gielda=g)

    assert rodzaj == "brak"
    assert any("status='expired'" in s for s, _ in cur.sql)


def test_wyjscie_bez_pozycji_NIE_jest_cicho_pomijane():
    """Sprzedaż wykonała się na giełdzie, a w bazie nie ma pozycji, którą miała
    zamknąć. Bez własnej nazwy plan dostawał `triggered`, czyli „rozstrzygnięty
    pomyślnie" — ani linijki na ekranie, ani wiersza w bazie."""
    wyjscie = dict(PLAN, id=44, side="sell",
                   trigger={"rodzaj": "wyjscie", "bar": "x", "position_id": 1})
    # Drugi wiersz to odczyt pozycji: None znaczy „nie ma czego zamykać".
    cur = Kursor([_wiersz_zlecenia(1, D("10")), None])
    g = Gielda(stan=_stan(D("10")))
    rodzaj, powod = asyncio.run(rozstrzygnij_plan(
        cur, g, wyjscie, F, engine="crypto", mode="demo", kategoria="spot",
        teraz=__import__("datetime").datetime(
            2026, 9, 8, tzinfo=__import__("datetime").timezone.utc),
        kind="spot"))

    assert rodzaj == "wyjscie_bez_pozycji"
    assert not any("INSERT INTO exec.position" in s for s, _ in cur.sql)


def test_identyfikator_proby_jest_JEDNORAZOWY():
    """Stały identyfikator sprawiał, że od drugiego uruchomienia próba nie
    wysyłała na giełdę NICZEGO: `zloz_zlecenie` odnajdywało w historii zlecenie
    z poprzedniego przebiegu i meldowało `juz_istnialo`.

    Potwierdzone na produkcji — dwa kolejne przebiegi zwróciły ten sam
    identyfikator giełdy i zero zapytań POST, a mimo to raportowały przejście
    całego obiegu. Odwrotnie niż w egzekucji, gdzie stałość identyfikatora jest
    właśnie tym, co chroni przed podwójną pozycją.
    """
    from services.engine.proba_egzekucji import id_proby

    assert id_proby("crypto") != id_proby("crypto")
    assert len(id_proby("crypto")) <= 36        # limit orderLinkId u Bybita


# ------------------------------------------------- pasmo ceny przy składaniu

def test_cena_oderwana_od_ksiegi_docelowej_BLOKUJE_wejscie():
    """Cena limitu powstaje ze świec podmiotu GLOBALNEGO, a zlecenie ląduje
    w księdze UNIJNEJ. Sprawdzanie limitu względem tej samej księgi, z której
    powstał, byłoby porównaniem liczby z samą sobą — przeszłoby zawsze, także
    dla zepsutego ticka.

    Rozjazd między księgami jest zmierzony i duży: w 16,8% świec przekracza
    całą rundę kosztową. Przy księdze podmiotu unijnego kilkukrotnie płytszej
    niż globalna przejechanie przez nią jest realną stratą, a w dzienniku
    wygląda jak zwykła transakcja.
    """
    plan = dict(PLAN, atr_pct=1.0)
    # Ksiega docelowa mowi 100, a limit stoi na 200 - dwukrotnie obok.
    g = Gielda(odniesienie=D("100"))
    cur, gielda, ok, msg = _zloz(plan=dict(plan, limit_price=D("200")), gielda=g)

    assert ok is False
    assert gielda.zlozone == []              # nic nie poszlo na gielde
    assert "odbiega od księgi docelowej" in msg


def test_cena_w_pasmie_przechodzi():
    """Druga granica: zlecenie zgodne z księgą docelową ma iść normalnie."""
    plan = dict(PLAN, atr_pct=1.0)
    g = Gielda(odniesienie=D("100"))
    _, gielda, ok, _ = _zloz(plan=dict(plan, limit_price=D("100")), gielda=g)

    assert ok is True
    assert len(gielda.zlozone) == 1


def test_padniecie_JEDNEGO_endpointu_nie_zatrzymuje_handlu():
    """Awaria jednego endpointu, która wstrzymuje handel, jest gorszym
    lekarstwem niż choroba — dlatego cena z chwili wysyłki ma DWA źródła.

    Tu pada odczyt ostatniej ceny (`cena_odniesienia`), czyli wyłącza się samo
    pasmo. Arkusz księgi egzekucji żyje, więc cena limitu nadal powstaje
    z chwili wysyłki i zlecenie idzie normalnie. Test sprawdza jedno i drugie:
    że poszło ORAZ że poszło po cenie z arkusza, a nie z planu.
    """
    # ATR 2%: od 2026-10-03 prog dryfu to 0,5 x ATR (`zlecenia.KROTNOSC_ZWLOKI`),
    # a 100 -> 99,49 to 0,51% - przy ATR 1% test mierzylby prog, nie zrodla ceny.
    plan = dict(PLAN, atr_pct=2.0)
    g = Gielda(odniesienie=None, ksiega=arkusz("99.50", "99.52"))
    _, gielda, ok, _ = _zloz(plan=plan, gielda=g)

    assert ok is True
    assert len(gielda.zlozone) == 1
    # Bid 99,50 minus krok: zlecenie lezy po stronie pasywnej, a nie na 100
    # z planu (co przy asku 99,52 przecinaloby spread i PostOnly by je odbil).
    assert gielda.zlozone[0]["cena_limitu"] == D("99.49")


def test_padniecie_OBU_zrodel_ceny_NIE_WYSYLA_wejscia():
    """Kanarek w drugą stronę. Bez ANI JEDNEGO odczytu z chwili wysyłki jedyną
    ceną, jaką mamy, jest zamknięcie świecy sprzed 71–82 minut — a `PostOnly`
    odrzuca cenę przecinającą spread. Wysłanie jej jest zakładem o kierunek
    rynku przez tę godzinę, nie wykonaniem decyzji.

    Wyjście tego progu NIE dostaje (osobny test niżej): tam odmowa zostawia
    otwartą pozycję wbrew decyzji, czyli kosztuje pieniądze, a nie okazję.
    """
    plan = dict(PLAN, atr_pct=1.0, trigger={"rodzaj": "wejscie", "bar": BAR})
    g = Gielda(odniesienie=None, blad_ksiegi=ExchangeError("timeout arkusza"))
    cur, gielda, ok, msg = _zloz(plan=plan, gielda=g)

    assert ok is False
    assert gielda.zlozone == []
    assert "brak ceny z chwili wysyłki" in msg
    powody = [p[4] for _, p in cur.zapytania("INSERT INTO journal.pominiecie")]
    assert powody == ["brak_ceny_wysylki"], (
        "odmowa bez wiersza w journal.pominiecie nie odpowiada na pytanie "
        "o to, czemu dla tej świecy nie weszliśmy - a to jest pytanie, "
        "które naprawdę się zadaje: %s" % (powody,))


def test_padniecie_OBU_zrodel_ceny_NIE_ZATRZYMUJE_wyjscia():
    """Symetria byłaby tu błędem. Wyjście, którego nie wysłaliśmy, zostawia
    pozycję otwartą wbrew decyzji, a risk kernel liczy z niej ekspozycję —
    więc brak świeżej ceny NIE MOŻE go wstrzymać. Cena zostaje planowa,
    dokładnie tak jak przed poprawką."""
    wyjscie = dict(PLAN, id=46, side="sell", atr_pct=1.0,
                   trigger={"rodzaj": "wyjscie", "bar": BAR, "position_id": 1})
    g = Gielda(odniesienie=None, blad_ksiegi=ExchangeError("timeout arkusza"))
    cur = Kursor([{"id": 1, "qty": D("10"), "avg_price": D("100")}])
    ok, _ = asyncio.run(zloz_plan(cur, g, wyjscie, F, engine="crypto",
                                  mode="demo", kategoria="spot"))

    assert ok is True
    assert g.zlozone[0]["cena_limitu"] == D("100")
    assert not cur.zapytania("INSERT INTO journal.pominiecie")


def test_WYJSCIE_poza_pasmem_jest_PRZYCIAGANE_a_nie_odrzucane():
    """Odmowa wyjścia zostawia otwartą pozycję wbrew decyzji — to zdarzenie
    krytyczne, a nie oszczędność. Cena jest więc przyciągana do pasma zamiast
    blokować zlecenie."""
    wyjscie = dict(PLAN, id=45, side="sell", atr_pct=1.0,
                   limit_price=D("200"),
                   trigger={"rodzaj": "wyjscie", "bar": "x", "position_id": 1})
    g = Gielda(odniesienie=D("100"))
    _, gielda, ok, _ = _zloz(plan=wyjscie, gielda=g)

    assert ok is True
    assert len(gielda.zlozone) == 1
    # Cena zeszla z 200 do gornej krawedzi pasma wokol 100.
    assert gielda.zlozone[0]["cena_limitu"] < D("200")


def test_PELEN_OBIEG_planu_nie_lamie_unikalnosci_zlecenia():
    """Złożenie i rozstrzygnięcie na JEDNYM kursorze — tak, jak dzieje się to
    w produkcji: wiersz zlecenia powstaje w jednym cyklu, wypełnienie księguje
    się w następnym.

    Każde z osobna przechodziło; dopiero razem łamały `UNIQUE (mode,
    client_order_id)`. Pierwsze prawdziwe wypełnienie zabiłoby silnik, a kupione
    aktywo zostałoby na giełdzie bez śladu w dzienniku. Testy tego nie widziały,
    bo żaden nie wykonywał obu kroków na wspólnym stanie.
    """
    import datetime as dt

    teraz = dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc)
    plan = dict(PLAN, atr_pct=1.0)
    g = Gielda(odniesienie=D("100"), stan=_stan(D("10")))

    # Kursor jeden na oba kroki — to on pilnuje unikalności.
    cur = Kursor([_wiersz_zlecenia(1, D("10")), {"id": 1}])

    ok, _ = asyncio.run(zloz_plan(cur, g, plan, F, engine="crypto",
                                  mode="demo", kategoria="spot"))
    assert ok is True

    rodzaj, _ = asyncio.run(rozstrzygnij_plan(
        cur, g, plan, F, engine="crypto", mode="demo", kategoria="spot",
        teraz=teraz, kind="spot"))
    assert rodzaj == "pelne"

    wstawienia = [s for s, _ in cur.sql if "INSERT INTO exec.order" in s]
    assert len(wstawienia) == 1, (
        "jeden plan musi zostawić dokładnie jeden wiersz zlecenia — drugi łamie "
        "UNIQUE (mode, client_order_id)")


def test_zapytanie_o_ATR_uzywa_ISTNIEJACEJ_kolumny():
    """Nazwa kolumny zgadnięta zamiast sprawdzonej zatrzymała silnik na
    produkcji: `feat.feature_value` ma `feature_def_id`, nie `feature_id`.

    Pięć przebiegów pod rząd zakończonych błędem, proces zakończony, trzy
    restarty poda. Zabezpieczenie zadziałało — awaria była widoczna zamiast
    cichej — ale przez kilkanaście minut silnik nie handlował.

    Atrapa kursora nie mogła tego złapać, bo nie zna schematu bazy; ten test
    porównuje nazwę wprost z migracją, która tę tabelę tworzy.
    """
    from pathlib import Path

    zrodlo = Path("services/engine/gielda_exec.py").read_text(encoding="utf-8")
    assert "fv.feature_def_id" in zrodlo
    assert "fv.feature_id" not in zrodlo

    # Kolumna musi istniec w migracji tworzacej tabele - inaczej test pilnuje
    # wlasnego zalozenia zamiast schematu.
    migracje = " ".join(
        p.read_text(encoding="utf-8")
        for p in sorted(Path("db/migrations").glob("*.sql")))
    assert "feature_def_id" in migracje


# ------------------------------------ sprzątanie zleceń, które przeżyły plan

def _sprzataj(wiersze, gielda):
    from services.engine.gielda_exec import posprzataj_zlecenia

    cur = Kursor(wiersze)
    st = asyncio.run(posprzataj_zlecenia(cur, gielda, engine="crypto",
                                         mode="demo", kategoria="spot",
                                         verbose=False))
    return cur, st


def test_sprzatanie_ZDEJMUJE_zlecenie_ktore_przezylo_swoj_plan():
    """Reszta po wypełnieniu częściowym, której nie udało się anulować, leży
    w arkuszu i wypełni się po cenie, której żadna decyzja już nie popiera.

    Rozstrzyganie do niej nie wróci — szuka planów `pending`. Jedynym miejscem
    ponawiającym anulowanie było `anuluj_wejscia`, które rusza WYŁĄCZNIE po
    kill-switchu: zlecenie porzucone przy zwykłym błędzie sieci czekało więc
    na obsunięcie kapitału, żeby ktoś je zdjął.
    """
    g = Gielda(stan=_stan(D("4"), status="partial"))
    cur, st = _sprzataj([{"id": 7, "client_order_id": "tai-p42",
                          "status": "partial", "symbol": "SOLUSDC"}], g)
    assert st["zdjete"] == 1
    assert len(g.anulowane) == 1
    assert any("status='cancelled'" in s for s, _ in cur.sql)


def test_sprzatanie_ZAMYKA_wiersz_ktorego_gielda_NIE_ZNA():
    """Wiersz `unknown` po nieudanej odpowiedzi zostawał otwarty na zawsze,
    a czujka zdrowia egzekucji zgłaszała go CODZIENNIE jako kandydata na
    pozycję-widmo. Lista rosła bez końca i uczyła odbiorcę ignorować alarm."""
    g = Gielda(stan=None)
    cur, st = _sprzataj([{"id": 8, "client_order_id": "tai-p43",
                          "status": "unknown", "symbol": "SOLUSDC"}], g)
    assert st["domkniete"] == 1
    assert g.anulowane == []                 # nie ma czego anulować
    assert any("nie zna tego zlecenia" in str(p) for _, p in cur.sql if p)


def test_sprzatanie_NIE_RUSZA_planow_ktore_jeszcze_czekaja():
    """Zlecenie czekające na swoją świecę nie jest sierotą. Ruszanie go
    zabierałoby rozstrzyganiu jego własną pracę."""
    cur, _ = _sprzataj([], Gielda())
    (sql, _), = [(s, p) for s, p in cur.sql if "FROM exec.order o" in s]
    assert "p.status::text <> 'pending'" in sql


def test_sprzatanie_ZGLASZA_wypelnienie_po_fakcie_zamiast_je_ksiegowac():
    """Księgowanie należy do rozstrzygania; robienie tego w dwóch miejscach
    rozjechałoby się przy pierwszej zmianie. Ale pozycja poza dziennikiem musi
    zostawić ślad — to jest realna ekspozycja, której risk kernel nie liczy."""
    g = Gielda(stan=_stan(D("10"), status="filled"))
    cur, st = _sprzataj([{"id": 9, "client_order_id": "tai-p44",
                          "status": "new", "symbol": "SOLUSDC"}], g)
    assert st["wypelnione_po_fakcie"] == 1
    assert not any("INSERT INTO exec.position" in s for s, _ in cur.sql)
    assert any("pozycja poza dziennikiem" in str(p) for _, p in cur.sql if p)


def test_prowizja_ksiegowana_w_WALUCIE_Z_GIELDY_a_nie_kwotowanej():
    """Na spocie Bybit pobiera prowizję od kupna W KUPIONYM AKTYWIE.

    Wpisanie waluty kwotowanej dawało dwa błędy naraz, oba w tę samą stronę:
    koszt wejścia zaniżony około stukrotnie (0,01 SOL zapisane jako 0,01 USDC
    zamiast ~1 USDC), a ilość w dzienniku wyższa od faktycznego salda o wartość
    prowizji — dokładnie na progu tolerancji uzgodnienia, więc rozjazd bywałby
    zgłaszany losowo.

    `Wypelnienie` miało pole `prowizja_w` od początku i uzgodnienie z niego
    korzysta; ścieżka egzekucji czytała samą kwotę.
    """
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=D("10"), wypelniono=D("10"),
                        srednia_cena=D("100"), prowizja=D("0.01"),
                        prowizja_w="SOL")
    cur, _, _, _ = _rozstrzygnij(stan)
    (_, params), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.fill" in s]
    assert "SOL" in params, "waluta prowizji musi pochodzić z giełdy"
    assert "USDC" not in params


def test_brak_waluty_prowizji_spada_na_kwotowana():
    """Giełda nie zawsze poda to pole. Brak odczytu nie może zatrzymać zapisu —
    ale nie może też udawać, że wiemy więcej, niż wiemy.

    Tu ANI zlecenie, ANI rejestr wykonań nie podają waluty, więc zostaje
    dotychczasowy domysł — i zostawia po sobie ślad (test niżej)."""
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=D("10"), wypelniono=D("10"),
                        srednia_cena=D("100"), prowizja=D("0.25"),
                        prowizja_w=None)
    cur, _, _, _ = _rozstrzygnij(stan)
    (_, params), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.fill" in s]
    assert "USDC" in params


# ============================== Z5: waluta prowizji z REJESTRU WYKONAN

# Wejście ETHUSDC z produkcji (`exec.order` 47, `exec.fill` z 2026-09-22).
# Prowizja makera 0,1% pobrana W ETH: 0,3395 x 0,001 = 0,0003395.
PLAN_ETH = {"id": 42, "decision_id": 7, "instrument_id": 3, "side": "buy",
            "qty": D("0.3395"), "limit_price": D("2741.57"),
            "symbol": "ETHUSDC", "quote_ccy": "USDC", "base_ccy": "ETH",
            "trigger": {"rodzaj": "wejscie", "bar": "x"}}


def _stan_eth(prowizja_w=None):
    """Tak, jak NAPRAWDĘ odpowiada `/v5/order/*`: kwota bez jednostki.

    `cumExecFee` jest w odpowiedzi, `feeCurrency` NIE — to pole wystawia
    wyłącznie `/v5/execution/list`. Atrapa podająca `prowizja_w` opisywałaby
    endpoint, którego `stan_zlecenia` nie czyta.
    """
    return StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=D("0.3395"), wypelniono=D("0.3395"),
                        srednia_cena=D("2741.57"), prowizja=D("0.0003395"),
                        prowizja_w=prowizja_w)


def _wykonanie(prowizja="0.0003395", prowizja_w="ETH", id_wlasne="tai-p42"):
    from services.exchanges.base import Wypelnienie
    return Wypelnienie(id_gieldy="E1", id_wlasne=id_wlasne, symbol="ETHUSDC",
                       strona="Buy", ilosc=D("0.3395"), cena=D("2741.57"),
                       prowizja=D(prowizja), prowizja_w=prowizja_w,
                       czas=SUBMITTED)


def _wejscie_eth(rejestr):
    g = Gielda(stan=_stan_eth(), rejestr=rejestr)
    cur = Kursor([_wiersz_zlecenia(1, D("0.3395")), {"id": 1}])
    asyncio.run(rozstrzygnij_plan(
        cur, g, PLAN_ETH, Filtry(krok_ilosci=D("0.0001"), krok_ceny=D("0.01"),
                                 min_wartosc=D("5")),
        engine="crypto", mode="demo", kategoria="spot",
        teraz=SUBMITTED, kind="spot_eu"))
    (_, fill), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.fill" in s]
    (_, poz), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.position" in s]
    return cur, g, fill, poz


def test_waluta_prowizji_DOCZYTANA_z_rejestru_wykonan():
    """Sedno Z5, na liczbach z produkcji.

    `/v5/order/*` oddaje `cumExecFee` BEZ `feeCurrency`, więc `prowizja_w`
    wracało puste i egzekucja podstawiała walutę kwotowaną — na WSZYSTKICH
    trzech giełdowych kupnach w bazie (`fee / qty` = dokładnie 0,001, czyli
    stawka makera liczona od ILOŚCI). Skutkiem było `exec.position.qty`
    zapisane BRUTTO, bo `rozlicz_prowizje` nie widziało waluty bazowej.
    """
    _, g, fill, poz = _wejscie_eth([_wykonanie()])
    assert "ETH" in fill and "USDC" not in fill, (
        f"waluta prowizji nadal zgadywana zamiast doczytana: {fill}")
    # ILOŚĆ NETTO: 0,3395 - 0,0003395. To jest ta liczba, której brak sprawiał,
    # że gałąź pyłu w `loop.zapisz_wyjscie` była nieosiągalna.
    assert D("0.3391605") in poz, f"pozycja zapisana BRUTTO: {poz}"
    assert g.pytano_o_wypelnienia, "rejestr wykonań nie został w ogóle zapytany"


def test_exec_fill_trzyma_prowizje_SUROWA_a_pozycja_PRZELICZONA():
    """Dwie kolumny, dwa pytania — i tę różnicę widać dopiero po Z5.

    `exec.fill.fee` czyta się PARĄ z `exec.fill.fee_ccy`: `sprzatanie` odtwarza
    z niej ilość brutto wzorem `f.qty + f.fee WHEN fee_ccy = base_ccy`.
    Wpisanie tam kwoty przeliczonej na USDC obok `fee_ccy` = ETH dałoby brutto
    0,3392 + 0,9308 = 1,27 ETH przeciwko `cumExecQty` 0,3395, czyli ogromną
    nieistniejącą „pozycję poza dziennikiem".

    `exec.position.fees_paid` odwrotnie: `update_equity` sumuje je jako liczby
    BEZ jednostki, więc 0,0003395 wpisane wprost zaniżyłoby koszt 2700x — i to
    w stronę, która ZAWYŻA kapitał, więc kill-switch strzela później.
    """
    _, _, fill, poz = _wejscie_eth([_wykonanie()])
    assert D("0.0003395") in fill, f"`exec.fill.fee` nie jest surowe: {fill}"
    assert D("0.0003395") * D("2741.57") in poz, (
        f"`fees_paid` nie jest przeliczone na walutę kwotowaną: {poz}")


def test_kanarek_ROZNE_waluty_prowizji_NIE_SA_zgadywane():
    """Gdy wypełnienia jednego zlecenia podają różne waluty, jedna kolumna
    `fee_ccy` nie opisuje ich obu. Wybranie jednej byłoby nazwaniem zgadnięcia
    pomiarem — zostaje dotychczasowy domysł i głośna uwaga."""
    cur, _, fill, _ = _wejscie_eth(
        [_wykonanie(prowizja_w="ETH"), _wykonanie(prowizja_w="USDC")])
    assert "USDC" in fill and "ETH" not in fill
    slad = [p for s, p in cur.sql if "INSERT INTO exec.order" in s or "raw" in s]
    assert any("ROZNE waluty" in str(p) for p in slad), (
        "niezgodność walut przeszła bez śladu w wierszu zlecenia")


def test_kanarek_CUDZE_wypelnienie_nie_jest_brane_za_nasze():
    """Rejestr wykonań oddaje wypełnienia całego symbolu w oknie, nie tylko
    naszego zlecenia. Dopasowanie po `orderLinkId` jest tu warunkiem
    poprawności — inaczej przypisalibyśmy sobie cudzą walutę prowizji."""
    _, _, fill, poz = _wejscie_eth([_wykonanie(id_wlasne="tai-p999")])
    assert "USDC" in fill, "waluta wzięta z CUDZEGO wypełnienia"
    assert D("0.3395") in poz, "ilość policzona z cudzej prowizji"


def test_zrodlo_waluty_prowizji_ZOSTAJE_w_wierszu_zlecenia():
    """`fee_ccy` = USDC z odczytu i `fee_ccy` = USDC z domysłu wyglądają
    w bazie identycznie, a to jest różnica między pomiarem a zgadnięciem —
    i to ona decyduje, czy `exec.position.qty` jest netto, czy brutto."""
    cur, _, _, _ = _wejscie_eth([_wykonanie()])
    assert any("rejestr_wykonan" in str(p) for _, p in cur.sql if p)

    cur_bez, _, _, _ = _wejscie_eth([])          # rejestr milczy
    assert any("nieznane" in str(p) for _, p in cur_bez.sql if p)


def test_rejestr_NIEOSIAGALNY_nie_wywraca_rozstrzygniecia():
    """Wypełnienie już zaszło i aktywo jest nasze niezależnie od tego, czy
    poznamy jednostkę prowizji. Wywrócenie się tutaj zgubiłoby pozycję, którą
    naprawdę mamy — czyli stworzyłoby pozycję-widmo własną ręką."""
    class GieldaBezRejestru(Gielda):
        async def wypelnienia(self, **kw):
            raise ExchangeError("execution/list niedostepne", code="500")

    g = GieldaBezRejestru(stan=_stan_eth())
    cur = Kursor([_wiersz_zlecenia(1, D("0.3395")), {"id": 1}])
    rodzaj, _ = asyncio.run(rozstrzygnij_plan(
        cur, g, PLAN_ETH, Filtry(krok_ilosci=D("0.0001"), krok_ceny=D("0.01"),
                                 min_wartosc=D("5")),
        engine="crypto", mode="demo", kategoria="spot",
        teraz=SUBMITTED, kind="spot_eu"))
    assert rodzaj == "pelne"
    assert any("INSERT INTO exec.position" in s for s, _ in cur.sql)


def test_zlecenie_ktore_PODALO_walute_nie_pyta_rejestru():
    """Kanarek na koszt. Drugie zapytanie ma iść WYŁĄCZNIE wtedy, gdy pierwsze
    nie odpowiedziało — inaczej każde rozstrzygnięcie płaci za odczyt, którego
    nie potrzebuje."""
    g = Gielda(stan=_stan_eth(prowizja_w="ETH"), rejestr=[_wykonanie()])
    cur = Kursor([_wiersz_zlecenia(1, D("0.3395")), {"id": 1}])
    asyncio.run(rozstrzygnij_plan(
        cur, g, PLAN_ETH, Filtry(krok_ilosci=D("0.0001"), krok_ceny=D("0.01"),
                                 min_wartosc=D("5")),
        engine="crypto", mode="demo", kategoria="spot",
        teraz=SUBMITTED, kind="spot_eu"))
    assert not g.pytano_o_wypelnienia


def test_sprzatanie_ZGLASZA_reszte_wypelniona_na_wierszu_CZESCIOWYM():
    """Znalezione w audycie, poprawione 2026-09-08.

    Warunek brzmiał tu `o["status"] != "partial"` i gubił przez to CAŁĄ resztę
    zlecenia częściowo wypełnionego. Wiersz `partial` miał już zaksięgowane
    pierwsze wypełnienie, więc każde następne było wyciszane: reszta dopełniała
    się po rozstrzygnięciu planu, wiersz przechodził niżej na `filled` jako
    „domknięty", a różnica między dziennikiem a rachunkiem zostawała BEZ ŚLADU.

    Risk kernel liczy wtedy ekspozycję mniejszą niż faktyczna — a to jest
    dokładnie ta wielkość, na której stoi kill-switch.
    """
    g = Gielda(stan=_stan(D("1.0"), status="filled"))
    cur, st = _sprzataj([{"id": 11, "client_order_id": "tai-p60",
                          "status": "partial", "symbol": "SOLUSDC",
                          "lot_size": D("0.001"), "zaksiegowane": D("0.4")}], g)
    assert st["wypelnione_po_fakcie"] == 1, (
        "reszta wypełniona po rozstrzygnięciu musi zostawić ślad")
    assert any("poza dziennikiem" in str(p) for _, p in cur.sql if p)
    assert any("0.6" in str(p) for _, p in cur.sql if p), (
        "zgłoszenie ma podawać ILOŚĆ POZA DZIENNIKIEM, nie sumę wypełnienia")


def test_sprzatanie_MILCZY_gdy_wszystko_juz_zaksiegowane():
    """Druga granica: gdyby zgłaszane było każde wypełnienie, wiersz częściowy
    z w pełni zaksięgowanym wypełnieniem alarmowałby przy każdym przebiegu
    sprzątania — czyli codziennie, o czymś, co jest w porządku."""
    g = Gielda(stan=_stan(D("0.4"), status="filled"))
    cur, st = _sprzataj([{"id": 12, "client_order_id": "tai-p61",
                          "status": "partial", "symbol": "SOLUSDC",
                          "lot_size": D("0.001"), "zaksiegowane": D("0.4")}], g)
    assert st["wypelnione_po_fakcie"] == 0
    assert st["domkniete"] == 1


def test_sprzatanie_NIE_ALARMUJE_o_roznicy_MNIEJSZEJ_NIZ_KROK():
    """Różnicy poniżej połowy kroku ilości nie da się kupić ani sprzedać, więc
    jest to zaokrąglenie, a nie wypełnienie. Alarm na nim byłby alarmem
    o granicy zapisu liczby."""
    g = Gielda(stan=_stan(D("0.4000004"), status="filled"))
    cur, st = _sprzataj([{"id": 13, "client_order_id": "tai-p62",
                          "status": "partial", "symbol": "SOLUSDC",
                          "lot_size": D("0.001"), "zaksiegowane": D("0.4")}], g)
    assert st["wypelnione_po_fakcie"] == 0


def test_sprzatanie_pyta_baze_o_ILOSC_ZAKSIEGOWANA():
    """Bez tej kolumny funkcja nie ma jak odróżnić „wypełniło się ponad to, co
    zapisaliśmy" od „wypełniło się to, co już mamy" — i musiałaby wnioskować ze
    statusu wiersza, czyli wrócić do wady, którą to naprawia."""
    from pathlib import Path

    zrodlo = Path("services/engine/gielda_exec.py").read_text(encoding="utf-8")
    assert "FROM exec.fill f" in zrodlo and "AS zaksiegowane" in zrodlo


PROBA_F = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
                 min_wartosc=D("5"), krok_ceny=D("0.01"))


class GieldaZPamiecia:
    """Atrapa giełdy, która PAMIĘTA złożone zlecenia — tak jak prawdziwa.

    Atrapa oddająca z góry zadany stan odpowiada identycznie w dwóch
    sytuacjach, których cała próba dymna ma nie mylić: gdy zlecenie naprawdę
    poszło na giełdę i gdy nie wysłano niczego. Dlatego `stan_zlecenia` widzi
    tu wyłącznie to, co wcześniej przyjęło `zloz_zlecenie` — albo to, co siedzi
    w `historia`, czyli wpis z poprzedniego przebiegu, który u prawdziwej
    giełdy leży w historii zleceń przez siedem dni.

    `posty` to lista zleceń, które NAPRAWDĘ zostały wysłane. Pusta lista przy
    „przechodzącej" próbie znaczy, że próba potwierdziła własne echo.
    """

    def __init__(self, *, historia=(), wypelnij=False, status_po_zlozeniu="new",
                 anulowanie=True, zombie=False, blad_zlozenia=None):
        self.zlecenia = {}
        self.posty = []
        self.anulowane = []
        self._n = 0
        self._wypelnij = wypelnij
        self._status = status_po_zlozeniu
        self._anulowanie = anulowanie
        self._zombie = zombie
        self._blad_zlozenia = blad_zlozenia
        for id_w in historia:
            self._zapisz(id_w, D("0.12"), status="cancelled", wypelniono=D("0"))

    def _zapisz(self, id_wlasne, ilosc, *, status, wypelniono):
        self._n += 1
        self.zlecenia[id_wlasne] = StanZlecenia(
            id_wlasne=id_wlasne, id_gieldy=f"G{self._n}", status=status,
            ilosc=ilosc, wypelniono=wypelniono,
            srednia_cena=D("50") if wypelniono else None, prowizja=D("0"))
        return self.zlecenia[id_wlasne]

    async def zloz_zlecenie(self, **kw):
        if self._blad_zlozenia:
            raise self._blad_zlozenia
        # SPRAWDZ, POTEM SKLADAJ — jak adapter Bybita. Gdy giełda zna nasz
        # identyfikator, ŻADNE zapytanie POST nie wychodzi.
        znane = self.zlecenia.get(kw["id_wlasne"])
        if znane is not None:
            return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy=znane.id_gieldy,
                                   status=znane.status, juz_istnialo=True)
        self.posty.append(kw)
        z = self._zapisz(kw["id_wlasne"], kw["ilosc"],
                         status="filled" if self._wypelnij else self._status,
                         wypelniono=kw["ilosc"] if self._wypelnij else D("0"))
        return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy=z.id_gieldy,
                               status=z.status)

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return self.zlecenia.get(id_wlasne)

    async def anuluj_zlecenie(self, **kw):
        self.anulowane.append(kw["id_wlasne"])
        z = self.zlecenia.get(kw["id_wlasne"])
        if z is None or not self._anulowanie:
            return False
        if not self._zombie:
            self.zlecenia[kw["id_wlasne"]] = replace(z, status="cancelled")
        return True


def _proba(gielda, znacznik="crypto"):
    from services.engine.proba_egzekucji import przebieg

    return asyncio.run(przebieg(gielda, symbol="SOLUSDC", f=PROBA_F,
                                cena_rynkowa=D("200"), znacznik=znacznik))


def _niepowodzenia(kroki):
    """Kroki, które `main` liczy jako niezaliczenie próby."""
    return [k for k in kroki if "NIE POWIODŁO" in k]


def test_proba_egzekucji_NAPRAWDE_WYSYLA_ZLECENIE_na_gielde():
    """Próba, która nic nie wysłała, nie sprawdziła niczego.

    Liczymy ZŁOŻONE ZLECENIA, a nie komunikaty: przy stałym identyfikatorze
    wszystkie kroki wypisywały się tak samo mimo zera zapytań POST — i dokładnie
    tak wyglądał przebieg na produkcji.
    """
    g = GieldaZPamiecia()
    kroki = _proba(g)

    assert len(g.posty) == 1
    assert _niepowodzenia(kroki) == []
    # ARKUSZ MA ZOSTAC PUSTY. Próba zostawiająca zlecenie jest gorsza niż jej brak.
    (id_w,) = [p["id_wlasne"] for p in g.posty]
    assert g.zlecenia[id_w].status == "cancelled"


def test_proba_egzekucji_DRUGIE_URUCHOMIENIE_tez_wysyla_zlecenie():
    """Narzędzie mające łapać regresję nie może przestać działać po pierwszym użyciu.

    `stan_zlecenia` przeszukuje także historię zleceń, a `zloz_zlecenie` pyta
    o stan ZANIM cokolwiek wyśle. Stały identyfikator sprawiał więc, że drugie
    uruchomienie odnajdywało wpis po pierwszym: idempotencja „potwierdzała się"
    bez jednego zapytania POST, anulowanie meldowało „nie było czego", a próba
    kończyła się słowami „przechodzi cały obieg".
    """
    g = GieldaZPamiecia()
    for _ in range(2):
        assert _niepowodzenia(_proba(g)) == []

    assert len(g.posty) == 2                      # OBA przebiegi coś wysłały
    assert len({p["id_wlasne"] for p in g.posty}) == 2


def test_proba_egzekucji_NIE_ZALICZA_gdy_gielda_ZNA_JUZ_identyfikator(monkeypatch):
    """Drugi bezpiecznik, na wypadek gdyby identyfikator przestał być świeży.

    Gdy giełda zna nasz jednorazowy identyfikator, nic nie zostało wysłane —
    a wtedy reszta próby ogląda cudzy wpis: anulowanie zdejmuje nie to zlecenie,
    a idempotencja potwierdza się sama.
    """
    import services.engine.proba_egzekucji as pe

    monkeypatch.setattr(pe, "id_proby", lambda znacznik: "proba-egz-stale")
    g = GieldaZPamiecia(historia=["proba-egz-stale"])
    with pytest.raises(pe.Niezaliczona) as exc:
        _proba(g)

    assert g.posty == []
    assert "ZNAŁA JUŻ" in str(exc.value)


def test_proba_egzekucji_ODRZUCA_odczyt_zlecenia_KTORE_NIE_LEZY_W_ARKUSZU():
    """Świeżo złożone zlecenie ma leżeć w arkuszu jako `new`.

    Na produkcji odczyt zwrócił `cancelled` — wpis z poprzedniego przebiegu —
    a próba przyjęła to jako poprawny stan i poszła dalej sprawdzać idempotencję
    zlecenia, którego już nie było.
    """
    from services.engine.proba_egzekucji import Niezaliczona

    g = GieldaZPamiecia(status_po_zlozeniu="cancelled")
    with pytest.raises(Niezaliczona) as exc:
        _proba(g)
    assert "'cancelled'" in str(exc.value)


def test_proba_egzekucji_NIE_ZALICZA_gdy_NIE_BYLO_CZEGO_anulowac():
    """„Nie było czego zdjąć" po świeżo złożonym zleceniu to awaria anulowania.

    Zlecenie leżało tam sekundę wcześniej i nie było wypełnione. Odpowiedź
    „nie znam takiego" znaczy więc, że anulowanie trafia gdzie indziej — a na
    nim opiera się kill-switch.
    """
    g = GieldaZPamiecia(anulowanie=False)
    kroki = _proba(g)
    assert g.anulowane                            # próbowaliśmy
    assert _niepowodzenia(kroki)


def test_proba_egzekucji_POTWIERDZA_ANULOWANIE_ODCZYTEM_a_nie_odpowiedzia():
    """Bybit na zlecenie już wypełnione odpowiada kodem, który adapter tłumaczy
    na „nie było czego zdejmować" — samo „poszło bez błędu" nie znaczy więc, że
    arkusz jest czysty. Liczy się stan po anulowaniu, nie potwierdzenie."""
    g = GieldaZPamiecia(zombie=True)              # potwierdza, ale nie zdejmuje
    kroki = _proba(g)
    assert _niepowodzenia(kroki)


def test_proba_egzekucji_MELDUJE_ODRZUCENIE_gieldy_jako_wynik():
    """Odrzucenie złożenia jest WYNIKIEM próby, nie jej awarią — tak wygląda
    parametr, którego podmiot nie przyjmuje (dziś `timeInForce: PostOnly`).
    Ślad stosu mówiłby o naszym kodzie zamiast o giełdzie."""
    from services.engine.proba_egzekucji import Niezaliczona

    g = GieldaZPamiecia(blad_zlozenia=ExchangeError("PostOnly nieobsługiwany"))
    with pytest.raises(Niezaliczona) as exc:
        _proba(g)
    assert "ODRZUCIŁA" in str(exc.value)


def test_identyfikator_proby_JEST_INNY_przy_kazdym_uruchomieniu():
    """Odwrotnie niż w egzekucji, gdzie stały identyfikator z planu jest jedyną
    ochroną przed podwójną pozycją. Tutaj stałość unieważnia całą próbę."""
    from services.engine.proba_egzekucji import PREFIKS, id_proby

    ident = [id_proby("crypto") for _ in range(5)]
    assert len(set(ident)) == 5
    assert all(i.startswith(PREFIKS) for i in ident)   # nie do pomylenia z planem
    assert all(len(i) <= 36 for i in ident)            # limit orderLinkId u Bybita




# =========================================== wyjscie nie czeka swiecy (T6, F-4.3)
#
# Do 2026-10-03 wyjscie odrzucone przez `EC_PostOnlyWillTakeLiquidity` wygasalo
# i czekalo na nastepna decyzje - 4 h. Zmierzony koszt: 85 USD na 4 przypadkach
# (NEAR #38 sam 60 USD, 480 pb pozycji). Teraz: druga proba PostOnly po swiezym
# arkuszu, potem GTC po drugiej stronie spreadu. Wejscia NIE eskaluja.

from services.engine.gielda_exec import (  # noqa: E402
    PROBY_POSTONLY_WYJSCIA, proba_z_id)
from services.exchanges.base import PowodOdmowy  # noqa: E402

POSTONLY = PowodOdmowy(kod="EC_PostOnlyWillTakeLiquidity", rodzaj="postonly",
                       opis="zlecenie przecielo spread [rejectReason="
                            "EC_PostOnlyWillTakeLiquidity]")

WYJSCIE = dict(PLAN, id=42, side="sell", qty=D("10"), limit_price=D("100"),
               base_ccy="SOL", atr_pct=2.0,
               trigger={"rodzaj": "wyjscie", "bar": BAR, "position_id": 32})


def _odrzucone(id_wlasne, n):
    return StanZlecenia(id_wlasne=id_wlasne, id_gieldy=f"G{n}", status="cancelled",
                        ilosc=D("10"), wypelniono=D("0"), srednia_cena=None,
                        prowizja=D("0"), powod_odmowy=POSTONLY)


class GieldaPostOnly:
    """Gielda, ktora PAMIETA zlecenia po `orderLinkId` i odrzuca PostOnly.

    `odrzuc` - ile kolejnych zlecen PostOnly gielda odrzuci (stan widoczny
    w `stan_zlecenia` zaraz po POST-cie, jak `EC_PostOnlyWillTakeLiquidity`
    na Bybicie). Zlecenie bez PostOnly zawsze przyjmuje. `arkusze` - kolejne
    odczyty arkusza; ostatni powtarza sie do konca.
    """

    def __init__(self, *, odrzuc=0, arkusze=None, znane=None, blad_stanu_po=False):
        self._odrzuc = odrzuc
        self._arkusze = list(arkusze or [("100.00", "100.02")])
        self.zlecenia = dict(znane or {})
        self.zlozone = []
        self.pytania_o_stan = []
        self._blad_stanu_po = blad_stanu_po

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        self.pytania_o_stan.append(id_wlasne)
        if self._blad_stanu_po and self.zlozone and \
                self.zlozone[-1]["id_wlasne"] == id_wlasne:
            raise ExchangeError("timeout odczytu stanu")
        return self.zlecenia.get(id_wlasne)

    async def zloz_zlecenie(self, **kw):
        self.zlozone.append(kw)
        n = len(self.zlozone)
        if kw.get("post_only", True) and self._odrzuc > 0:
            self._odrzuc -= 1
            self.zlecenia[kw["id_wlasne"]] = _odrzucone(kw["id_wlasne"], n)
        else:
            self.zlecenia[kw["id_wlasne"]] = StanZlecenia(
                id_wlasne=kw["id_wlasne"], id_gieldy=f"G{n}", status="new",
                ilosc=kw["ilosc"], wypelniono=D("0"), srednia_cena=None,
                prowizja=D("0"))
        return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy=f"G{n}",
                               status="new")

    async def cena_odniesienia(self, symbol, kategoria=None):
        return None

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        bid, ask = self._arkusze.pop(0) if len(self._arkusze) > 1 else self._arkusze[0]
        return arkusz(bid, ask)


def _zloz_wyjscie(g, plan=WYJSCIE):
    cur = Kursor()
    ok, msg = asyncio.run(zloz_plan(cur, g, plan, F, engine="crypto",
                                    mode="demo", kategoria="spot"))
    return cur, ok, msg


def _wiersze_zlecen(cur):
    """(sql, params, raw) kazdego INSERT-u do exec.order."""
    import json as _json
    return [(s, p, _json.loads(p[-1])) for s, p in cur.zapytania("INSERT INTO exec.order")]


def test_wyjscie_odrzucone_DWA_RAZY_idzie_trzeci_raz_BEZ_PostOnly_w_tym_samym_cyklu():
    """SEDNO T6. Trzy wywołania `zloz_zlecenie`: dwa PostOnly, trzecie GTC.

    Druga próba ma cenę ze ŚWIEŻEGO arkusza (rynek zszedł między odczytami),
    trzecia stoi NA BIDZIE - po drugiej stronie spreadu - i dlatego wypełnia
    się takerem zamiast wrócić jako trzecie odrzucenie."""
    # Kazda proba czyta arkusz DWA razy: przed POST-em (cena) i po nim
    # (`arkusz_po_wysylce`, diagnostyka).
    g = GieldaPostOnly(odrzuc=2, arkusze=[("100.00", "100.02")] * 2
                                         + [("99.90", "99.92")] * 2
                                         + [("99.80", "99.82")])
    cur, ok, msg = _zloz_wyjscie(g)

    assert ok is True, msg
    assert [z["id_wlasne"] for z in g.zlozone] == ["tai-p42", "tai-p42-r1", "tai-p42-a"]
    assert "post_only" not in g.zlozone[0] and "post_only" not in g.zlozone[1]
    assert g.zlozone[2]["post_only"] is False
    # Pasywnie o tick nad askiem, potem znowu - z NOWEGO arkusza.
    assert g.zlozone[0]["cena_limitu"] == D("100.03")
    assert g.zlozone[1]["cena_limitu"] == D("99.93")
    # Agresywnie: sprzedaz NA BIDZIE, nie nad askiem.
    assert g.zlozone[2]["cena_limitu"] == D("99.80")


def test_odrzucone_ogniwa_zostaja_jako_ZLECENIA_BEZ_PLANU_a_plan_ma_JEDEN_wiersz():
    """Jeden wiersz z `planned_order_id` na plan - na tym stoi rozstrzyganie
    (`ORDER BY id LIMIT 1`). Odrzucone ogniwa zostają w `exec.order`, bo poszły
    na giełdę i liczą się do wypełnialności - ale BEZ planu i z sufiksem, żeby
    UNIQUE (mode, client_order_id) niczego później nie wywróciło."""
    g = GieldaPostOnly(odrzuc=2)
    cur, ok, _ = _zloz_wyjscie(g)
    assert ok is True

    wiersze = _wiersze_zlecen(cur)
    assert len(wiersze) == 3
    z_planem = [w for w in wiersze if "VALUES (%s,NULL," not in w[0]]
    bez_planu = [w for w in wiersze if "VALUES (%s,NULL," in w[0]]
    assert len(z_planem) == 1 and len(bez_planu) == 2

    (_, params, raw), = z_planem
    assert params[1] == 42 and "tai-p42-a" in params
    assert raw["wyjscie_agresywne"] is True
    assert raw["proba"] == PROBY_POSTONLY_WYJSCIA

    for _, params, raw in bez_planu:
        assert raw["planned_order_id"] == 42
        assert raw["powod_odmowy_rodzaj"] == "postonly"
        assert any(isinstance(p, str) and p.endswith("~postonly") for p in params)
    # Plan NIE jest odrzucony - jego zlecenie lezy w arkuszu.
    assert not cur.zapytania("journal.planned_order")


def test_wyjscie_przyjete_za_DRUGIM_razem_nie_eskaluje_dalej():
    g = GieldaPostOnly(odrzuc=1)
    cur, ok, _ = _zloz_wyjscie(g)
    assert ok is True
    assert [z["id_wlasne"] for z in g.zlozone] == ["tai-p42", "tai-p42-r1"]
    assert all("post_only" not in z for z in g.zlozone)
    (_, _, raw), = [w for w in _wiersze_zlecen(cur) if "VALUES (%s,NULL," not in w[0]]
    assert raw["wyjscie_agresywne"] is False and raw["proba"] == 1


def test_wyjscie_bez_odrzucenia_zostawia_FLAGE_false_jako_grupe_odniesienia():
    """Udział agresywnych wyjść (`poslizg.udzial_wyjsc_takerem`) potrzebuje
    mianownika - flaga `false` na każdym zwykłym wyjściu."""
    g = GieldaPostOnly(odrzuc=0)
    cur, ok, _ = _zloz_wyjscie(g)
    assert ok is True and len(g.zlozone) == 1
    (_, _, raw), = _wiersze_zlecen(cur)
    assert raw["wyjscie_agresywne"] is False and raw["proba"] == 0


def test_WEJSCIE_odrzucone_przez_PostOnly_NIE_eskaluje():
    """Niewypełnione wejście kosztuje okazję; wejście takerem to strategia,
    której symulacja nie zna. Jedno zlecenie, bez pytania o stan po wysyłce.

    KONTRAST W TYM SAMYM TEŚCIE: ta sama giełda eskaluje wyjście - inaczej
    test przechodziłby także dla kodu, który nie eskaluje niczego."""
    g_wyj = GieldaPostOnly(odrzuc=5)
    _zloz_wyjscie(g_wyj)
    assert len(g_wyj.zlozone) == PROBY_POSTONLY_WYJSCIA + 1

    g = GieldaPostOnly(odrzuc=5)
    plan = dict(PLAN, atr_pct=2.0)
    cur = Kursor()
    ok, _ = asyncio.run(zloz_plan(cur, g, plan, F, engine="crypto",
                                  mode="demo", kategoria="spot"))
    assert ok is True
    assert len(g.zlozone) == 1
    assert "post_only" not in g.zlozone[0]
    assert g.pytania_o_stan == ["tai-p42"]          # tylko idempotencja PRZED
    (_, _, raw), = _wiersze_zlecen(cur)
    assert "wyjscie_agresywne" not in raw


def test_BLAD_odczytu_stanu_po_wysylce_NIE_eskaluje():
    """O zleceniu nic nie wiemy. Drugie zlecenie obok żywego pierwszego to
    podwójna sprzedaż - zostaje zwykły wiersz, resztę złapie rozstrzyganie."""
    g = GieldaPostOnly(odrzuc=2, blad_stanu_po=True)
    cur, ok, _ = _zloz_wyjscie(g)
    assert ok is True
    assert len(g.zlozone) == 1
    # Kontrast: bez bledu odczytu ta sama gielda daje trzy zlecenia.
    assert len(_zloz_wyjscie(GieldaPostOnly(odrzuc=2))[0].zapytania(
        "INSERT INTO exec.order")) == 3
    (_, params, _), = _wiersze_zlecen(cur)
    assert params[1] == 42


def test_IDEMPOTENCJA_po_restarcie_odnajduje_ZYWE_ogniwo_i_niczego_nie_sklada():
    """Cykl wysłał `tai-p42` (odrzucone) i `tai-p42-r1` (leży), po czym zginął
    przed commitem. Następny cykl pyta po ogniwach - i nie składa NICZEGO."""
    zywe = StanZlecenia(id_wlasne="tai-p42-r1", id_gieldy="G2", status="new",
                        ilosc=D("10"), wypelniono=D("0"), srednia_cena=None,
                        prowizja=D("0"))
    g = GieldaPostOnly(znane={"tai-p42": _odrzucone("tai-p42", 1),
                              "tai-p42-r1": zywe})
    cur, ok, msg = _zloz_wyjscie(g)
    assert ok is True, msg
    assert g.zlozone == []
    assert g.pytania_o_stan == ["tai-p42", "tai-p42-r1"]
    (_, params, raw), = _wiersze_zlecen(cur)
    assert "tai-p42-r1" in params and params[1] == 42
    assert raw["juz_istnialo"] is True


def test_IDEMPOTENCJA_po_restarcie_kontynuuje_OD_NASTEPNEGO_ogniwa():
    """Gielda zna tylko odrzucone `tai-p42` - nastepna proba to `-r1`, a nie
    drugi raz `tai-p42` (duplikat 170141) ani od razu taker."""
    g = GieldaPostOnly(znane={"tai-p42": _odrzucone("tai-p42", 1)})
    cur, ok, _ = _zloz_wyjscie(g)
    assert ok is True
    assert [z["id_wlasne"] for z in g.zlozone] == ["tai-p42-r1"]
    assert "post_only" not in g.zlozone[0]


def test_identyfikatory_ogniw_sa_ROZNE_STALE_i_ODWRACALNE():
    ids = [id_zlecenia(42, p) for p in range(PROBY_POSTONLY_WYJSCIA + 1)]
    assert ids == ["tai-p42", "tai-p42-r1", "tai-p42-a"]
    assert len(set(ids)) == len(ids)
    assert all(len(i) <= 36 for i in ids)          # limit orderLinkId u Bybita
    assert [proba_z_id(42, i) for i in ids] == [0, 1, 2]
    assert proba_z_id(43, "tai-p42") is None


def _rozstrzygnij_wyjscie(stan, *, ponow, client_order_id="tai-p42"):
    class G(Gielda):
        async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
            self.pytano_o_stan = id_wlasne
            return stan
    g = G()
    plan = dict(WYJSCIE, client_order_id=client_order_id,
                valid_until=None)
    cur = Kursor([dict(id=501, qty=D("10"), submitted_at=None)])
    wynik = asyncio.run(rozstrzygnij_plan(
        cur, g, plan, F, engine="crypto", mode="demo", kategoria="spot",
        teraz=__import__("datetime").datetime(2026, 10, 3, 12, 5,
                                              tzinfo=__import__("datetime").timezone.utc),
        kind="spot", ponow_wyjscie=ponow))
    return cur, g, wynik


def test_odrzucenie_widoczne_DOPIERO_przy_rozstrzyganiu_nie_wygasza_planu():
    """Pierwszy odczyt po wysyłce widział `new`, giełda opisała odrzucenie
    później. Przed terminem planu ogniwo jest wycofywane, plan zostaje
    `pending` bez wiersza i `zloz_oczekujace` złoży następną próbę w tym
    samym cyklu."""
    cur, g, (rodzaj, _) = _rozstrzygnij_wyjscie(_odrzucone("tai-p42", 1), ponow=True)
    assert rodzaj == "wyjscie_ponowione"
    assert not cur.zapytania("journal.planned_order")
    (sql, params), = cur.zapytania("UPDATE exec.order")
    assert "planned_order_id=NULL" in sql and "'~postonly'" in sql
    assert params[-1] == 501


def test_po_terminie_planu_odrzucone_wyjscie_WYGASA_jak_dotad():
    """Po terminie następna świeca ma własną decyzję - drugie wyjście z tej
    samej pozycji obok jej planu byłoby podwójną sprzedażą."""
    cur, _, (rodzaj, _) = _rozstrzygnij_wyjscie(_odrzucone("tai-p42", 1), ponow=False)
    assert rodzaj == "brak"
    assert cur.zapytania("SET status='expired'")


def test_rozstrzyganie_pyta_o_ID_Z_WIERSZA_a_nie_o_pierwsze_ogniwo():
    """Zastępcze zlecenie żyje pod `tai-p42-a`. Pytanie o `tai-p42` oddałoby
    pierwsze, odrzucone ogniwo i rozstrzygnęło plan jako niewypełniony."""
    zywe = StanZlecenia(id_wlasne="tai-p42-a", id_gieldy="G3", status="new",
                        ilosc=D("10"), wypelniono=D("0"), srednia_cena=None,
                        prowizja=D("0"))
    _, g, _ = _rozstrzygnij_wyjscie(zywe, ponow=True, client_order_id="tai-p42-a")
    assert g.pytano_o_stan == "tai-p42-a"


def test_bybit_post_only_False_wysyla_GTC_a_domysl_zostaje_PostOnly():
    """Patrzymy na CIAŁO zapytania, które naprawdę poszło."""
    import json as _json

    import httpx

    from services.exchanges.bybit import BybitAdapter

    wyslane = []

    def handler(req):
        if "order/create" in req.url.path:
            wyslane.append(_json.loads(req.content))
            return httpx.Response(200, json=dict(retCode=0, result=dict(orderId="G1")))
        return httpx.Response(200, json=dict(retCode=0, result=dict(list=[])))

    a = BybitAdapter("klucz", "sekret", mode="demo")
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(a.zloz_zlecenie(symbol="SOLUSDC", strona="sell", ilosc=D("1"),
                                cena_limitu=D("99.80"), id_wlasne="tai-p42-a",
                                post_only=False, istnienie_sprawdzone=True))
    asyncio.run(a.zloz_zlecenie(symbol="SOLUSDC", strona="sell", ilosc=D("1"),
                                cena_limitu=D("100.03"), id_wlasne="tai-p42",
                                istnienie_sprawdzone=True))
    asyncio.run(a.close())
    assert wyslane[0]["timeInForce"] == "GTC" and wyslane[0]["orderType"] == "Limit"
    assert wyslane[0]["price"] == "99.80"
    assert wyslane[1]["timeInForce"] == "PostOnly"


def test_udzial_wyjsc_takerem_liczy_TYLKO_wiersze_z_flaga():
    """Mianownik to wyjścia z polem `wyjscie_agresywne` (z bazy przychodzi
    jako tekst 'true'/'false'); wejścia i zlecenia sprzed zmiany nie są ani
    agresywne, ani pasywne. Pusta próba to None, nie zero."""
    from services.engine.poslizg import udzial_wyjsc_takerem

    zl = [{"wyjscie_agresywne": "true"}, {"wyjscie_agresywne": "false"},
          {"wyjscie_agresywne": "false"}, {"wyjscie_agresywne": "false"},
          {"wyjscie_agresywne": None}, {}]
    assert udzial_wyjsc_takerem(zl) == {"wyjsc": 4, "agresywnych": 1, "udzial": 0.25}
    assert udzial_wyjsc_takerem([{}])["udzial"] is None
