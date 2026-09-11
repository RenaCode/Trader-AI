"""Kanarki rachunku kapitału: `cash` i ślad prowizji wyjścia.

Kapitał jest liczbą, od której zależy wszystko inne — progi bezpiecznika są
jej centylami, wielkość pozycji jej ułamkiem. Błąd w niej nie objawia się
awarią: liczba po prostu nie znaczy tego, co mówi jej nazwa.
"""
from decimal import Decimal
from datetime import datetime, timezone


class Kursor:
    """Atrapa odpowiadająca na zapytania `update_equity` po fragmencie SQL.

    NIE jest emulatorem bazy. Zna dokładnie te cztery zapytania, które ta
    funkcja wykonuje, i zapamiętuje instrukcję zapisu — bo o jej parametry
    tu chodzi.
    """

    def __init__(self, *, realized, pozycje, cena, wplaty, units):
        self.realized, self.pozycje = realized, pozycje
        self.cena, self.wplaty, self.units = cena, wplaty, units
        self.sql: list[tuple[str, tuple]] = []
        self._odp = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "AS realized FROM exec.position" in sql:
            self._odp = {"realized": self.realized}
        elif "FROM exec.position p JOIN ref.instrument" in sql:
            self._odp = list(self.pozycje)
        elif "FROM market.candle" in sql:
            self._odp = {"close": self.cena, "wiek_min": 0.0}
        elif "FROM exec.cash_flow" in sql:
            self._odp = {"wplaty": self.wplaty, "units": self.units}
        else:
            self._odp = None

    def fetchone(self):
        return self._odp if isinstance(self._odp, dict) else None

    def fetchall(self):
        return self._odp if isinstance(self._odp, list) else []

    def zapis_kapitalu(self) -> dict:
        for sql, params in self.sql:
            if "INSERT INTO exec.equity_point" in sql:
                kolumny = ("engine", "mode", "event_time", "equity", "cash",
                           "unrealized", "realized_cum", "exposure",
                           "nav", "units", "wplaty_netto")
                return dict(zip(kolumny, params))
        raise AssertionError("update_equity nie zapisał punktu kapitału")


def test_krotka_sprzedaz_NIE_zabiera_gotowki():
    """`cash` zapisywane jest jako `equity - exposure` — czyli jako gotówka
    MINUS wartość rynkowa pozycji. Dla pozycji długiej to prawda: kupno
    zamieniło gotówkę na aktywo. Dla krótkiej jest odwrotnie — sprzedaż
    DODAŁA gotówkę na rachunek, a aktywo stało się zobowiązaniem.

    Ten sam wzór odejmuje więc od gotówki kwotę, którą sprzedaż na nią wpłaciła:
    pomyłka o dwie ekspozycje. W dzienniku produkcyjnym 8 z 18 pozycji to były
    krótkie sprzedaże (exec.position id 5–12), więc liczba pokazana wtedy
    właścicielowi jako „gotówka" była zaniżona o 2 x ekspozycję.
    """
    from services.engine.loop import update_equity

    pozycja = {"id": 1, "instrument_id": 7, "side": "short",
               "qty": Decimal("100"), "avg_price": Decimal("10"),
               "symbol": "TESTUSDT"}
    cur = Kursor(realized=Decimal("0"), pozycje=[pozycja], cena=Decimal("10"),
                 wplaty=Decimal("100000"), units=Decimal("100000"))
    update_equity(cur, "crypto", "demo", datetime.now(timezone.utc))
    w = cur.zapis_kapitalu()

    assert w["exposure"] == Decimal("1000"), "ekspozycja krótkiej to 100 x 10"
    assert w["equity"] == Decimal("100000"), "cena się nie ruszyła — kapitał bez zmian"
    assert w["cash"] >= w["equity"], (
        f"krótka sprzedaż wpłaciła 1000 na rachunek, a `cash` wyszło "
        f"{w['cash']} przy kapitale {w['equity']} — wzór `equity - exposure` "
        f"odjął to, co sprzedaż dodała")


def test_prowizja_wyjscia_zostawia_slad_w_exec_fill():
    """`zapisz_wejscie` dopisuje wiersz do `exec.fill`, `zapisz_wyjscie` nie.

    Skutek zmierzony na produkcji 2026-09-09: 18 zamkniętych pozycji, 18 wierszy
    w `exec.fill` (same wejścia) i 18 w `exec.order`. Prowizje wyjść — 9,33 USDC
    z 17,44, czyli 54% całego kosztu — nie istnieją w żadnym wierszu wypełnienia.
    Każdy pomiar budowany na `exec.fill` (jakość egzekucji, poślizg, uzgodnienie
    z giełdą) widzi więc połowę transakcji i nie ma jak się o tym dowiedzieć.
    """
    from services.engine.loop import zapisz_wyjscie

    class Prosty:
        """Atrapa, która zapamiętuje instrukcje i odpowiada po fragmencie SQL.

        Odpowiada na dokładnie dwa zapytania: skąd jest pozycja (`engine`,
        `mode`, `instrument_id`) i jaki identyfikator dostało nowe zlecenie.
        Na kodzie SPRZED naprawy `fetchone` nie jest wołane ani razu — wyjście
        nie czytało wtedy niczego — więc test pada na asercji, czyli na tym,
        co ma sprawdzać, a nie na braku metody w atrapie.
        """

        def __init__(self):
            self.sql = []
            self._odp = None

        def execute(self, sql, params=None):
            self.sql.append(sql)
            self._odp = ({"id": 1} if "RETURNING id" in sql
                         else {"engine": "crypto", "mode": "demo",
                               "instrument_id": 7, "side": "long",
                               "quote_ccy": "USDC"})

        def fetchone(self):
            return self._odp

    cur = Prosty()
    pozycja = {"id": 1, "qty": Decimal("10"), "avg_price": Decimal("100"),
               "side": "long"}
    zapisz_wyjscie(cur, pozycja, Decimal("110"), Decimal("1.1"),
                   datetime.now(timezone.utc), 42)

    assert any("INSERT INTO exec.fill" in s for s in cur.sql), (
        "wyjście nie zostawia wiersza w exec.fill — prowizja wyjścia trafia "
        "wyłącznie do exec.position.fees_paid i znika z dziennika wypełnień")
