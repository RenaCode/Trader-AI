"""Pozycja, z której po wyjściu zostaje pył — musi się DOMKNĄĆ, a nie zmniejszyć.

DLACZEGO TEN PLIK ISTNIEJE

Warunek zamknięcia pozycji brzmiał `ile >= calosc`, czyli zakładał, że da się
sprzedać dokładnie tyle, ile stoi na wierszu. Na spocie jest to nieprawda ZA
KAŻDYM RAZEM: kupno płaci prowizję w kupionym aktywie, więc
`egzekucja.rozlicz_prowizje` zapisuje na `exec.position.qty` ilość NETTO, która
z definicji nie leży na kroku ilości giełdy, a `zlecenia.sprawdz_zlecenie`
zaokrągla wyjście W DÓŁ do kroku. Zostaje resztka mniejsza od kroku — nie do
sprzedania — przez którą `closed_at` zostaje NULL na zawsze, `open_position`
dalej oddaje ten wiersz, a pętla decyzyjna przy niepustej pozycji wybiera
wyłącznie `exit` albo `hold`. Instrument wypada z handlu NA STAŁE, a w logu
wygląda to na rynek bez sygnałów.

Nadzór tego nie zgłosi: `uzgodnienie.progi_bezwzgledne` istnieje właśnie po to,
żeby resztkę poniżej `lot_size` wyciszyć. Pył jest więc jednocześnie
NIEWIDOCZNY dla nadzoru i BLOKUJĄCY dla silnika.

LICZBY SĄ PRODUKCYJNE, A NIE UŁOŻONE POD TEZĘ. ADAUSDC na Bybicie: krok ilości
0,1, krok ceny 0,0001, prowizja makera 0,1% pobrana w ADA. Cały rachunek robią
funkcje produkcyjne — `rozlicz_prowizje`, `sprawdz_zlecenie`, `czy_zamykalna`
i `zapisz_wyjscie` — a nie wpisana z ręki tabelka.
"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from services.engine.egzekucja import rozlicz_prowizje
from services.engine.gielda_exec import rozstrzygnij_plan
from services.engine.loop import zapisz_wyjscie
from services.engine.zlecenia import Filtry, czy_zamykalna, sprawdz_zlecenie
from services.exchanges.base import ExchangeError, StanZlecenia

# ADAUSDC, reguły tak, jak oddaje je giełda do `ref.instrument`.
ADAUSDC = Filtry(krok_ilosci=D("0.1"), krok_ceny=D("0.0001"),
                 min_wartosc=D("1"), min_ilosc=None)
MAKER = D("0.001")                      # 0,1%
TERAZ = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


class Kursor:
    """Atrapa kursora oddająca WYŁĄCZNIE kolumny wymienione w SELECT.

    TO NIE JEST OZDOBA, TYLKO WARUNEK UCZCIWOŚCI TEGO TESTU. Atrapa, która
    oddaje słownik ułożony przez test, potrafi podać kolumnę, o którą prawdziwy
    `SELECT` nie prosi — i wtedy test przechodzi, a produkcja dostaje `KeyError`
    albo, gorzej, `None`. Jest to znana wada tego projektu, więc zwracany wiersz
    powstaje tutaj przez PRZYCIĘCIE stanu do listy kolumn wyczytanej z samego
    zapytania.
    """

    # Źródło wiersza rozpoznajemy po TABELI, a nie po kolejności wywołań.
    # Kolejność jest szczegółem implementacji, a `exec.order.qty` i
    # `exec.position.qty` to DWIE RÓŻNE liczby — ilość wysłana i ilość
    # posiadana. Atrapa, która oddaje jedną na oba pytania, kasuje dokładnie tę
    # różnicę, o którą chodzi w tym pliku.
    ZRODLA = (("FROM exec.order", "order"),
              ("FROM exec.position", "position"))

    def __init__(self, stan: dict):
        self.stan = stan
        self.sql: list[tuple[str, tuple]] = []
        self._odp: dict | None = None

    @staticmethod
    def _kolumny(sql: str) -> list[str]:
        wybor = sql[sql.index("SELECT ") + 7:sql.index(" FROM ")]
        nazwy = []
        for pole in wybor.split(","):
            pole = pole.strip()
            # alias po " AS ", inaczej nazwa kolumny po kropce kwalifikatora
            nazwy.append(pole.split(" AS ")[-1].split(".")[-1].strip())
        return nazwy

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        oczekiwane = jedno.count("%s")
        podane = 0 if params is None else len(params)
        assert oczekiwane == podane, (
            f"zapytanie ma {oczekiwane} miejsc na parametry, a dostało {podane}: "
            f"{jedno}")
        self.sql.append((jedno, tuple(params or ())))
        if not jedno.startswith("SELECT"):
            self._odp = None
            return
        zrodlo = next((n for f, n in self.ZRODLA if f in jedno), None)
        assert zrodlo is not None, f"atrapa nie zna tabeli w zapytaniu: {jedno}"
        wiersz = self.stan.get(zrodlo)
        if wiersz is None:
            self._odp = None
            return
        kolumny = self._kolumny(jedno)
        brak = [k for k in kolumny if k not in wiersz]
        assert not brak, (
            f"zapytanie prosi o kolumny {brak}, których atrapa nie zna dla "
            f"{zrodlo!r} — uzupełnij stan, zamiast oddawać wiersz w innym "
            f"kształcie niż produkcja")
        self._odp = {k: wiersz[k] for k in kolumny}

    def fetchone(self):
        return self._odp

    def fetchall(self):
        return [self._odp] if self._odp else []

    def zapytania(self, fragment):
        return [(s, p) for s, p in self.sql if fragment in s]


class Gielda:
    def __init__(self, stan):
        self._stan = stan
        self.anulowane = []

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return self._stan

    async def anuluj_zlecenie(self, **kw):
        self.anulowane.append(kw)
        return True

    async def cena_odniesienia(self, symbol, kategoria=None):
        return None

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        """Arkusz NIEODCZYTANY — i to jest w tym pliku decyzja, nie brak.

        `zloz_plan` czyta arkusz księgi egzekucji, żeby cena limitu pochodziła
        z chwili wysyłki. Ten plik mierzy co innego (POWÓD odmowy giełdy),
        a podstawienie arkusza podmieniałoby cenę wysyłaną i mieszało dwa
        pomiary w jednej asercji. Atrapa MUSI jednak tę metodę mieć: bez niej
        produkcyjne wywołanie kończy się `AttributeError`, który wygląda jak
        błąd testu, a nie jak luka w atrapie.
        """
        raise ExchangeError("arkusz nieczytelny (atrapa)")


# =========================================================== rachunek pętli

def test_petla_wejscie_wyjscie_resztka_JEST_NIE_DO_SPRZEDANIA():
    """Cała pętla na funkcjach produkcyjnych: wejście → wyjście → resztka →
    następna świeca. Dowód, że resztki nie da się sprzedać NIGDY, a nie że
    „czasem bywa za mała"."""
    # 1. WEJŚCIE. Giełda oddaje 1998,7 ADA (na kroku 0,1) i pobiera prowizję
    #    makera W ADA — tak działa spot na Bybicie przy kupnie.
    brutto, cena_wejscia = D("1998.7"), D("0.3500")
    prowizja_ada = brutto * MAKER                       # 1,9987 ADA
    netto, koszt = rozlicz_prowizje(brutto, cena_wejscia, prowizja_ada,
                                    "ADA", "ADA", "USDC")
    assert netto == D("1996.7013")
    assert koszt == prowizja_ada * cena_wejscia         # koszt w USDC, nie w ADA
    # Ilość netto NIE LEŻY NA KROKU — i to jest cała przyczyna tej wady.
    assert netto % ADAUSDC.krok_ilosci != 0

    # 2. WYJŚCIE. Plan obejmuje całą pozycję; sprawdzenie zaokrągla W DÓŁ.
    cena_wyjscia = D("0.3600")
    wyslana, _, powod = sprawdz_zlecenie(netto, cena_wyjscia, ADAUSDC,
                                         kupno=False, wyjscie=True)
    assert powod is None
    assert wyslana == D("1996.7")

    # 3. RESZTKA. Mniejsza od kroku, więc z definicji nie do sprzedania.
    resztka = netto - wyslana
    assert resztka == D("0.0013")
    assert not czy_zamykalna(resztka, cena_wyjscia, ADAUSDC)

    # 4. NASTĘPNA ŚWIECA. Silnik planuje wyjście z resztki i dostaje odmowę —
    #    i tak w kółko, bo każdy przebieg wygląda tak samo jak poprzedni.
    _, _, powod_nastepnej = sprawdz_zlecenie(resztka, cena_wyjscia, ADAUSDC,
                                             kupno=False, wyjscie=True)
    assert powod_nastepnej is not None and "wynosi zero" in powod_nastepnej


