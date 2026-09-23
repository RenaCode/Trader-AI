"""Egzekucja na giełdzie — to giełda rozstrzyga, nie nasze założenie o świecy."""
import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest

from services.engine.gielda_exec import id_zlecenia, rozstrzygnij_plan, zloz_plan
from services.engine.horizons import get as get_horizon
from services.engine.zlecenia import Filtry
from services.exchanges.base import ExchangeError, StanZlecenia, ZlozoneZlecenie

F = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
           min_wartosc=D("5"), krok_ceny=D("0.01"))
BEZ_REGUL = Filtry()

PLAN = {"id": 42, "decision_id": 7, "instrument_id": 3, "side": "buy",
        "qty": D("10"), "limit_price": D("100"), "symbol": "SOLUSDC",
        "quote_ccy": "USDC", "trigger": {"rodzaj": "wejscie", "bar": "x"}}


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


class Gielda:
    def __init__(self, *, zlozenie=None, stan=None, blad_zlozenia=None,
                 blad_anulowania=None, odniesienie=None):
        self._odniesienie = odniesienie
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

    async def cena_odniesienia(self, symbol, kategoria=None):
        """Cena z ksiegi PODMIOTU. None znaczy „nie da sie odczytac"."""
        return self._odniesienie

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


def _rozstrzygnij(stan, plan=PLAN, gielda=None, wiersze=None):
    # Pierwszy wiersz to ZLECENIE ZLOZONE WCZESNIEJ: jego `qty` jest podstawa
    # ulamka wypelnienia, bo to ona poszla na gielde. Ilosc z planu jest sprzed
    # zaokraglenia do kroku i uzycie jej robi z kazdego pelnego wypelnienia
    # czesciowe.
    cur = Kursor(wiersze or [{"id": 1, "qty": D("10")}, {"id": 1}])
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


def test_petla_NIE_bramkuje_wysylki_stanem_killswitcha():
    """Reguła „wyjścia przechodzą przez kill-switch" żyje w `run_once`, którego
    testy jednostkowe nie obejmują — wymaga bazy.

    Sprawdzamy więc samo wywołanie: wysyłka ma być wołana ZAWSZE, a stan
    kill-switcha ma trafiać do `tylko_wyjscia`, nie do warunku wejścia. Gdyby
    trafiał do warunku, plany wyjścia powstawałyby i nigdy nie szły na giełdę —
    pozycje zostałyby uwięzione dokładnie wtedy, gdy ryzyko jest za duże.

    KOMENTARZE SĄ ODFILTROWANE przed przeszukaniem. Wcześniejszy test w tym
    projekcie przechodził, bo szukanego wzorca dostarczał komentarz nad kodem.
    """
    from pathlib import Path

    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))

    (wywolanie,) = [b for b in kod.split("zloz_oczekujace(")[1:]
                    if "tylko_wyjscia" in b]
    assert "tylko_wyjscia=wejscia_wstrzymane" in wywolanie.split(")")[0]

    warunek = kod.split("from services.engine.gielda_exec import zloz_oczekujace")[0]
    ostatni_if = warunek.rstrip().splitlines()[-1]
    assert "gielda is not None" in ostatni_if
    assert "wejscia_wstrzymane" not in ostatni_if


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
    cur = Kursor([{"id": 501, "qty": D("10")}, {"id": 1}])
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
        stan, plan=plan, wiersze=[{"id": 1, "qty": wyslano}, {"id": 1}])
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
    cur = Kursor([{"id": 1, "qty": D("10")}, None])
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


def test_brak_odczytu_ksiegi_NIE_zatrzymuje_handlu():
    """Awaria jednego endpointu, która wstrzymuje handel, jest gorszym
    lekarstwem niż choroba. Brak odniesienia wyłącza samo pasmo, a nie
    zlecenie — ale zostawia to widoczne w opisie."""
    plan = dict(PLAN, atr_pct=1.0)
    g = Gielda(odniesienie=None)
    _, gielda, ok, _ = _zloz(plan=dict(plan, limit_price=D("200")), gielda=g)

    assert ok is True
    assert len(gielda.zlozone) == 1


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
    cur = Kursor([{"id": 1, "qty": D("10")}, {"id": 1}])

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
    ale nie może też udawać, że wiemy więcej, niż wiemy."""
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=D("10"), wypelniono=D("10"),
                        srednia_cena=D("100"), prowizja=D("0.25"),
                        prowizja_w=None)
    cur, _, _, _ = _rozstrzygnij(stan)
    (_, params), = [(s, p) for s, p in cur.sql if "INSERT INTO exec.fill" in s]
    assert "USDC" in params


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


