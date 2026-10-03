"""Panel nie może nazywać „instrumentami" zbioru, którym silnik nie obstawia.

CO ZMIERZONO NA PRODUKCJI 2026-09-25

`/api/engines` zwracało jedną liczbę — `count(*) ... AND i.active` — a panel
podpisywał ją „N instrumentów". Wychodziło z tego:

    Bybit — 34 instrumentów     silnik obstawia DZIESIĘĆ
    Alpaca — 21 instrumentów    silnik obstawia ZERO

Pierwsza liczba jest myląca, druga jest nieprawdziwa jako opis stanu: tor equity
nie złożył ani jednego zlecenia i złożyć nie mógł, bo migracja 0047 zdjęła
`tradeable` wszystkim 21 akcjom — świadomie, z adnotacją, że włączenie do
uniwersum to osobna migracja.

`active` i `tradeable` odpowiadają na DWA różne pytania i żadne nie jest zbędne:
historię zbieramy szerzej, niż handlujemy (migracja 0020), a od migracji 0023 ten
sam aktyw jest w bazie dwa razy — jako *USDT i jako *USDC. Poprawny wzór stał
obok: `routers/kapital_laczny.py` liczy waluty osobno dla `tradeable` i osobno
dla wszystkich aktywnych.

DLACZEGO TEST WYKONUJE PRAWDZIWE PODZAPYTANIA

Cała poprawka siedzi w dwóch warunkach SQL. Przepisanie ich tutaj testowałoby
moje przepisanie — więc podzapytania są WYCIĄGANE z treści `main.py` i
uruchamiane na sqlite, na tabeli odtwarzającej realny skład `ref.instrument`.
Zapytanie całościowe (`jsonb_agg`, `DISTINCT ON`) ma składnię, której sqlite nie
zna, a przedmiotem poprawki nie jest ono, tylko liczone zbiory.

Nic nie łączy się z produkcyjną bazą.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path


KORZEN = Path(__file__).resolve().parents[1]
API_MAIN = KORZEN / "services/api/main.py"
APP_JSX = KORZEN / "web/src/App.jsx"

# STAN BAZY NA 2026-09-25, te same liczby, które panel pokazuje jako podpisy.
BYBIT_USDC = 10        # uniwersum decyzyjne crypto
BYBIT_USDT = 20        # `active`, `tradeable = false` od migracji 0023
BYBIT_PERP = 4         # `kind = 'perp'`, źródło stawki finansowania
ALPACA_AKCJE = 21      # `active`, `tradeable = false` od migracji 0047


def _zapytanie_engines() -> str:
    """Treść zapytania z `engines()` — zwinięta, bez komentarzy SQL."""
    zrodlo = API_MAIN.read_text(encoding="utf-8")
    # Od nazwy funkcji do pierwszego `fetch_one` wewnątrz niej, a potem pierwszy
    # literał `"""..."""` — czyli treść zapytania. Bez szukania końca funkcji:
    # `engines()` stoi dziś na końcu pliku i dopisanie następnej trasy nie może
    # decydować o tym, czy ten test cokolwiek mierzy.
    poczatek = zrodlo.index("async def engines(")
    po_wywolaniu = zrodlo.index("fetch_one", poczatek)
    sql = zrodlo[zrodlo.index('"""', po_wywolaniu):].split('"""')[1]
    assert "ref.instrument" in sql, (
        f"wyciągnięty fragment nie jest zapytaniem `engines()`:\n{sql[:300]}")
    # Komentarze `--` PIERWSZE: zwinięcie w jeden wiersz przed ich usunięciem
    # zamienia pierwszy komentarz w koniec zapytania.
    sql = " ".join(w.split("--")[0] for w in sql.splitlines())
    return " ".join(sql.split())


def _podzapytania_liczace() -> dict[str, str]:
    """Skalarne podzapytania `count(*) FROM ref.instrument` po nazwie pola JSON.

    Nazwa pola stoi w zapytaniu bezpośrednio przed podzapytaniem
    (`'tradeable', (SELECT count(*) ...)`), więc bierzemy ją stamtąd — dzięki
    temu test nie zna nazw pól z innego źródła niż kod.
    """
    sql = _zapytanie_engines()
    wzor = re.compile(
        r"'(?P<pole>\w+)',\s*\((?P<zapytanie>SELECT count\(\*\) FROM ref\.instrument.*?)\)\s*(?=,|\))")
    znalezione = {m.group("pole"): m.group("zapytanie") for m in wzor.finditer(sql)}
    assert znalezione, f"nie znaleziono podzapytań liczących w:\n{sql}"
    return znalezione


def _baza():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE ref_venue (id INTEGER, code TEXT, name TEXT,
            engine TEXT, is_24_7 INTEGER);
        CREATE TABLE ref_instrument (id INTEGER, venue_id INTEGER, symbol TEXT,
            engine TEXT, kind TEXT, active INTEGER, tradeable INTEGER);
        INSERT INTO ref_venue VALUES (1,'bybit','Bybit','crypto',1);
        INSERT INTO ref_venue VALUES (2,'alpaca','Alpaca','equity',0);
    """)
    nid = 0
    wiersze = []
    for _ in range(BYBIT_USDC):
        nid += 1
        wiersze.append((nid, 1, f"A{nid}USDC", "crypto", "spot", 1, 1))
    for _ in range(BYBIT_USDT):
        nid += 1
        wiersze.append((nid, 1, f"A{nid}USDT", "crypto", "spot", 1, 0))
    for _ in range(BYBIT_PERP):
        nid += 1
        # `tradeable = 1`: `funding.ensure_perp` nie podawał tej kolumny,
        # a DEFAULT to `true` (punkt 11b audytu). Uniwersum decyzyjne musi je
        # odrzucać po `kind`, a nie liczyć na wartość w innej kolumnie.
        wiersze.append((nid, 1, f"A{nid}USDT.P", "crypto", "perp", 1, 1))
    for _ in range(ALPACA_AKCJE):
        nid += 1
        wiersze.append((nid, 2, f"S{nid}", "equity", "stock", 1, 0))
    db.executemany("INSERT INTO ref_instrument VALUES (?,?,?,?,?,?,?)", wiersze)
    return db


