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
    """Atrapa kursora: zapamiętuje SQL, oddaje z góry zadane wiersze."""

    def __init__(self, wiersze=None):
        self.sql = []
        self._wiersze = list(wiersze or [])

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._wiersze.pop(0) if self._wiersze else None

    def fetchall(self):
        w, self._wiersze = self._wiersze, []
        return w

    def zawiera(self, fragment):
        return any(fragment in s for s, _ in self.sql)


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
    cur = Kursor(wiersze or [{"id": 1}])
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
    zdjete = asyncio.run(anuluj_wejscia(cur, g, kategoria="spot", powod="test"))
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