# ================================================= domknięcie wiersza pozycji

def _stan_bazy(qty, avg_price=D("0.3500"), wyslano=None):
    """`position` to ilość POSIADANA (netto po prowizji), `order` — WYSŁANA."""
    return {
        "position": {"id": 77, "side": "long", "qty": qty,
                     "avg_price": avg_price, "engine": "crypto", "mode": "demo",
                     "instrument_id": 3, "quote_ccy": "USDC"},
        # `submitted_at` jest w tym SELECT-cie, bo zaweza rejestr wykonan przy
        # odczycie waluty prowizji (`gielda_exec.prowizja_z_rejestru`).
        "order": {"id": 9, "qty": qty if wyslano is None else wyslano,
                  "submitted_at": TERAZ},
    }


def test_wyjscie_z_pylem_DOMYKA_pozycje_zamiast_ja_zmniejszac():
    """Warunek `ile >= calosc` zostawiał `closed_at` NULL na zawsze."""
    cur = Kursor(_stan_bazy(D("1996.7013")))
    pozycja = {"id": 77, "side": "long", "qty": D("1996.7013"),
               "avg_price": D("0.3500")}
    zapisz_wyjscie(cur, pozycja, D("0.3600"), D("0.72"), TERAZ, 501,
                   ilosc=D("1996.7"), order_id=9, filtry=ADAUSDC)

    domkniecia = cur.zapytania("UPDATE exec.position SET closed_at")
    zmniejszenia = cur.zapytania("UPDATE exec.position SET qty = qty -")
    assert domkniecia, ("pozycja z resztką 0,0013 ADA przy kroku 0,1 nie "
                        "zostałaby zamknięta NIGDY — instrument wypada z handlu")
    assert not zmniejszenia