def _policz(pole: str, venue_id: int) -> int:
    zapytanie = _podzapytania_liczace()[pole]
    zapytanie = zapytanie.replace("ref.instrument", "ref_instrument")
    zapytanie = zapytanie.replace("i.venue_id = v.id", f"i.venue_id = {venue_id}")
    return _baza().execute(zapytanie).fetchone()[0]


def test_api_oddaje_DWIE_liczby_a_nie_jedna():
    """Sedno poprawki. Jedna liczba nie ma jak opisać dwóch różnych zbiorów."""
    pola = set(_podzapytania_liczace())
    assert pola == {"instruments", "tradeable"}, (
        f"/api/engines liczy {sorted(pola)}; spodziewane `instruments` "
        f"(w historii) i `tradeable` (w uniwersum decyzyjnym)")


def test_liczba_w_historii_to_wszystko_co_aktywne():
    """Ta liczba NIE zmienia znaczenia — zmienia się wyłącznie jej podpis.

    Gdyby poprawka ją zawęziła, panel przestałby pokazywać, dla ilu
    instrumentów zbieramy historię; a zbieramy szerzej, niż handlujemy (0020).
    """
    assert _policz("instruments", 1) == BYBIT_USDC + BYBIT_USDT + BYBIT_PERP
    assert _policz("instruments", 2) == ALPACA_AKCJE


def test_liczba_w_uniwersum_to_dokladnie_to_co_silnik_obstawia():
    """To jest ta poprawka: dziesięć dla Bybita, ZERO dla Alpaki.

    Zero nie jest tu brakiem danych — jest stanem, o którym zdecydowała
    migracja 0047, i panel ma go pokazywać, a nie zasłaniać liczbą 21.
    """
    assert _policz("tradeable", 1) == BYBIT_USDC, (
        f"uniwersum crypto wyszło {_policz('tradeable', 1)} zamiast {BYBIT_USDC} "
        f"— pary USDT albo perpetuale wchodzą do liczby, której nie opisują")
    assert _policz("tradeable", 2) == 0, (
        "panel nadal pokazuje niezerowe uniwersum dla silnika, który nie ma ani "
        "jednego instrumentu dopuszczonego do handlu")


def test_perpetualy_NIE_wchodza_do_uniwersum():
    """Kanarek na warunek bez `kind <> 'perp'`.

    Perpetuale są w bazie WYŁĄCZNIE jako źródło stawki finansowania i nikt ich
    nie obstawia; z `tradeable` wziętym z DEFAULT-u (punkt 11b) podniosłyby tę
    liczbę o cztery i nikt by tego nie zauważył, bo 14 wygląda tak samo
    wiarygodnie jak 10.
    """
    zapytanie = _podzapytania_liczace()["tradeable"]
    assert "perp" in zapytanie, (
        "warunek uniwersum nie odcina perpetualów po `kind` — przy `tradeable` "
        "z DEFAULT-u policzyłby je jako obstawiane")
    assert _policz("tradeable", 1) == BYBIT_USDC


# ------------------------------------------------------------------ panel

def test_panel_pokazuje_OBIE_liczby_z_roznymi_podpisami():
    """Zwrócenie dwóch liczb przez API nie naprawia panelu, który pokazuje jedną.

    Podpisy muszą się różnić, bo tu właśnie siedziała wada: liczba `active`
    z podpisem opisującym uniwersum decyzyjne.
    """
    jsx = APP_JSX.read_text(encoding="utf-8")
    assert "current.instruments" in jsx, "panel przestał pokazywać liczbę z historii"
    assert "current.tradeable" in jsx, (
        "panel nie pokazuje uniwersum decyzyjnego — API oddaje liczbę, której "
        "nikt nie czyta")
    assert "w historii" in jsx and "w uniwersum" in jsx, (
        "podpisy nie rozróżniają obu zbiorów")
    assert not re.search(r"\{current\.instruments\}\s*instrumentów", jsx), (
        "wróciło „{current.instruments} instrumentów” — liczba `active` "
        "podpisana słowem opisującym uniwersum decyzyjne")
