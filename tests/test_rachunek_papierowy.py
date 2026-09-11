"""Rachunek papierowy: rozstrzyganie planów bez giełdy.

DLACZEGO TEN PLIK ISTNIEJE

Ścieżka papierowa jest WZORCEM, z którym trener porównuje produkcję. Każdy
przypadek, który produkcja umie nazwać, a rachunek papierowy przemilcza,
przesuwa porównanie w jedną stronę — „symulacja wygląda lepiej". Rozjazd w tę
stronę jest najgorszy, bo nie wygląda na awarię, tylko na lepszy wynik.

Dokładnie tak zachowywało się wyjście bez pozycji: `gielda_exec` dostał dla
niego własny stan (`wyjscie_bez_pozycji`), a bliźniacza gałąź w `loop.py`
miała `if poz:` bez `else` — plan lądował na `triggered`, licznik `wypelnione`
rósł, i nie było ani linijki na ekranie, ani wiersza w bazie.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from services.engine import gielda_exec
from services.engine.horizons import get as get_horizon
from services.engine.loop import rozstrzygnij_plany
from services.engine.zlecenia import Filtry
from services.exchanges.base import StanZlecenia
from tests.atrapa_bazy import Kursor

H = get_horizon("5m")

# Świeca rozstrzygająca musi być DOMKNIETA — `zakres_swiecy` odmawia oceny
# świecy trwającej. Dlatego wszystkie znaczniki są w przeszłości wobec zegara.
BAR = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
SPRAWDZANA = BAR + timedelta(minutes=H.bar_minutes)
TERAZ = BAR + timedelta(hours=1)

SWIECA = {"high": D("110"), "low": D("90"), "close": D("100"), "n": 5}
POZYCJA = {"id": 77, "side": "long", "qty": D("10"), "avg_price": D("95")}
# Odpowiedzi na dwa odczyty, które `zapisz_wyjscie` wykonuje przy księgowaniu
# wypełnienia: skąd jest pozycja (`engine`, `mode`, `instrument_id`, waluta
# kwotowana) i jaki identyfikator dostało założone zlecenie wyjścia.
SKAD_POZYCJA = {"engine": "crypto", "mode": "demo", "instrument_id": 3,
                "side": "long", "quote_ccy": "USDC"}
ID_ZLECENIA = {"id": 91}


def plan(**nadpisz):
    """Plan `pending`, którego limit ŚWIECA TRAFIA — inaczej test badałby
    gałąź niewypełnienia zamiast tej, o którą chodzi."""
    p = {"id": 42, "decision_id": 7, "instrument_id": 3, "side": "sell",
         "qty": D("10"), "limit_price": D("100"), "symbol": "SOLUSDC",
         "quote_ccy": "USDC",
         "valid_until": SPRAWDZANA + timedelta(minutes=H.bar_minutes),
         "trigger": {"rodzaj": "wyjscie", "bar": BAR.isoformat(),
                     "position_id": 77}}
    p.update(nadpisz)
    return p


class KursorSekwencyjny(Kursor):
    """Atrapa z KOLEJKĄ odpowiedzi, po jednej na zapytanie.

    Bazowa atrapa oddaje z `fetchall` całą resztę podłożonych wierszy, a
    `rozstrzygnij_plany` po pobraniu planów wykonuje jeszcze dwa odczyty
    (świeca, pozycja). Bez kolejki drugi i trzeci odczyt zawsze zwracałyby
    None — czyli test „braku pozycji" przechodziłby także wtedy, gdyby kod
    w ogóle o pozycję nie pytał.
    """

    def fetchall(self):
        odp = self._wiersze.pop(0) if self._wiersze else []
        assert isinstance(odp, list), \
            f"fetchall dostał odpowiedź przeznaczoną dla fetchone: {odp!r}"
        return list(odp)


def rozstrzygnij(plany, *dalsze_odpowiedzi):
    cur = KursorSekwencyjny([list(plany), *dalsze_odpowiedzi])
    st = rozstrzygnij_plany(cur, "crypto", "demo", H, TERAZ, "spot", False)
    return cur, st


def wstawienia(cur, tabela):
    return [(s, p) for s, p in cur.sql if f"INSERT INTO {tabela}" in s]


# ------------------------------------------------ wyjście bez pozycji

def test_wyjscie_bez_pozycji_NIE_jest_liczone_jako_wypelnienie():
    """Cicha „sprzedaż bez pozycji" zawyżała licznik wypełnień rachunku
    papierowego wobec produkcji.

    Inaczej niż na giełdzie, tutaj NIC się nie wypełniło w żadnej księdze: nie
    powstał wiersz w `exec.fill` ani zmiana w `exec.position`. Policzenie tego
    jako wypełnienia byłoby wpisaniem do statystyki zdarzenia, którego nie ma
    w danych.
    """
    cur, st = rozstrzygnij([plan()], SWIECA, None)

    assert st["wyjscie_bez_pozycji"] == 1
    assert st["wypelnione"] == 0
    assert st["niewypelnione"] == 0


def test_licznik_anomalii_ISTNIEJE_zanim_anomalia_wystapi():
    """Klucz pojawiający się dopiero przy anomalii jest w heartbeacie nie do
    odróżnienia od klucza, którego nikt nie liczy."""
    _, st = rozstrzygnij([])

    assert st["wyjscie_bez_pozycji"] == 0


def test_wyjscie_bez_pozycji_ZOSTAWIA_wiersz_w_bazie():
    """Bez wiersza rozjazd z rzeczywistością wychodzi dopiero przy uzgodnieniu
    sald i wygląda wtedy jak błąd uzgodnienia, a nie jak ślad po tym zdarzeniu.

    Zapisujemy TYLKO to, co wiemy. Zamknięcie dowolnej innej otwartej pozycji
    na tym instrumencie przypisałoby sprzedaż cudzej decyzji i sfałszowało jej
    wynik — więc `exec.position` i `exec.fill` zostają nietknięte.
    """
    cur, _ = rozstrzygnij([plan()], SWIECA, None)

    (sql, params), = wstawienia(cur, "exec.order")
    assert "'rejected'" in sql, "wiersz zlecenia nie mówi, że nic się nie zaksięgowało"
    powod = next(p for p in params if isinstance(p, str) and "BEZ otwartej" in p)
    assert "position_id=77" in powod, "powód nie wskazuje pozycji, której zabrakło"
    assert not wstawienia(cur, "exec.position"), "zgadywana pozycja"
    assert not wstawienia(cur, "exec.fill"), "zgadywane wypełnienie"


def test_wyjscie_bez_pozycji_NIE_zostawia_planu_do_ponowienia():
    """Świeca rozstrzygająca jest już domknięta i w każdym kolejnym cyklu dałaby
    tę samą odpowiedź. Plan `pending` wracałby tu w kółko, bez szansy na zmianę
    wyniku; `expired` kłamałby w drugą stronę („limit nie został trafiony")."""
    cur, _ = rozstrzygnij([plan()], SWIECA, None)

    assert cur.zawiera("UPDATE journal.planned_order SET status='triggered'")
    assert not cur.zawiera("SET status='expired'")


def test_wyjscie_bez_pozycji_JEST_widoczne_na_ekranie(capsys):
    """Silnik czyta się z logu cyklu. Rozstrzygnięcie, które nie zostawia tam
    linijki, nie istnieje dla nikogo, kto ten log ogląda."""
    cur = KursorSekwencyjny([[plan()], SWIECA, None])
    rozstrzygnij_plany(cur, "crypto", "demo", H, TERAZ, "spot", True)

    assert "wyjscie_bez_pozycji" in capsys.readouterr().out


def test_plan_wyjscia_BEZ_position_id_tez_jest_anomalia():
    """Druga droga do tego samego stanu: plan, który nigdy nie niósł
    `position_id`. Kod nie pyta wtedy bazy o pozycję, więc gałąź `else` jest
    jedynym miejscem, gdzie ten przypadek może zostać nazwany."""
    bez_id = plan(trigger={"rodzaj": "wyjscie", "bar": BAR.isoformat(),
                           "position_id": None})
    cur, st = rozstrzygnij([bez_id], SWIECA)

    assert st["wyjscie_bez_pozycji"] == 1
    assert not cur.zawiera("FROM exec.position")


# --------------------------------------- ścieżki, których poprawka nie rusza

def test_wyjscie_Z_POZYCJA_nadal_ksieguje_zamkniecie():
    """Kontrola dodatnia. Bez niej „nic nie księgujemy" przechodziłoby także
    wtedy, gdyby poprawka zabiła normalne wyjście.

    Sprawdza też ślad w rejestrze wypełnień, bo to jest ta sama trasa co przy
    wejściu i musi zostawiać ten sam rodzaj wiersza. Do 2026-09-09 zostawiała
    wyłącznie zmianę w `exec.position`, więc rachunek papierowy miał w
    `exec.fill` same wejścia — a to na tej tabeli stoi pomiar egzekucji.
    """
    cur, st = rozstrzygnij([plan()], SWIECA, POZYCJA, SKAD_POZYCJA, ID_ZLECENIA)

    assert st["wypelnione"] == 1
    assert st["wyjscie_bez_pozycji"] == 0
    assert cur.zawiera("UPDATE exec.position SET closed_at=")
    assert wstawienia(cur, "exec.order"), "wyjście musi mieć własne zlecenie"
    assert wstawienia(cur, "exec.fill"), "wyjście musi mieć własne wypełnienie"


def test_wejscie_nadal_otwiera_pozycje():
    wejscie = plan(side="buy", trigger={"rodzaj": "wejscie", "bar": BAR.isoformat()})
    # Trzecia odpowiedź to `RETURNING id` z INSERT-a zlecenia.
    cur, st = rozstrzygnij([wejscie], SWIECA, {"id": 1})

    assert st["wypelnione"] == 1
    assert st["wyjscie_bez_pozycji"] == 0
    assert wstawienia(cur, "exec.position")


def test_nietrafiony_limit_nadal_wygasa_bez_ksiegowania():
    """Jedyne pozostałe przejście na `triggered` prowadzi przez trafiony limit.
    Plan, którego świeca nie dotknęła, ma wygasnąć i nie dotknąć żadnej księgi."""
    cur, st = rozstrzygnij([plan(limit_price=D("200"))], SWIECA)

    assert st["niewypelnione"] == 1
    assert not cur.zawiera("SET status='triggered'")
    assert not wstawienia(cur, "exec.order")


def test_brak_swiecy_NIE_ksieguje_niczego():
    """Brak danych to nie jest wypełnienie. `zakres_swiecy` oddaje None także
    wtedy, gdy świec źródłowych po prostu nie ma."""
    cur, st = rozstrzygnij([plan()], None)

    assert st["niewypelnione"] == 1
    assert not cur.zawiera("SET status='triggered'")


# ------------------------------------ zgodność nazw obu ścieżek egzekucji

class GieldaZWypelnieniem:
    """Minimalny adapter: zlecenie wypełnione w całości, nic do anulowania."""

    async def stan_zlecenia(self, id_wlasne, kategoria=None):
        return StanZlecenia(id_wlasne=id_wlasne, id_gieldy="G1", status="filled",
                            ilosc=D("10"), wypelniono=D("10"),
                            srednia_cena=D("100"), prowizja=D("0.25"))


def test_obie_sciezki_nazywaja_ten_sam_stan_TAK_SAMO():
    """Kanarek na rozjazd nazw.

    Rachunek papierowy i giełda opisują ten sam przypadek — wyjście, które nie
    ma czego zamknąć. Gdyby nazwały go inaczej, zniknąłby dokładnie tam, gdzie
    się go szuka: w zestawieniu obu ścieżek. Zmiana nazwy po JEDNEJ stronie ma
    wywrócić ten test.
    """
    cur = Kursor([{"id": 1, "qty": D("10")}, None])
    rodzaj, powod = asyncio.run(gielda_exec.rozstrzygnij_plan(
        cur, GieldaZWypelnieniem(),
        plan(id=44, trigger={"rodzaj": "wyjscie", "bar": BAR.isoformat(),
                             "position_id": 77}),
        Filtry(), engine="crypto", mode="demo", kategoria="spot",
        teraz=TERAZ, kind="spot"))

    _, st = rozstrzygnij([plan()], SWIECA, None)

    assert rodzaj == "wyjscie_bez_pozycji"
    assert rodzaj in st, (
        f"giełda nazywa ten stan {rodzaj!r}, a rachunek papierowy zna tylko "
        f"{sorted(st)} — porównanie obu ścieżek przestaje ten przypadek widzieć")
    assert st[rodzaj] == 1
    assert "BEZ otwartej pozycji" in powod