def test_pyl_JEST_SPISANY_NA_STRATE_a_nie_znika_po_cichu():
    """Samo `closed_at` skasowałoby niezrealizowaną stratę na resztce z
    `update_equity` (liczy `unrealized` z pozycji OTWARTYCH) i PODNIOSŁO
    kapitał w chwili, w której coś tracimy — czyli obsunięcie wyszłoby
    mniejsze, a kill-switch strzeliłby później."""
    entry, wyjscie = D("0.3500"), D("0.3600")
    cur = Kursor(_stan_bazy(D("1996.7013"), entry))
    pozycja = {"id": 77, "side": "long", "qty": D("1996.7013"), "avg_price": entry}
    wynik = zapisz_wyjscie(cur, pozycja, wyjscie, D("0.72"), TERAZ, 501,
                           ilosc=D("1996.7"), order_id=9, filtry=ADAUSDC)

    pyl = D("1996.7013") - D("1996.7")
    oczekiwany = (wyjscie - entry) * D("1996.7") - entry * pyl
    assert wynik == oczekiwany
    (_, params), = cur.zapytania("UPDATE exec.position SET closed_at")
    assert oczekiwany in params, "do `realized_pnl` poszła inna liczba niż zwrócona"

    # ŚLAD PO RESZTCE ZOSTAJE W WIERSZU ZLECENIA. Bez niego domknięcie z pyłem
    # wygląda identycznie jak domknięcie pełne, a różnicą jest aktywo, które
    # nadal leży na giełdzie.
    (sql, params), = cur.zapytania("UPDATE exec.order SET reject_reason")
    assert any(isinstance(p, str) and "resztka_nierozliczalna" in p
               for p in params)
    assert str(pyl) in json.dumps([str(p) for p in params])


def test_wyjscie_czesciowe_ktore_DA_SIE_dokonczyc_nadal_zmniejsza_pozycje():
    """Kanarek w drugą stronę. Gdyby domykanie ruszało każdą niepełną
    sprzedaż, wyjście wypełnione w połowie zapisywałoby, że nie mamy niczego —
    czyli pozycję-widmo, i to najgorszego rodzaju, bo taką, której
    przestalibyśmy pilnować."""
    cur = Kursor(_stan_bazy(D("1996.7013")))
    pozycja = {"id": 77, "side": "long", "qty": D("1996.7013"),
               "avg_price": D("0.3500")}
    zapisz_wyjscie(cur, pozycja, D("0.3600"), D("0.36"), TERAZ, 501,
                   ilosc=D("1000.0"), order_id=9, filtry=ADAUSDC)

    assert not cur.zapytania("UPDATE exec.position SET closed_at")
    assert cur.zapytania("UPDATE exec.position SET qty = qty -")


def test_bez_regul_gieldy_NIC_NIE_DOMYKAMY():
    """Braku reguł nie wolno czytać jako „nie da się sprzedać". Domyślne
    `Filtry()` uznałoby każdą resztkę za niezamykalną i zamykało pozycje,
    z których da się jeszcze wyjść."""
    cur = Kursor(_stan_bazy(D("1996.7013")))
    pozycja = {"id": 77, "side": "long", "qty": D("1996.7013"),
               "avg_price": D("0.3500")}
    zapisz_wyjscie(cur, pozycja, D("0.3600"), D("0.72"), TERAZ, 501,
                   ilosc=D("1996.7"), order_id=9)          # bez `filtry`
    assert cur.zapytania("UPDATE exec.position SET qty = qty -")


