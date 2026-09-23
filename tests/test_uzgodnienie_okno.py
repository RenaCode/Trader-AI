"""Okno rejestru wypełnień: skąd się zaczyna i względem czego mierzy pokrycie.

DLACZEGO TEN PLIK ISTNIEJE OSOBNO

`pozycje_z_wypelnien` sumuje KAŻDE wypełnienie, które giełda odda w oknie —
także należące do pozycji dawno zamkniętych. Okno decyduje więc nie tylko
o tym, ile historii widzimy, ale o tym, CZY ODTWORZENIE W OGÓLE MA SENS:
runda, której sprzedaż wpadła do okna, a kupno zostało przed nim, wnosi do
sumy gołą sprzedaż, czyli liczbę ujemną nie do odróżnienia od prawdziwie
urwanego rejestru.

Do 2026-09-23 okno liczyło się z `min(opened_at)` po pozycjach WYŁĄCZNIE
OTWARTYCH, minus stała jednej doby. Strażnik (`pokrycie_wypelnien`) mierzył
pokrycie względem tego samego, węższego zbioru — czyli nie miał jak zobaczyć
pozycji, która do sumy weszła, a do jego zapytania nie. Ślepa plamka była
strukturalna: obejmowała dokładnie ten kształt, dla którego strażnik powstał.

LICZBY SĄ Z PRODUKCJI (`exec.position`, `exec.order`, stan na 2026-09-23),
a nie ułożone pod tezę. W szczególności rozjazd 2 h 40 min między złożeniem
zlecenia (`submitted_at` 13:20:51) a zapisem pozycji (`opened_at` 16:00:35)
to zmierzone zlecenie 47 — ono właśnie jest tym, co stała jednej doby miała
zgadywać.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from services.engine.uzgodnienie import (
    Nieuzgadnialne,
    pokrycie_wypelnien,
    uzgodnij,
    zakres_wypelnien,
)
from services.exchanges.base import ExchangeError, Wypelnienie


def _t(dzien, godz=0, minuta=0):
    return datetime(2026, 9, dzien, godz, minuta, tzinfo=timezone.utc)


# ref.instrument, stan produkcyjny 2026-09-23.
REGULY = {
    "ETH": (D("0.0001"), D("5"), D("2716.26")),
    "SOL": (D("0.001"), D("5"), D("117.24")),
}


def _pozycja(od, opened_at, closed_at=None):
    """Wiersz w kształcie, w jakim oddaje go `kontekst_wypelnien`.

    `od` to chwila ZŁOŻENIA zlecenia otwierającego, `opened_at` — chwila,
    w której zapisaliśmy jego skutek. To są dwie różne liczby i cały ten plik
    stoi na tej różnicy.
    """
    return {"od": od, "opened_at": opened_at, "closed_at": closed_at}


def _wypelnienie(qty, strona, symbol, czas, prowizja="0", prowizja_w=None):
    return Wypelnienie(
        id_gieldy="x", id_wlasne=None, symbol=symbol, strona=strona,
        ilosc=D(qty), cena=D("2716.26"), prowizja=D(prowizja),
        prowizja_w=prowizja_w, czas=czas)


class _RejestrZeStartTime:
    """Host bez sald, którego rejestr NAPRAWDĘ honoruje `startTime`.

    Atrapa oddająca cały rejestr niezależnie od `od_ms` mierzyłaby wyłącznie
    samą siebie: okno przestałoby mieć jakikolwiek wpływ na wynik, więc test
    przechodziłby tak samo dla okna poprawnego i dla okna wyciętego w złym
    miejscu. Bybit tnie po `startTime` (`/v5/execution/list`) i ta atrapa też.

    `retencja` to najstarsze wypełnienie, jakie giełda W OGÓLE jeszcze trzyma —
    osobno od `startTime`, bo to są dwa różne ograniczenia i drugie z nich jest
    tym, przed którym ostrzega `pokrycie_wypelnien`.
    """

    def __init__(self, wypelnienia, retencja=None):
        self._wyp = wypelnienia
        self._retencja = retencja
        self.od_ms = "nie pytano"

    async def fetch_balances(self):
        raise ExchangeError("endpoint niedostepny na tym hoscie", code="404")

    async def wypelnienia(self, *, od_ms=None, symbol=None):
        self.od_ms = od_ms
        out = list(self._wyp)
        if self._retencja is not None:
            out = [w for w in out if w.czas >= self._retencja]
        if od_ms is not None:
            granica = datetime.fromtimestamp(od_ms / 1000, tz=timezone.utc)
            out = [w for w in out if w.czas >= granica]
        return out


def _fetch(aktywa, pozycje_dziennika=(), okno=()):
    async def fetch_all(sql, params=None):
        if "sum(p.qty)" in sql:
            return list(pozycje_dziennika)
        if "min(i.lot_size)" in sql:
            return [{"aktywo": a, "lot": REGULY[a][0],
                     "min_wartosc": REGULY[a][1], "cena": REGULY[a][2]}
                    for a in aktywa]
        if "DISTINCT base_ccy" in sql:
            return [{"base_ccy": a} for a in aktywa]
        if "LEAST(p.opened_at" in sql:
            return list(okno)
        if 'app."user"' in sql:
            return [{"id": 1}]
        return [{"symbol": f"{a}USDC", "base_ccy": a} for a in aktywa]
    return fetch_all


# ====================================== punkt staly: co nalezy do okna

def test_okno_COFA_SIE_po_pozycje_zamknieta_w_srodku_okna():
    """Sedno Z3. Pozycja otwarta PRZED oknem i zamknięta WEWNĄTRZ niego wnosi
    do odtworzenia gołą sprzedaż — więc okno musi sięgnąć po jej kupno.

    Punkt odniesienia liczony z pozycji wyłącznie otwartych dawał okno
    zaczynające się 2026-09-22, czyli już PO kupnie ETH z 2026-09-20.
    """
    okno = [_pozycja(_t(22, 13, 20), _t(22, 16, 0)),                 # SOL, otwarta
            _pozycja(_t(20, 13, 20), _t(20, 16, 0), _t(22, 18, 0))]  # ETH, zamknieta
    od, najstarsze = zakres_wypelnien(okno)
    assert od == _t(20, 13, 20), (
        "okno nie cofnęło się po kupno pozycji, której sprzedaż sumuje")
    assert najstarsze == _t(20, 16, 0), (
        "strażnik mierzy pokrycie względem węższego zbioru, niż sumuje "
        "odtworzenie — i to jest jego ślepa plamka")


def test_pozycja_zamknieta_PRZED_oknem_go_NIE_ROZSZERZA():
    """Kanarek do poprzedniego. Gdyby okno brało każdą pozycję z historii,
    sięgałoby po cały rejestr — czyli dokładnie tam, gdzie każda historyczna
    runda dokłada swoją resztkę, a strażnik milczy."""
    okno = [_pozycja(_t(22, 13, 20), _t(22, 16, 0)),
            _pozycja(_t(10, 13, 20), _t(10, 16, 0), _t(11, 9, 0))]
    od, _ = zakres_wypelnien(okno)
    assert od == _t(22, 13, 20), (
        "runda domknięta na długo przed oknem nie wchodzi do odtworzenia, "
        "więc nie ma powodu po nią sięgać")


def test_punkt_staly_przechodzi_przez_LANCUCH_nachodzacych_rund():
    """Jedno przejście nie wystarcza, gdy rundy zachodzą na siebie łańcuchem.

    A cofa okno po B, B po C. Zatrzymanie się na pierwszym kroku zostawiłoby
    kupno C poza oknem — czyli gołą sprzedaż w sumie i fałszywy `ilosc-ujemna`.
    """
    okno = [_pozycja(_t(22, 12, 0), _t(22, 12, 0)),                 # A, otwarta
            _pozycja(_t(21, 12, 0), _t(21, 12, 0), _t(22, 13, 0)),  # B
            _pozycja(_t(20, 12, 0), _t(20, 12, 0), _t(21, 13, 0))]  # C
    od, _ = zakres_wypelnien(okno)
    assert od == _t(20, 12, 0)


def test_BRAK_otwartych_pozycji_daje_okno_OGRANICZONE_a_nie_caly_rejestr():
    """Stan bez otwartych pozycji był NAJGORSZY, nie najlepszy.

    `od_ms = None` każe adapterowi pobrać cały dostępny rejestr, więc KAŻDA
    historyczna runda z prowizją w aktywie bazowym dokładała swoją resztkę —
    a `pokrycie_wypelnien` zwracało None w pierwszej linii, więc straży nie
    było żadnej. Sięgamy zamiast tego po ostatnią rundę, o której wiemy.
    """
    okno = [_pozycja(_t(20, 13, 20), _t(20, 16, 0), _t(21, 9, 0)),
            _pozycja(_t(22, 13, 20), _t(22, 16, 0), _t(23, 16, 3))]
    od, najstarsze = zakres_wypelnien(okno)
    assert od == _t(22, 13, 20), "okno bez ograniczenia to brak strażnika"
    assert najstarsze == _t(22, 16, 0)


def test_brak_JAKICHKOLWIEK_pozycji_zostawia_okno_puste():
    """Pusty dziennik to jedyny przypadek, w którym nie ma czego ograniczać."""
    assert zakres_wypelnien([]) == (None, None)


# ====================================== Z4: okno z danych, nie ze stalej

def test_poczatek_okna_idzie_z_SUBMITTED_AT_a_nie_z_opened_at():
    """Z4. Stała `timedelta(days=1)` miała zgadywać opóźnienie między
    wypełnieniem a zapisem pozycji. Zmierzone na zleceniu 47 wynosi ono
    2 h 40 min przy świecy H4H — czyli doba jest o rząd wielkości za szeroka,
    a przy świecy dobowej byłaby ZA WĄSKA i wycięłaby kupno tworzące pozycję.

    Zgadywać nie trzeba: giełda nie może wypełnić zlecenia, zanim je do niej
    wyślemy, a chwilę wysłania trzyma `exec.order.submitted_at`.
    """
    okno = [_pozycja(_t(22, 13, 20), _t(22, 16, 0))]
    od, najstarsze = zakres_wypelnien(okno)
    assert od == _t(22, 13, 20), "okno wzięte z chwili ZAPISU, nie WYSŁANIA"
    assert najstarsze == _t(22, 16, 0), (
        "pokrycie mierzone względem chwili wysłania alarmowałoby przy każdym "
        "przebiegu — wypełnienie jest z definicji późniejsze niż wysłanie")


def test_poczatek_okna_ZMIENIA_SIE_z_wierszem_zlecenia():
    """Kanarek do poprzedniego: gdyby odstęp był zaszytą stałą, oba zestawy
    dałyby tę samą liczbę i test wyżej przechodziłby przez przypadek."""
    wczesniej, _ = zakres_wypelnien([_pozycja(_t(22, 8, 0), _t(22, 16, 0))])
    pozniej, _ = zakres_wypelnien([_pozycja(_t(22, 15, 0), _t(22, 16, 0))])
    assert wczesniej == _t(22, 8, 0) and pozniej == _t(22, 15, 0)


def test_w_module_NIE_MA_juz_stalej_dobowej_na_okno():
    """Nagłówek modułu stawia tę zasadę wprost: „Żaden z nich nie jest stałą
    wpisaną w ten plik. Stała byłaby trzecim miejscem, w którym trzyma się
    reguły giełdy — i pierwszym, które by się rozjechało".

    SPRAWDZENIE NA ŹRÓDLE, bo test na zachowaniu przechodziłby także dla
    stałej dobranej tak, żeby akurat pasowała do liczb z tego pliku. To jest
    sprawdzenie słabsze niż reszta i jest tak nazwane.

    PATRZYMY NA DRZEWO SKŁADNI, NIE NA TEKST PLIKU. Dopasowanie po napisie
    trafiałoby także w komentarz OPISUJĄCY usuniętą stałą — a komentarz
    o stałej nie jest stałą, więc taki test kazałby wymazać wyjaśnienie,
    zamiast pilnować kodu.
    """
    import ast
    import inspect

    from services.engine import uzgodnienie

    drzewo = ast.parse(inspect.getsource(uzgodnienie))
    for w in ast.walk(drzewo):
        if (isinstance(w, ast.Call)
                and getattr(w.func, "id", None) == "timedelta"):
            nazwy = {k.arg for k in w.keywords}
            assert "days" not in nazwy, (
                f"okno pobierania wypełnień znowu jest stałą wpisaną w ten "
                f"plik (linia {w.lineno}) — a to ona decyduje, ile domkniętych "
                f"rund wejdzie do odtworzenia")


def test_margines_okna_to_RECV_WINDOW_gieldy_a_nie_wlasna_liczba():
    """Jedyne, co zostaje po stałej, to rozjazd zegarów — i tę liczbę też
    podaje giełda. `recv_window` jest granicą, powyżej której odrzuca KAŻDE
    podpisane zapytanie, więc zegar zdolny do handlu mieści się i tutaj."""
    from services.exchanges.bybit import RECV_WINDOW

    okno = [_pozycja(_t(22, 13, 20), _t(22, 16, 0))]
    gielda = _RejestrZeStartTime([])
    with pytest.raises(Nieuzgadnialne):
        # Pusty rejestr przy otwartej pozycji to brak pokrycia — pytamy tu
        # wylacznie o `od_ms`, ktore adapter zdazyl zapamietac.
        asyncio.run(uzgodnij(_fetch(("ETH",), okno=okno), _notify_zabroniony,
                             gielda, kind="spot_eu"))
    oczekiwane = int((_t(22, 13, 20)
                      - timedelta(milliseconds=int(RECV_WINDOW))).timestamp() * 1000)
    assert gielda.od_ms == oczekiwane


async def _notify_zabroniony(*a, **k):
    raise AssertionError("powiadomienie wysłane tam, gdzie go nie oczekiwano")


# ====================================== pelna sciezka: Z3 na liczbach

# Rejestr giełdy w kształcie z produkcji: runda ETH otwarta przed oknem
# i zamknięta w jego środku, obok żywego kupna SOL.
REJESTR = [
    _wypelnienie("0.604367", "Buy", "ETHUSDC", _t(20, 13, 25),
                 prowizja="0.000604367", prowizja_w="ETH"),
    _wypelnienie("12.138", "Buy", "SOLUSDC", _t(22, 13, 21),
                 prowizja="0.012138", prowizja_w="SOL"),
    _wypelnienie("0.604367", "Sell", "ETHUSDC", _t(22, 17, 30),
                 prowizja="1.6414", prowizja_w="USDC"),
]
OKNO_PRODUKCYJNE = [
    _pozycja(_t(22, 13, 20), _t(22, 16, 0)),                 # SOL, otwarta
    _pozycja(_t(20, 13, 20), _t(20, 16, 0), _t(22, 18, 0)),  # ETH, zamknieta
]
# `exec.position.qty` po odjeciu prowizji pobranej w kupionym aktywie.
DZIENNIK = [{"aktywo": "SOL", "ilosc": D("12.138") - D("0.012138")}]


def test_runda_zamknieta_w_oknie_NIE_JEST_juz_iloscia_ujemna():
    """Pełna ścieżka. Okno zaczynające się po kupnie ETH zostawiało w sumie
    gołą sprzedaż `-0,604367` i meldowało `ilosc-ujemna` — czyli kazało szukać
    okna retencji, które było w porządku."""
    async def notify(*a, **k):
        raise AssertionError("runda domknięta w oknie wysłała alarm krytyczny")

    n = asyncio.run(uzgodnij(
        _fetch(("ETH", "SOL"), DZIENNIK, OKNO_PRODUKCYJNE), notify,
        _RejestrZeStartTime(REJESTR), kind="spot_eu"))
    assert n == 0


def test_kanarek_PRAWDZIWIE_urwany_rejestr_nadal_jest_NIEWYKONALNY():
    """Kanarek do poprzedniego i sedno Z3 od drugiej strony.

    Ten sam dziennik, ale giełda naprawdę nie trzyma już kupna ETH z 20.09.
    Strażnik mierzący pokrycie względem pozycji WYŁĄCZNIE OTWARTYCH milczał
    tutaj (jego odniesienie to 22.09 16:00, a rejestr zaczynał się 22.09
    13:21), więc urwane okno docierało do `rozjazdy` jako `ilosc-ujemna` —
    czyli jako rozjazd stanu, a nie jako niewykonalność pomiaru.
    """
    with pytest.raises(Nieuzgadnialne) as e:
        asyncio.run(uzgodnij(
            _fetch(("ETH", "SOL"), DZIENNIK, OKNO_PRODUKCYJNE),
            _notify_zabroniony,
            _RejestrZeStartTime(REJESTR, retencja=_t(21, 0, 0)),
            kind="spot_eu"))
    assert "okno retencji" in str(e.value)


def test_pokrycie_mierzy_sie_wzgledem_NAJSTARSZEJ_POZYCJI_W_OKNIE():
    """Ta sama reguła na samej funkcji, bez ścieżki.

    Odniesienie z pozycji otwartych (22.09 16:00) przepuszczało rejestr
    zaczynający się 22.09 13:21; odniesienie z pozycji W OKNIE (20.09 16:00)
    zgłasza brak pokrycia — a to jest ten sam rejestr i ta sama chwila.
    """
    wyp = [_wypelnienie("12.138", "Buy", "SOLUSDC", _t(22, 13, 21))]
    assert pokrycie_wypelnien(wyp, _t(22, 16, 0)) is None
    powod = pokrycie_wypelnien(wyp, _t(20, 16, 0))
    assert powod and "okno retencji" in powod
