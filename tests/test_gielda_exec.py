"""Egzekucja na giełdzie — to giełda rozstrzyga, nie nasze założenie o świecy."""
import asyncio
from decimal import Decimal as D

import pytest

from services.engine.gielda_exec import id_zlecenia, rozstrzygnij_plan, zloz_plan
from services.engine.zlecenia import Filtry
from services.exchanges.base import ExchangeError, StanZlecenia, ZlozoneZlecenie

F = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
           min_wartosc=D("5"), krok_ceny=D("0.01"))
BEZ_REGUL = Filtry()

PLAN = {"id": 42, "decision_id": 7, "instrument_id": 3, "side": "buy",
        "qty": D("10"), "limit_price": D("100"), "symbol": "SOLUSDC",
        "quote_ccy": "USDC", "trigger": {"rodzaj": "wejscie", "bar": "x"}}


class Kursor:
    """Atrapa kursora: zapamiętuje SQL, oddaje z góry zadane wiersze.

    ATRAPA BEZ OGRANICZEŃ PRZEPUSZCZA INSTRUKCJE, KTÓRE BAZA ODRZUCA. Test
    dopasowujący sam tekst SQL-a przechodzi wtedy także dla zapytania
    niewykonalnego — dlatego tu sprawdzamy przynajmniej to, co da się sprawdzić
    bez bazy: liczba miejsc na parametry musi się zgadzać z liczbą parametrów.
    Rozjazd w tę stronę jest najczęstszym błędem przy ręcznie sklejanych
    UPDATE-ach i psuje zapytanie dopiero na produkcji.
    """

    def __init__(self, wiersze=None):
        self.sql = []
        self._wiersze = list(wiersze or [])

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        oczekiwane = jedno.count("%s")
        podane = 0 if params is None else len(params)
        assert oczekiwane == podane, (
            f"zapytanie ma {oczekiwane} miejsc na parametry, a dostało {podane}: "
            f"{jedno}")
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
                 blad_anulowania=None):
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
    asyncio.run(anuluj_wejscia(cur, Gielda(), kategoria="spot", powod="test"))
    (sql, _), = [(s, p) for s, p in cur.sql if "SELECT o.client_order_id" in s]
    assert "'rodzaj', 'wejscie') <> 'wyjscie'" in sql


def test_wysylka_przy_killswitchu_obejmuje_TYLKO_wyjscia():
    """Plany wyjścia powstają także przy wstrzymanych wejściach. Niewysłanie
    ich uwięziłoby pozycje dokładnie wtedy, gdy ryzyko jest za duże."""
    from services.engine.gielda_exec import zloz_oczekujace

    cur = Kursor()
    asyncio.run(zloz_oczekujace(cur, Gielda(), engine="crypto", mode="demo",
                                kategoria="spot", verbose=False,
                                tylko_wyjscia=True))
    (sql, params), = [(s, p) for s, p in cur.sql if "journal.planned_order" in s]
    assert "p.trigger->>'rodzaj' = 'wyjscie'" in sql
    assert params == (True,)


def test_wysylka_bez_killswitcha_obejmuje_WSZYSTKIE_plany():
    from services.engine.gielda_exec import zloz_oczekujace

    cur = Kursor()
    asyncio.run(zloz_oczekujace(cur, Gielda(), engine="crypto", mode="demo",
                                kategoria="spot", verbose=False))
    (_, params), = [(s, p) for s, p in cur.sql if "journal.planned_order" in s]
    assert params == (False,)


def test_nieudane_anulowanie_zostawia_SLAD_a_nie_cisze():
    """„Kill-switch zadziałał" musi znaczyć to, co znaczy. Zlecenie, którego
    nie udało się zdjąć, dalej leży w arkuszu."""
    from services.engine.gielda_exec import anuluj_wejscia

    cur = Kursor([{"client_order_id": "tai-p1", "symbol": "SOLUSDC",
                   "planned_order_id": 1}])
    g = Gielda(blad_anulowania=ExchangeError("nie poszlo"))
    zdjete, _ = asyncio.run(anuluj_wejscia(cur, g, kategoria="spot", powod="test"))
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
        anuluj_wejscia(cur, g, kategoria="spot", powod="test"))

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