# ======================= czego nie da sie sprzedac NIGDY, a czego tylko DZIS

@pytest.mark.parametrize("reguly, opis", [
    (Filtry(), "gołe Filtry() — tak `gielda_exec` podstawia instrument "
               "nieobecny w mapie reguł (`filtry.get(id, Filtry())`)"),
    (Filtry(krok_ilosci=D("0.1"), krok_ceny=D("0.0001"), min_wartosc=None),
     "`min_notional` NULL w ref.instrument — kolumna jest nullowalna"),
])
def test_REGUL_NIEZNANYCH_nie_wolno_czytac_jako_nie_da_sie_sprzedac(reguly, opis):
    """Kanarek, którego brakowało — i dlatego ta wada przeżyła pierwszy audyt.

    Docstring `zapisz_wyjscie` nazywał to zagrożenie wprost („domyślne
    `Filtry()` uznałoby każdą resztkę za niezamykalną"), ale warunek brzmiał
    `filtry is not None`, a to NIE JEST ten sam strażnik: gołe `Filtry()` nie
    jest `None` i przechodziło. Test obok sprawdzał wywołanie BEZ argumentu,
    czyli gałąź, którą produkcja nigdy nie chodzi.

    Skutek jest bez ograniczenia co do wielkości: przy nieznanych regułach
    KAŻDE wyjście częściowe domykało pozycję i spisywało całą resztę na stratę
    po cenie wejścia — tu 996,7 ADA (~359 USDC), `realized_pnl` -338,85
    zamiast +10,00. Aktywo zostaje przy tym na giełdzie, poza ewidencją.
    """
    cur = Kursor(_stan_bazy(D("1996.7013")))
    pozycja = {"id": 77, "side": "long", "qty": D("1996.7013"),
               "avg_price": D("0.3500")}
    wynik = zapisz_wyjscie(cur, pozycja, D("0.3600"), D("0.72"), TERAZ, 501,
                           ilosc=D("1000.0"), order_id=9, filtry=reguly)
    assert not cur.zapytania("UPDATE exec.position SET closed_at"), (
        f"pozycja domknięta przy regułach nieznanych ({opis}) — 996,7 ADA "
        f"spisane na stratę, choć nikt nie wie, czy da się je sprzedać")
    assert wynik == (D("0.3600") - D("0.3500")) * D("1000.0")


def test_resztka_ponizej_MINIMUM_WARTOSCI_ale_na_kroku_NIE_JEST_pylem():
    """Kryterium domknięcia musi być TRWAŁE, a nie chwilowe.

    `gielda_exec.zloz_plan` mówi to wprost w komentarzu: „Sama `min_notional`
    (wartość poniżej minimum) NIE wystarcza: pozycja na kroku, która dziś jest
    warta za mało, jutro po wzroście ceny sprzeda się normalnie, a spisanie
    jej na stratę byłoby wyrzuceniem żywych pieniędzy". `zapisz_wyjscie`
    pytało jednak `czy_zamykalna`, które przewraca się właśnie na wartości.

    Liczby produkcyjne: ADAUSDC ma krok 0,1 i `min_notional` 5 USDC, więc przy
    cenie 0,36 próg wartości to 13,9 ADA — czyli 139 PEŁNYCH kroków ilości.
    """
    ada_prod = Filtry(krok_ilosci=D("0.1"), krok_ceny=D("0.0001"),
                      min_wartosc=D("5"))
    resztka = D("13.8")                        # 138 kroków, ~4,97 USDC
    assert resztka % ada_prod.krok_ilosci == 0
    assert not czy_zamykalna(resztka, D("0.3600"), ada_prod), (
        "kanarek atrapy: przy dzisiejszej cenie giełda TEJ resztki nie kupi")
    assert czy_zamykalna(resztka, D("0.3700"), ada_prod), (
        "a po wzroście ceny o 2,8% kupi — czyli kryterium jest CHWILOWE")

    cur = Kursor(_stan_bazy(D("1996.7013")))
    pozycja = {"id": 77, "side": "long", "qty": D("1996.7013"),
               "avg_price": D("0.3500")}
    zapisz_wyjscie(cur, pozycja, D("0.3600"), D("0.72"), TERAZ, 501,
                   ilosc=D("1996.7013") - resztka, order_id=9, filtry=ada_prod)
    assert not cur.zapytania("UPDATE exec.position SET closed_at"), (
        "13,8 ADA spisane na stratę jako „nie do sprzedania”, choć to 138 "
        "kroków ilości i wystarczy wzrost ceny o 2,8%, żeby je sprzedać")


