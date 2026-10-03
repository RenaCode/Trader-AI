"""Uzgodnienie nie alarmuje o wypełnieniu, którego jeszcze nie zaksięgowaliśmy (audyt 3 T3).

Księgujemy wypełnienie przy rozstrzyganiu planu, czyli w najbliższym cyklu
silnika PO wypełnieniu. Do tej chwili `exec.order.status` stoi na `new`,
a uzgodnienie widzi saldo bez naszej pozycji. Zmierzone na produkcji:

    NEAR 01.10: alarm 07:20:51, zaksięgowane 07:21:27 (zlecenie 81, 277,84 kupna)
    BTC  02.10: alarm 05:30:52, zaksięgowane 05:33:02 (zlecenie 83)

Obok: alarm szedł kodem `killswitch.triggered`, a `exec.fill.event_time` był
chwilą księgowania, nie wykonania.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from services.engine import uzgodnienie
from services.engine.uzgodnienie import uzgodnij
from services.exchanges.base import Balance

KORZEN = Path(__file__).resolve().parents[1]


def _rozjazd(aktywo, u_nas, u_gieldy):
    u_nas, u_gieldy = D(u_nas), D(u_gieldy)
    return {"aktywo": aktywo, "u_nas": u_nas, "u_gieldy": u_gieldy,
            "roznica": u_gieldy - u_nas, "wzgledna": D(1),
            "rodzaj": "pozycja-widmo" if u_nas > u_gieldy else "saldo-nieznane"}


KUPNO_NEAR = {"aktywo": "NEAR", "strona": "buy", "ilosc": D("277.84")}


def test_kupno_w_locie_wyjasnia_saldo_nieznane():
    prawdziwe, lot = uzgodnienie.w_locie([_rozjazd("NEAR", "0", "277.57094")],
                                         [KUPNO_NEAR])
    assert prawdziwe == [] and len(lot) == 1


def test_kupno_w_locie_NIE_wyjasnia_pozycji_widma():
    """Kupno nie może obniżyć salda - brak aktywa przy otwartym kupnie to alarm."""
    prawdziwe, lot = uzgodnienie.w_locie([_rozjazd("NEAR", "277", "0")], [KUPNO_NEAR])
    assert len(prawdziwe) == 1 and lot == []


def test_roznica_wieksza_niz_zlecenie_jest_alarmem():
    prawdziwe, _ = uzgodnienie.w_locie([_rozjazd("NEAR", "0", "600")], [KUPNO_NEAR])
    assert len(prawdziwe) == 1


def test_zlecenie_na_innym_aktywie_niczego_nie_wyjasnia():
    prawdziwe, _ = uzgodnienie.w_locie(
        [_rozjazd("BTC", "0", "0.015")], [KUPNO_NEAR])
    assert len(prawdziwe) == 1


def test_sprzedaz_w_locie_wyjasnia_pozycje_widmo():
    prawdziwe, lot = uzgodnienie.w_locie(
        [_rozjazd("NEAR", "277.56", "0")],
        [{"aktywo": "NEAR", "strona": "sell", "ilosc": D("277.56")}])
    assert prawdziwe == [] and len(lot) == 1


class _Gielda:
    def __init__(self, salda):
        self.salda = salda

    async def fetch_balances(self):
        return [Balance(currency=k, total=v, available=v) for k, v in self.salda.items()]


def _fetch(w_locie):
    async def fetch_all(sql, params=None):
        if "AS strona" in sql:
            return list(w_locie)
        if "AS prowizja_w" in sql:
            return []
        if "sum(p.qty)" in sql:
            return []
        if "min(i.lot_size)" in sql:
            return [{"aktywo": "NEAR", "lot": D("0.01"), "min_wartosc": D("5"),
                     "cena": D("2.5")}]
        if "DISTINCT base_ccy" in sql:
            return [{"base_ccy": "NEAR"}]
        if 'app."user"' in sql:
            return [{"id": 1}]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")
    return fetch_all


def test_produkcyjny_przypadek_NEAR_nie_wysyla_alarmu_krytycznego():
    zgloszone = []

    async def notify(*a, **k):
        zgloszone.append(a)

    n = asyncio.run(uzgodnij(_fetch([KUPNO_NEAR]), notify,
                             _Gielda({"NEAR": D("277.57094")})))
    assert n == 0 and zgloszone == [], (
        "wypełnienie czekające na rozstrzygnięcie planu zgłoszone jako "
        "rozjazd krytyczny")


def test_prawdziwy_rozjazd_idzie_wlasnym_kodem_a_nie_kill_switchem():
    zgloszone = []

    async def notify(uid, kod, *a, **k):
        zgloszone.append(kod)

    n = asyncio.run(uzgodnij(_fetch([]), notify, _Gielda({"NEAR": D("277.57094")})))
    assert n == 1
    assert zgloszone == ["reconcile.mismatch"]
    migracje = "".join(p.read_text(encoding="utf-8")
                       for p in (KORZEN / "db" / "migrations").glob("*.sql"))
    assert "'reconcile.mismatch'" in migracje, "kod zdarzenia bez wiersza w notify.event_type"


def test_zlecenie_w_locie_ma_termin():
    """`new` po `valid_until` + dwa cykle to zawieszone rozstrzygnięcie, nie lot."""
    zapytania = []

    async def fetch_all(sql, params=None):
        zapytania.append((sql, params))
        return []
    asyncio.run(uzgodnienie.zlecenia_w_locie(fetch_all, "crypto", "demo"))
    sql, params = zapytania[0]
    assert "valid_until" in sql and uzgodnienie.KARENCJA_PO_WAZNOSCI_S in params


# ------------------------------------------------------------ czas wypełnienia

def test_czas_wypelnienia_z_zegara_gieldy_dla_pelnego():
    from services.engine.gielda_exec import czas_wykonania
    t = datetime(2026, 10, 1, 7, 2, 11, tzinfo=timezone.utc)
    stan = SimpleNamespace(status="filled",
                           zaktualizowano_ms=int(t.timestamp() * 1000))
    assert czas_wykonania(stan) == t
    assert czas_wykonania(SimpleNamespace(status="partial",
                                          zaktualizowano_ms=1)) is None
    assert czas_wykonania(SimpleNamespace(status="filled")) is None


class _Kursor:
    def __init__(self):
        self.zapytania = []

    def execute(self, sql, params=None):
        self.zapytania.append((" ".join(sql.split()), params))

    def fetchone(self):
        return {"id": 7}


def test_exec_fill_dostaje_czas_gieldy_a_nie_chwile_ksiegowania():
    from services.engine.loop import zapisz_wejscie
    ksiegowanie = datetime(2026, 10, 1, 7, 21, 27, tzinfo=timezone.utc)
    gielda = ksiegowanie - timedelta(minutes=19)
    cur = _Kursor()
    zapisz_wejscie(cur, "crypto", "demo", 1, 2, "buy", D("277.84"), D("2.5"),
                   D("0.1"), ksiegowanie, "USDC", {}, order_id=81,
                   czas_wykonania=gielda)
    fill = [p for sql, p in cur.zapytania if sql.startswith("INSERT INTO exec.fill")]
    assert fill and fill[0][-1] == gielda
    pozycja = [p for sql, p in cur.zapytania if sql.startswith("INSERT INTO exec.position")]
    assert ksiegowanie in pozycja[0], "pozycja zostaje na czasie księgowania"


def test_czas_gieldy_z_przyszlosci_nie_wyprzedza_ksiegowania():
    from services.engine.loop import _czas_wypelnienia
    teraz = datetime(2026, 10, 1, 7, 21, tzinfo=timezone.utc)
    assert _czas_wypelnienia(teraz + timedelta(seconds=3), teraz) == teraz
    assert _czas_wypelnienia(None, teraz) == teraz
