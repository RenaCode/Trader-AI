"""`loop.run_once` WYKONANY, a nie przeczytany (audyt Fable U-1).

DLACZEGO TEN PLIK ISTNIEJE

`run_once` to ~700 linii, na których stoją wszystkie reguły bezpiecznika:
kill-switch wstrzymuje WEJŚCIA, a wyjścia idą dalej; bramka sesji kończy cykl
pulsem; szeroki spread i spóźniony potok zamieniają wejście na `hold` z własnym
powodem. Do 2026-10-03 żaden test bez bazy nie wykonał z tego ani jednej
instrukcji (pokrycie 1/230) — reguły pilnowały asercje `"…" in ZRODLO`, które
nie wykonują kodu, przechodzą na komentarzu i pękają przy refaktorze.

JAK TO DZIAŁA

Kursor to `tests/atrapa_bazy.Kursor`: zna ograniczenia `journal.planned_order`
i `exec.*` z migracji, więc plan zapisany przez prawdziwe `zaplanuj()` jest
sprawdzany na `CHECK`/`NOT NULL`/kolumnach. Podmienione są wyłącznie funkcje,
które CZYTAJĄ bazę (`active_model`, `latest_features`, `zakres_swiecy`, …)
i `RiskKernel` — czyli źródła stanu, nie reguły. Wszystko, co rozstrzyga
o akcji, wykonuje się naprawdę: bramki, `position_budget`, `wielkosc_pozycji`,
`decyzja_przeterminowana`, `za_szeroki_spread`, `zaplanuj`, `nowe_stats`.
Z wykluczeń spreadem podmieniony jest wyłącznie POMIAR (`wykluczenia_spreadowe`
czyta bazę); reguła progu `za_szeroki_spread` wykonuje się naprawdę.

Zegar jest zamrożony (`loop.datetime`), bo `decyzja_przeterminowana` zależy od
odstępu między zamknięciem świecy a chwilą decyzji — bez tego wynik testu
zależałby od godziny, o której ktoś go uruchomił.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from services.engine import gielda_exec, likwidacja, loop
from services.engine.horizons import get as get_horizon
from services.engine.risk_rules import (DEFAULT_LIMITS, RiskVerdict,
                                        position_budget, wielkosc_pozycji)
from services.engine.sesja import StanSesji
from services.engine.uniwersum import za_szeroki_spread
from tests.atrapa_bazy import Kursor

H = get_horizon("4h")
# 08:05 UTC: świeca decyzyjna 04:00-08:00 zamknęła się 5 minut temu.
TERAZ = datetime(2026, 10, 3, 8, 5, tzinfo=timezone.utc)
SWIECA = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)
KAPITAL = D("10000")
CENA = D("2.5")

INSTRUMENTY = [
    {"id": 1, "symbol": "ADAUSDC", "kind": "spot", "quote_ccy": "USDC"},
    {"id": 2, "symbol": "XRPUSDC", "kind": "spot", "quote_ccy": "USDC"},
]


class Polaczenie:
    def __init__(self, cur):
        self.cur, self.commity = cur, 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commity += 1

    def rollback(self):
        pass


class Swiat:
    """Stan, który `run_once` czyta z bazy — i ślad tego, co z nim zrobił."""

    def __init__(self):
        self.wyniki: dict[int, float] = {1: 0.05, 2: 0.05}   # słaby sygnał
        self.pozycje: dict[int, dict] = {}
        self.spready: dict[int, tuple[float, int]] = {}
        # Powód odrzucenia pomiaru spreadu bramką spójności (None = przyjęty).
        self.spread_odrzucony: str | None = None
        # Mnożnik ilości z gałęzi `downsize` kernela (None = `allow`).
        self.przyciecie: D | None = None
        self.killswitch: dict | None = None
        self.teraz = TERAZ
        self.decyzje: list[dict] = []
        self.wywolania: dict[str, list[dict]] = {}
        self.sprawdzone_konto = 0

    def zanotuj(self, nazwa, **kw):
        self.wywolania.setdefault(nazwa, []).append(kw)
        return kw


@pytest.fixture
def swiat(monkeypatch):
    s = Swiat()

    class Zegar(datetime):
        @classmethod
        def now(cls, tz=None):
            return s.teraz

    class Ryzyko:
        def __init__(self, conn, engine, mode="demo"):
            self.limits = DEFAULT_LIMITS

        def check_account(self):
            s.sprawdzone_konto += 1
            if s.killswitch:
                return RiskVerdict(verdict="block",
                                   reason=f"kill-switch aktywny: {s.killswitch['reason']}")
            return RiskVerdict(verdict="allow")

        def kapital(self):
            return KAPITAL

        def killswitch_active(self):
            return s.killswitch

        def check(self, iid, notional, qty):
            if s.przyciecie is not None:
                return RiskVerdict(verdict="downsize", reason="limit pozycji",
                                   qty=(qty * s.przyciecie).quantize(D("0.000001")))
            return RiskVerdict(verdict="allow", qty=qty)

        def record_usage(self, usage):
            pass

        def record_violations(self, violations, did):
            pass

    def decyzja(cur, engine, mode, iid, now, action, qty, *reszta, bar_start,
                strategia=None):
        s.decyzje.append({"iid": iid, "action": action, "qty": qty,
                          "verdict": reszta[4].verdict, "powod_holdu": reszta[11],
                          "strategia": strategia})
        return len(s.decyzje)

    def ocena(features, regime, weights, h):
        wynik = s.wyniki[features["iid"]]
        return wynik, 0.8, [{"name": "momentum", "label": "Momentum",
                             "contribution": wynik}]

    m = monkeypatch.setattr
    m(loop, "datetime", Zegar)
    m(loop, "RiskKernel", Ryzyko)
    m(loop, "ensure_signal_defs", lambda cur, engine: {})
    m(loop, "seed_equity", lambda cur, engine, mode: None)
    m(loop, "pozycje_do_wyceny", lambda cur, engine, mode, as_of: [])
    m(loop, "wykluczenia_spreadowe", lambda cur, engine, ids, h, teraz: (
        ({}, s.spread_odrzucony) if s.spread_odrzucony
        else (za_szeroki_spread(s.spready), None)))
    m(loop, "_SPREAD_BEZ_WYKLUCZEN", {})
    m(loop, "active_model", lambda cur, engine, h, status="active", mode=None: {
        "weights": None, "enter": loop.ENTER_THRESHOLD, "exit": loop.EXIT_THRESHOLD,
        "bramki": loop.bramki_z_modelu(None), "min_conf": loop.MIN_CONFIDENCE,
        "version": None})
    m(loop, "horizon_feature_ids", lambda cur, h, symbole=None: [])
    m(loop, "decided_for_bar", lambda cur, engine, mode, iid, bar_start, h: False)
    m(loop, "latest_features", lambda cur, iid, now, h, ids, bar_start: (
        {"iid": iid, "rsi": 50.0, "ema_ratio": 1.0, "atr_pct": 1.0, "ret_mid": 0.0},
        {}))
    m(loop, "zakres_swiecy", lambda cur, iid, h, bar_start: {"close": CENA})
    m(loop, "atr_percentile_80", lambda cur, iid, now, h: None)
    m(loop, "classify_regime", lambda features, atr: ("range", 0.7))
    m(loop, "evaluate", ocena)
    m(loop, "pokrycie_wag", lambda nazwy, regime, weights: 1.0)
    m(loop, "open_position", lambda cur, engine, mode, iid: s.pozycje.get(iid))
    m(loop, "record_decision", decyzja)
    m(loop, "update_equity", lambda cur, engine, mode, now: loop.Wycena())
    m(loop, "rozstrzygnij_plany",
      lambda cur, engine, mode, h, now, kind, verbose=True, tylko_wyjscia=False:
      s.zanotuj("rozstrzygnij_plany", tylko_wyjscia=tylko_wyjscia) and {})

    async def stan_sesji(cur, engine, gielda):
        return StanSesji(otwarta=True, zrodlo="test")

    m(loop, "stan_sesji", stan_sesji)

    async def anuluj(cur, adapter, **kw):
        s.zanotuj("anuluj_wejscia", **kw)
        return 0, 0

    async def likwiduj(cur, adapter, **kw):
        s.zanotuj("likwiduj_po_killswitchu", **kw)
        return {"zamkniete": 0}

    def gieldowa(nazwa):
        async def f(cur, adapter, **kw):
            s.zanotuj(nazwa, **kw)
            return {}
        return f

    m(gielda_exec, "anuluj_wejscia", anuluj)
    m(likwidacja, "likwiduj_po_killswitchu", likwiduj)
    for nazwa in ("rozstrzygnij_oczekujace", "posprzataj_zlecenia",
                  "ponow_nieznane", "zloz_oczekujace"):
        m(gielda_exec, nazwa, gieldowa(nazwa))
    return s


def cykl(swiat, instrumenty=INSTRUMENTY, engine="crypto", **kw):
    cur = Kursor([dict(i) for i in instrumenty])
    conn = Polaczenie(cur)
    kw.setdefault("limit", True)
    kw.setdefault("h", H)
    kw.setdefault("verbose", False)
    stats = asyncio.run(loop.run_once(conn, engine, "demo", **kw))
    return stats, cur


def puls(cur) -> str:
    (wpis,) = [p for s, p in cur.sql if "risk.engine_heartbeat" in s]
    return wpis[2]


def plany(cur):
    return [p for s, p in cur.sql if s.startswith("INSERT INTO journal.planned_order")]


POZYCJA = {"id": 7, "side": "long", "qty": D("40"), "avg_price": D("2.4")}


# ------------------------------------------------------------------ brak sygnałów

def test_brak_sygnalow_to_HOLD_z_powodem_i_puls_z_pelnym_ksztaltem(swiat):
    """Cykl, w którym nic nie przekroczyło progu: zero planów, każda decyzja
    z powodem `slaby_wynik`, puls niesie rozmiar uniwersum — „nic się nie
    działo" ma być odróżnialne od „nie było czego rozważać"."""
    stats, cur = cykl(swiat)

    assert [d["action"] for d in swiat.decyzje] == ["hold", "hold"]
    assert {d["powod_holdu"] for d in swiat.decyzje} == {"slaby_wynik"}
    assert plany(cur) == []
    assert stats["instrumenty"] == 2 and stats["rozwazone"] == 2
    assert stats["wejscia"] == stats["wyjscia"] == 0
    assert stats["wstrzymanie_killswitch_id"] is None
    assert set(loop.nowe_stats()) <= set(stats), "puls zgubił klucz licznika"
    assert '"instrumenty": 2' in puls(cur)
    # Ścieżka papierowa z limitem: plany z poprzedniej świecy rozstrzygają się
    # BEZ ograniczenia do wyjść, bo nic nie wstrzymało wejść.
    assert swiat.wywolania["rozstrzygnij_plany"] == [{"tylko_wyjscia": False}]


def test_mocny_sygnal_daje_plan_wejscia_o_wielkosci_z_reguly_wspolnej(swiat):
    """Wejście bez przeszkód — punkt odniesienia dla testów wstrzymania niżej.
    Wielkość planu MUSI być tą z `risk_rules.wielkosc_pozycji` (U-3): to ta sama
    funkcja, którą liczy symulacja walk-forward."""
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, cur = cykl(swiat)

    (plan,) = plany(cur)
    baza = position_budget(DEFAULT_LIMITS, len(INSTRUMENTY), KAPITAL)
    _, qty = wielkosc_pozycji(baza, 0.6, CENA)
    assert plan[1] == 1 and plan[2] == "buy" and plan[3] == qty
    assert stats["wejscia"] == 1


# ------------------------------------------------------------------ kill-switch

def test_KILLSWITCH_wstrzymuje_wejscie_ale_wyjscie_idzie_dalej(swiat):
    """Najgroźniejsza możliwa pomyłka w tym miejscu: bezpiecznik, który blokuje
    też WYJŚCIA, zostawia system z otwartymi pozycjami w rynku uciekającym
    przeciwko niemu. Instrument 1 chce wejść — dostaje `hold` z powodem
    `wstrzymanie_ryzykiem` i werdyktem `block`. Instrument 2 trzyma pozycję
    przy sygnale odwróconym — dostaje plan WYJŚCIA mimo kill-switcha.

    Zastępuje tekstowy `test_killswitch.test_petla_wstrzymuje_WYLACZNIE_wejscia`
    i `test_plany_engine_mode.test_PETLA_podaje_stan_kill_switcha_sciezce_papierowej`.
    """
    swiat.killswitch = {"id": 23, "reason": "obsunięcie"}
    swiat.wyniki = {1: 0.6, 2: -0.4}
    swiat.pozycje = {2: POZYCJA}
    stats, cur = cykl(swiat)

    decyzje = {d["iid"]: d for d in swiat.decyzje}
    assert decyzje[1]["action"] == "hold"
    assert decyzje[1]["powod_holdu"] == "wstrzymanie_ryzykiem"
    assert decyzje[1]["verdict"] == "block"
    assert decyzje[2]["action"] == "exit"

    (plan,) = plany(cur)
    assert plan[1] == 2 and plan[2] == "sell" and plan[3] == POZYCJA["qty"]
    assert '"rodzaj": "wyjscie"' in plan[5]
    assert stats["wyjscia"] == 1 and stats["wejscia"] == 0
    assert stats["zablokowane"] == 1
    # Ogniwo 3 próby pożarowej (U-12) czyta to z pulsu.
    assert stats["wstrzymanie_killswitch_id"] == 23
    # Papierowy odpowiednik zdjęcia zleceń z arkusza: plany wejścia nie
    # rozstrzygają się po zadziałaniu bezpiecznika, plany wyjścia tak.
    assert swiat.wywolania["rozstrzygnij_plany"] == [{"tylko_wyjscia": True}]


def test_KILLSWITCH_na_gieldzie_zdejmuje_wejscia_i_WYSYLA_wyjscia(swiat):
    """Ścieżka giełdowa tego samego przypadku. Kill-switch:
      * zdejmuje z arkusza zlecenia wejścia — WŁASNEGO silnika i trybu,
      * nie likwiduje pozycji, gdy przełącznik `likwiduj` jest wyłączony,
      * wysyłkę woła ZAWSZE, z `tylko_wyjscia=True` — gdyby stan bezpiecznika
        trafił do warunku wysyłki, plany wyjścia powstawałyby i nigdy nie szły
        na giełdę.

    Zastępuje tekstowe `test_gielda_exec.test_petla_NIE_bramkuje_wysylki_stanem_killswitcha`
    i `test_zgubione_wypelnienie.test_PETLA_podaje_SWOJ_silnik_i_tryb_a_nie_stale`.
    """
    swiat.killswitch = {"id": 23, "reason": "obsunięcie"}
    swiat.wyniki = {1: 0.6, 2: -0.4}
    swiat.pozycje = {2: POZYCJA}
    stats, cur = cykl(swiat, gielda=object())

    (anulowanie,) = swiat.wywolania["anuluj_wejscia"]
    assert (anulowanie["engine"], anulowanie["mode"]) == ("crypto", "demo")
    assert "kill-switch" in anulowanie["powod"]
    assert "likwiduj_po_killswitchu" not in swiat.wywolania
    assert stats["likwidacja"] is None

    (wysylka,) = swiat.wywolania["zloz_oczekujace"]
    assert wysylka["tylko_wyjscia"] is True
    assert wysylka["sesja"].wolno_decydowac
    assert swiat.wywolania["ponow_nieznane"] == [
        {"engine": "crypto", "mode": "demo", "kategoria": "spot",
         "verbose": False, "tylko_wyjscia": True}]
    assert "rozstrzygnij_plany" not in swiat.wywolania, (
        "przy giełdzie plany rozstrzyga giełda, nie świeca")
    assert [p[2] for p in plany(cur)] == ["sell"]


def test_bez_killswitcha_wysylka_obejmuje_wejscia(swiat):
    """Druga strona poprzedniego testu: `tylko_wyjscia` jest stanem bramki,
    a nie stałą — bez wstrzymania wysyłka idzie z wejściami."""
    swiat.wyniki = {1: 0.6, 2: 0.05}
    cykl(swiat, gielda=object())

    assert "anuluj_wejscia" not in swiat.wywolania
    assert swiat.wywolania["zloz_oczekujace"][0]["tylko_wyjscia"] is False


def test_KILLSWITCH_zapisany_W_TRAKCIE_cyklu_wstrzymuje_wysylke_wejsc(swiat, monkeypatch):
    """T-2 (przegląd 2026-10-04): dozorca albo panel zapisuje kill-switch po
    `check_account`, a przed wysyłką. Decyzja wejścia już zapadła (zostaje
    w dzienniku), ale na giełdę idą wyłącznie wyjścia."""
    swiat.wyniki = {1: 0.6, 2: 0.05}

    async def stan_sesji(cur, engine, gielda):
        # Odczyt sesji przed wysyłką = chwila między decyzją a wysyłką.
        swiat.killswitch = {"id": 31, "reason": "ręcznie z panelu"}
        return StanSesji(otwarta=True, zrodlo="test")

    monkeypatch.setattr(loop, "stan_sesji", stan_sesji)
    stats, cur = cykl(swiat, gielda=object(), sesja=StanSesji(otwarta=True, zrodlo="test"))

    assert swiat.sprawdzone_konto == 1
    assert [d["action"] for d in swiat.decyzje if d["iid"] == 1] == ["enter_long"]
    assert stats["wstrzymanie_killswitch_id"] is None
    (wysylka,) = swiat.wywolania["zloz_oczekujace"]
    assert wysylka["tylko_wyjscia"] is True
    assert swiat.wywolania["ponow_nieznane"][0]["tylko_wyjscia"] is True


def test_likwidacja_po_killswitchu_tylko_za_przelacznikiem(swiat):
    swiat.killswitch = {"id": 23, "reason": "obsunięcie"}
    stats, _ = cykl(swiat, gielda=object(), likwiduj=True)

    (wywolanie,) = swiat.wywolania["likwiduj_po_killswitchu"]
    assert (wywolanie["engine"], wywolanie["mode"]) == ("crypto", "demo")
    assert stats["likwidacja"] == {"zamkniete": 0}


def test_PULS_mowi_czy_kill_switch_ZAMKNIE_pozycje_rynkowo(swiat):
    """Audyt 2026-10-10, S2-3: panel mówił „zatrzyma otwieranie pozycji", a przy
    `--likwiduj-po-killswitchu` ręczne zatrzymanie sprzedaje całą ekspozycję
    zleceniem rynkowym. API czyta to ustawienie z pulsu - puls musi je nieść,
    i to w postaci, która faktycznie zadziała (flaga bez giełdy nie zamyka nic).
    """
    stats, cur = cykl(swiat, gielda=object(), likwiduj=True)
    assert stats["likwiduj_po_killswitchu"] is True
    assert '"likwiduj_po_killswitchu": true' in puls(cur)

    stats, _ = cykl(swiat, gielda=object())
    assert stats["likwiduj_po_killswitchu"] is False
    stats, _ = cykl(swiat, likwiduj=True)          # bez giełdy: nie ma czym zamknąć
    assert stats["likwiduj_po_killswitchu"] is False


# ------------------------------------------------------------------ bramki cyklu

def test_sesja_zamknieta_konczy_cykl_PULSEM_po_pomiarze_ryzyka(swiat):
    """Poza sesją: żadnej decyzji, żadnego kontaktu z giełdą, ale puls bije
    (inaczej czujka żywotności uznałaby każdą noc equity za śmierć silnika)
    i limity konta ZOSTAŁY zmierzone — podłoga kapitału obowiązuje też w nocy.

    Zastępuje tekstowe `test_silnik_gieldy_petla.test_pomiar_ryzyka_dzieje_sie_PRZED_bramkami`
    i część `test_run_once_oddaje_powod_blokady_a_nie_same_zera`.
    """
    swiat.wyniki = {1: 0.6, 2: 0.6}
    sesja = StanSesji(otwarta=False, zrodlo="zegar gieldy")
    stats, cur = cykl(swiat, gielda=object(), sesja=sesja)

    assert stats["poza_sesja"] is True and stats["sesja_znana"] is True
    assert swiat.sprawdzone_konto == 1, "pomiar ryzyka nie wykonał się przed bramką"
    assert swiat.decyzje == [] and plany(cur) == []
    assert swiat.wywolania == {}, "poza sesją pętla dotknęła giełdy"
    assert '"poza_sesja": true' in puls(cur)
    pelny = loop.nowe_stats()
    # `likwiduj_po_killswitchu` to ustawienie procesu, nie licznik - puls
    # niesie je także poza sesją (panel czyta je do opisu kill-switcha).
    assert all(stats[k] == pelny[k] for k in pelny
               if k != "likwiduj_po_killswitchu"), "bramka zmyśliła licznik"
    assert stats["likwiduj_po_killswitchu"] is False


def test_sesja_czytana_PONOWNIE_przed_wysylka_i_ten_obiekt_idzie_do_zloz(swiat,
                                                                         monkeypatch):
    """Stan sesji z początku cyklu nie opisuje jego końca: cykl wystartowany
    15:59 czasu Nowego Jorku przechodzi bramkę jako „otwarta", a wysyłka stoi
    po 16:00. `zloz_oczekujace` ma dostać ŚWIEŻY odczyt — i to obiekt, nie
    `None`, bo `zloz_plan` pomija bramkę przy `sesja is None`.

    Zastępuje tekstowy `test_prog_wieku_arkusza_a_sesja.test_run_once_pyta_o_sesje_PONOWNIE_przed_wysylka`.
    """
    koniec = StanSesji(otwarta=False, zrodlo="zegar gieldy")

    async def stan_sesji(cur, engine, gielda):
        return koniec

    monkeypatch.setattr(loop, "stan_sesji", stan_sesji)
    cykl(swiat, gielda=object(), sesja=StanSesji(otwarta=True, zrodlo="zegar gieldy"))

    (wysylka,) = swiat.wywolania["zloz_oczekujace"]
    assert wysylka["sesja"] is koniec


def test_stan_sesji_NIEZNANY_tez_nie_jest_otwarciem(swiat):
    stats, _ = cykl(swiat, sesja=StanSesji(otwarta=None, zrodlo="zegar", blad="timeout"))

    assert stats["poza_sesja"] is True and stats["sesja_znana"] is False
    assert swiat.decyzje == []


def test_brak_modelu_przy_wymaganym_modelu_konczy_cykl_pulsem(swiat):
    stats, cur = cykl(swiat, wymagaj_modelu=True)

    assert stats["bez_modelu"] is True
    assert stats["model_nazwa"] == "crypto_signal_weighting_4h"
    assert swiat.decyzje == []
    assert '"bez_modelu": true' in puls(cur)


# ------------------------------------------------------------------ wejście odrzucone

def test_spozniony_potok_PRZETERMINOWUJE_wejscie_ale_nie_wyjscie(swiat):
    """T7 (F-4.4): 30 min po zamknięciu świecy wejście odpada z własnym powodem;
    wyjście z pozycji nadal zapada — spóźnione zdjęcie ryzyka to wciąż zdjęcie."""
    swiat.teraz = SWIECA + timedelta(hours=4, minutes=30)
    swiat.wyniki = {1: 0.6, 2: -0.4}
    swiat.pozycje = {2: POZYCJA}
    stats, cur = cykl(swiat)

    decyzje = {d["iid"]: d for d in swiat.decyzje}
    assert decyzje[1]["powod_holdu"] == "decyzja_przeterminowana"
    assert decyzje[2]["action"] == "exit"
    assert stats["decyzja_przeterminowana"] == 1
    assert [p[2] for p in plany(cur)] == ["sell"]


def test_szeroki_spread_blokuje_WEJSCIE_a_nie_wyjscie(swiat):
    """F-1.5: instrument o średnim spreadzie ponad próg nie dostaje nowej
    pozycji, ale z istniejącej wychodzi normalnie.

    Zastępuje tekstowy `test_wykluczenie_szerokiego_spreadu.test_petla_blokuje_WEJSCIE_a_nie_wyjscie`.
    """
    swiat.spready = {1: (21.0, 40), 2: (30.0, 40)}
    swiat.wyniki = {1: 0.6, 2: -0.4}
    swiat.pozycje = {2: POZYCJA}
    stats, cur = cykl(swiat)

    decyzje = {d["iid"]: d for d in swiat.decyzje}
    assert decyzje[1]["action"] == "hold"
    assert decyzje[1]["powod_holdu"] == "szeroki_spread"
    assert decyzje[2]["action"] == "exit"
    assert stats["szeroki_spread"] == 1
    assert [p[2] for p in plany(cur)] == ["sell"]


def test_short_na_spocie_jest_niewykonalny(swiat):
    swiat.wyniki = {1: -0.6, 2: 0.05}
    stats, cur = cykl(swiat)

    assert swiat.decyzje[0]["powod_holdu"] == "short_niewykonalny"
    assert stats["short_niewykonalny"] == 1 and plany(cur) == []


def test_brak_cech_zostawia_SLAD_pominiecia(swiat, monkeypatch):
    """Instrument bez kompletu cech nie znika bezgłośnie: licznik w pulsie
    i wiersz w `journal.pominiecie`."""
    monkeypatch.setattr(loop, "latest_features",
                        lambda cur, iid, now, h, ids, bar_start: ({}, {}))
    stats, cur = cykl(swiat)

    assert stats["pominiete_bez_cech"] == 2 and stats["rozwazone"] == 0
    assert sum("journal.pominiecie" in s for s, _ in cur.sql) == 2


# ------------------------------------------------------------------ spread: bramka spójności

def test_ODRZUCONY_pomiar_spreadu_nie_wyklucza_i_mowi_o_tym_RAZ(swiat, capsys):
    """Equity: `book_spread_bps` z feedu IEX średnio 278 pb, ale bramka spójności
    (`spread.zmierz_spread`) odrzuca ten pomiar - trener go nie używa, więc
    pętla też nie może nim wykluczać. Powód idzie do logu przy PRZEJŚCIU,
    nie co cykl (pętla chodzi co 300 s)."""
    swiat.spready = {1: (278.0, 40), 2: (35.0, 40)}
    swiat.spread_odrzucony = "wykluczenia spreadem WYŁĄCZONE — pomiar odrzucony"
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, cur = cykl(swiat)

    assert stats["szeroki_spread"] == 0
    assert [p[2] for p in plany(cur)] == ["buy"]
    assert capsys.readouterr().out.count("WYŁĄCZONE") == 1

    cykl(swiat)
    assert "WYŁĄCZONE" not in capsys.readouterr().out, "powód wypisany co cykl"


# ------------------------------------------------------------------ kwota poniżej jednej akcji

AKCJE = [
    {"id": 1, "symbol": "LLY", "kind": "stock", "quote_ccy": "USD", "lot_size": D("1")},
    {"id": 2, "symbol": "KO", "kind": "stock", "quote_ccy": "USD", "lot_size": D("1")},
]


def test_kwota_ponizej_JEDNEJ_AKCJI_to_hold_z_powodem_przy_decyzji(swiat, monkeypatch):
    """Budżet na pozycję niższy niż cena jednej akcji: plan z ilością 0,13 akcji
    odpadał dopiero przy wysyłce (`zlecenia.sprawdz_zlecenie`), a dziennik
    decyzji mówił „enter". Teraz `hold` z powodem już przy decyzji, a tańsza
    spółka (ilość >= 1) wchodzi normalnie."""
    ceny = {1: D("5000"), 2: D("60")}
    monkeypatch.setattr(loop, "zakres_swiecy",
                        lambda cur, iid, h, bar_start: {"close": ceny[iid]})
    swiat.wyniki = {1: 0.6, 2: 0.6}
    stats, cur = cykl(swiat, instrumenty=AKCJE)

    decyzje = {d["iid"]: d for d in swiat.decyzje}
    assert decyzje[1]["action"] == "hold"
    assert decyzje[1]["powod_holdu"] == "ponizej_jednej_akcji"
    assert decyzje[2]["action"] == "enter_long"
    assert stats["ponizej_jednej_akcji"] == 1 and stats["wejscia"] == 1
    assert [p[1] for p in plany(cur)] == [2]


def test_krok_ulamkowy_krypto_NIE_zmienia_wejscia(swiat):
    """Kanarek: krypto z krokiem 0,1 i ilością ~150 wchodzi jak wcześniej,
    a instrument bez reguły kroku (`lot_size` NULL) nie dostaje tu odmowy -
    o nim rozstrzyga wysyłka."""
    swiat.wyniki = {1: 0.6, 2: 0.6}
    stats, cur = cykl(swiat, instrumenty=[
        {**INSTRUMENTY[0], "lot_size": D("0.1")}, {**INSTRUMENTY[1], "lot_size": None}])

    assert stats["ponizej_jednej_akcji"] == 0 and stats["wejscia"] == 2


def test_selekcja_uniwersum_POBIERA_krok_ilosci(swiat):
    """Atrapa oddaje cały wiersz, więc test wyżej przeszedłby także wtedy, gdy
    zapytanie nie pobiera `lot_size` - a na produkcji reguła by milczała."""
    _, cur = cykl(swiat)
    (sql,) = [q for q, _ in cur.sql if "FROM ref.instrument" in q and "tradeable" in q]
    assert "lot_size" in sql.split("FROM")[0]


# ------------------------------------------------------------------ świeca sprzed sesji

H1 = get_horizon("1h")
SESJA_ALPACA = StanSesji(otwarta=True, zrodlo="zegar giełdy", venue="alpaca")


def test_swieca_SPRZED_SESJI_ma_wlasny_powod_a_nie_przeterminowanie(swiat):
    """Piątek 16.10.2026, czas letni: sesja od 13:30 UTC. O 13:33 zamknięta jest
    świeca 12:00-13:00 - notowanie sprzed sesji, rozstrzygane 33 min po
    zamknięciu. Kończyło się jako `decyzja_przeterminowana`, licznik spóźnienia
    POTOKU. Wyjście z pozycji idzie dalej."""
    swiat.teraz = datetime(2026, 10, 16, 13, 33, tzinfo=timezone.utc)
    swiat.wyniki = {1: 0.6, 2: -0.4}
    swiat.pozycje = {2: POZYCJA}
    stats, cur = cykl(swiat, engine="equity", h=H1, sesja=SESJA_ALPACA)

    decyzje = {d["iid"]: d for d in swiat.decyzje}
    assert decyzje[1]["powod_holdu"] == "swieca_przed_sesja"
    assert decyzje[2]["action"] == "exit"
    assert stats["swieca_przed_sesja"] == 1
    assert stats["decyzja_przeterminowana"] == 0


def test_pierwsza_swieca_W_SESJI_decyduje_normalnie(swiat):
    """Kanarek: 14:05 - świeca 13:00-14:00 zawiera otwarcie, wejście przechodzi."""
    swiat.teraz = datetime(2026, 10, 16, 14, 5, tzinfo=timezone.utc)
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, cur = cykl(swiat, engine="equity", h=H1, sesja=SESJA_ALPACA)

    assert stats["swieca_przed_sesja"] == 0 and stats["wejscia"] == 1


# ------------------------------------------------------------------ log cyklu

def test_verbose_wypisuje_powod_holdu_TAKZE_dla_strategii_model(swiat, capsys):
    swiat.wyniki = {1: -0.6, 2: 0.05}
    cykl(swiat, verbose=True)

    out = capsys.readouterr().out
    assert "(short_niewykonalny)" in out and "(slaby_wynik)" in out


def test_podsumowanie_liczy_zatrzymane_wejscia():
    s = loop.nowe_stats(szeroki_spread=3, short_niewykonalny=1,
                        decyzja_przeterminowana=2, ponizej_jednej_akcji=4,
                        swieca_przed_sesja=5)
    linia = loop.opis_zatrzymanych_wejsc(s)
    for k, n in (("szeroki_spread", 3), ("short_niewykonalny", 1),
                 ("decyzja_przeterminowana", 2), ("ponizej_jednej_akcji", 4),
                 ("swieca_przed_sesja", 5)):
        assert f"{k} {n}" in linia
    assert loop.opis_zatrzymanych_wejsc(loop.nowe_stats()) == ""


def test_komunikat_spoznionego_potoku_podaje_LIMIT_WEJSC_i_deployment_silnika(
        swiat, monkeypatch, capsys):
    """Komunikat mówił „po {długość świecy} min silnik NIE HANDLUJE" i kazał
    patrzeć w `trader-features` także silnikowi equity - a wejścia odpadają po
    `MAKS_ZWLOKA_WEJSCIA`, cechy equity liczy `trader-features-equity`."""
    monkeypatch.setattr(loop, "latest_features", lambda cur, iid, now, h, ids, bar_start: (
        {}, {"rsi": datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)}))
    swiat.teraz = datetime(2026, 10, 16, 15, 5, tzinfo=timezone.utc)
    cykl(swiat, engine="equity", h=H1, sesja=SESJA_ALPACA)

    out = capsys.readouterr().out
    assert "deploy/trader-features-equity" in out
    assert f"{loop.MAKS_ZWLOKA_WEJSCIA.total_seconds() / 60:.0f} min" in out


def test_DOWNSIZE_kernela_ponizej_jednej_akcji_to_ten_sam_hold(swiat, monkeypatch):
    """Przed kernelem ilość >= 1 akcji, ale `downsize` (limit wielkości pozycji)
    obcina ją poniżej kroku - plan odpadłby przy wysyłce bez powodu."""
    monkeypatch.setattr(loop, "zakres_swiecy",
                        lambda cur, iid, h, bar_start: {"close": D("60")})
    swiat.wyniki = {1: 0.6, 2: 0.05}
    swiat.przyciecie = D("0.01")
    stats, cur = cykl(swiat, instrumenty=AKCJE)

    decyzje = {d["iid"]: d for d in swiat.decyzje}
    assert decyzje[1]["action"] == "hold"
    assert decyzje[1]["powod_holdu"] == "ponizej_jednej_akcji"
    assert stats["ponizej_jednej_akcji"] == 1 and plany(cur) == []


def test_DOWNSIZE_powyzej_kroku_nadal_wchodzi(swiat, monkeypatch):
    """Kanarek: obcięcie, po którym zostaje >= 1 akcja, wchodzi obciętą ilością."""
    monkeypatch.setattr(loop, "zakres_swiecy",
                        lambda cur, iid, h, bar_start: {"close": D("60")})
    swiat.wyniki = {1: 0.6, 2: 0.05}
    swiat.przyciecie = D("0.5")
    stats, cur = cykl(swiat, instrumenty=AKCJE)

    assert stats["ponizej_jednej_akcji"] == 0 and stats["wejscia"] == 1