def test_kanarek_resztka_PONIZEJ_KROKU_nadal_domyka_pozycje():
    """Kanarek w drugą stronę do obu powyższych.

    Zawężenie kryterium nie może cofnąć poprawki, dla której ten plik powstał:
    resztka mniejsza od kroku jest nie do sprzedania przy KAŻDEJ cenie, a
    otwarty wiersz wyklucza instrument z handlu na stałe."""
    ada_prod = Filtry(krok_ilosci=D("0.1"), krok_ceny=D("0.0001"),
                      min_wartosc=D("5"))
    cur = Kursor(_stan_bazy(D("1996.7013")))
    pozycja = {"id": 77, "side": "long", "qty": D("1996.7013"),
               "avg_price": D("0.3500")}
    zapisz_wyjscie(cur, pozycja, D("0.3600"), D("0.72"), TERAZ, 501,
                   ilosc=D("1996.7"), order_id=9, filtry=ada_prod)
    assert cur.zapytania("UPDATE exec.position SET closed_at")


# ============================================ przez produkcyjne rozstrzyganie

def test_ROZSTRZYGNIECIE_wyjscia_domyka_pozycje_z_pylem():
    """Ten sam scenariusz, ale przez `gielda_exec.rozstrzygnij_plan` — czyli
    przez drogę, którą naprawdę chodzi produkcja. Test wyżej sprawdza regułę,
    ten sprawdza, czy reguła jest w ogóle wołana z regułami instrumentu."""
    plan = {"id": 42, "decision_id": 501, "instrument_id": 3, "side": "sell",
            "qty": D("1996.7013"), "limit_price": D("0.3600"),
            "symbol": "ADAUSDC", "quote_ccy": "USDC", "base_ccy": "ADA",
            "trigger": {"rodzaj": "wyjscie", "position_id": 77, "bar": "x"}}
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="filled",
                        ilosc=D("1996.7"), wypelniono=D("1996.7"),
                        srednia_cena=D("0.3600"), prowizja=D("0.72"),
                        prowizja_w="USDC")
    # Na giełdę poszło 1996,7 — ilość z planu zaokrąglona W DÓŁ do kroku.
    cur = Kursor(_stan_bazy(D("1996.7013"), wyslano=D("1996.7")))
    rodzaj, _ = asyncio.run(rozstrzygnij_plan(
        cur, Gielda(stan), plan, ADAUSDC, engine="crypto", mode="demo",
        kategoria="spot", teraz=TERAZ, kind="spot"))

    assert rodzaj == "pelne"
    assert cur.zapytania("UPDATE exec.position SET closed_at"), (
        "rozstrzygnięcie wyjścia nie przekazało reguł instrumentu, więc pozycja "
        "została otwarta z resztką 0,0013 ADA — i zostałaby taka na zawsze")


def test_pozycja_CALA_ponizej_kroku_domyka_sie_zamiast_blokowac_instrument():
    """Trzecie wcielenie tej samej wady: pozycja mniejsza od kroku ilości
    jeszcze ZANIM cokolwiek wyślemy (tak powstają wejścia `nie_do_zamkniecia`).
    Sprawdzenie przed wysłaniem odmawia, plan idzie na `rejected` — i wiersz
    pozycji zostaje otwarty na zawsze, blokując instrument."""
    from services.engine.gielda_exec import zloz_plan

    plan = {"id": 43, "decision_id": 502, "instrument_id": 3, "side": "sell",
            "qty": D("0.05"), "limit_price": D("0.3600"), "symbol": "ADAUSDC",
            "quote_ccy": "USDC", "base_ccy": "ADA", "atr_pct": None,
            "trigger": {"rodzaj": "wyjscie", "position_id": 77, "bar": "x"}}
    cur = Kursor(_stan_bazy(D("0.05")))
    ok, msg = asyncio.run(zloz_plan(cur, Gielda(None), plan, ADAUSDC,
                                    engine="crypto", mode="demo",
                                    kategoria="spot"))
    assert ok is False
    assert cur.zapytania("UPDATE exec.position SET closed_at"), (
        "pozycja 0,05 ADA przy kroku 0,1 nie da się sprzedać przy ŻADNEJ cenie, "
        "a otwarty wiersz wyklucza instrument z handlu na stałe")
    assert "POZYCJA DOMKNIĘTA" in msg
