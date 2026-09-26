"""Jedna seria krzywej kapitału — JEDNA definicja kapitału.

CO SIĘ STAŁO

Do `exec.equity_point` piszą TRZY procesy, każdy z inną definicją kapitału:

    loop.seed_equity             STAŁA: START_EQUITY i `nav = 1.0` na sztywno
    loop.update_equity           REKONSTRUKCJA z naszego dziennika wypełnień
    kapital_gieldy.zapisz_punkt  ODCZYT z ksiąg giełdy (Alpaca /v2/account)

Trzeci doszedł 2026-09-22 razem z workerem `kapital-equity` i od tego dnia
seria `equity/demo` ma DWÓCH pisarzy naraz. Czytelnicy — `RiskKernel`, dozorca
ryzyka i `wznowienie.stan_konta` — biorą z niej `max`, ostatni punkt i punkt
sprzed doby BEZ pytania, kto zapisał, więc zlanie definicji nie objawia się
błędem. Objawia się miarą liczoną z dwóch różnych rzeczy naraz.

DLACZEGO TO JEST GROŹNE, A NIE TYLKO NIEELEGANCKIE

`SQL_NAV_AGREGAT` to `max(nav)` BEZ OGRANICZENIA CZASOWEGO. Zasiane
`nav = 1.0` — twierdzenie „rachunek jest dokładnie równy wpłatom", którego
nikt nie zmierzył — zostaje więc szczytem NA ZAWSZE. Przy `max_drawdown` 10%
(migracja 0008, akcja `killswitch`) konto paper poniżej 90% zgadniętej wpłaty
daje kill-switcha, który NIE MIJA Z DOBĄ i nie mija nigdy: przyczyna jest
arytmetyczna, a dla `engine='equity'` nie ma procesu zdolnego go zwolnić
(dozorca chodzi z `--engine crypto`).

STAN ZMIERZONY NA PRODUKCJI 2026-09-26: 367 punktów `equity/demo`, WSZYSTKIE
z `nav = 1.000000`; zero kill-switchy equity w całej historii; zero
instrumentów `engine='equity' AND tradeable`. Skoro nic nie handluje, trzy
definicje dają trywialnie tę samą liczbę — szkoda jest REALNA W MECHANIZMIE
i UŚPIONA. Dlatego naprawa jest tania właśnie teraz: nie ma historii do
uratowania.

WŁASNOŚCI, KTÓRYCH PILNUJĄ TE TESTY

1. Do jednej serii pisze DOKŁADNIE JEDEN z trzech procesów. Który — mówi
   `stan_kapitalu.KRZYWA_Z_KSIAG_GIELDY`, w jednym miejscu.
2. Punkt ZASIANY nie może być szczytem serii pisanej z ksiąg giełdy. To jest
   sedno szkody i dlatego ma osobny, odwrotny kanarek: dla serii pisanej przez
   pętlę zasiew MUSI wchodzić do szczytu, bo jest jej legalnym początkiem —
   wyrzucenie go zaniżałoby obsunięcie jednostronnie, czyli powtarzało błąd
   naprawiony w migracji 0042.
3. Wiersz sprzed migracji 0058 (`zrodlo IS NULL`) wchodzi do szczytu ZAWSZE.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from services.engine import stan_kapitalu
from services.engine.stan_kapitalu import (
    KRZYWA_Z_KSIAG_GIELDY, ZRODLA, ZRODLO_KSIEGI_GIELDY, ZRODLO_PETLA,
    ZRODLO_ZASIEW, zrodlo_obowiazujace,
)

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJA = KORZEN / "db" / "migrations" / "0058_zrodlo_punktu_krzywej.sql"


# ============================================================ DEKLARACJA
#
# Zbiór silników z krzywą z ksiąg jest DECYZJĄ, nie szczegółem implementacji.
# Testy niżej opisują ją wprost, żeby jej zmiana wymagała zmiany testu — a nie
# przeszła jako „poprawka przy okazji".

def test_dla_toru_equity_obowiazuja_KSIEGI_GIELDY():
    """Decyzja 2026-09-26, zapisana w jednym miejscu.

    Uzasadnienie (pełne w `stan_kapitalu.KRZYWA_Z_KSIAG_GIELDY`): księgi są
    jedynym źródłem, którego nie musimy zgadywać — konto paper Alpaki jest
    prawdziwym rachunkiem z wyceną liczoną po stronie giełdy. Dwie pozostałe
    definicje są rekonstrukcjami z naszego dziennika, a tor equity nie ma w nim
    ani jednego wypełnienia.
    """
    assert "equity" in KRZYWA_Z_KSIAG_GIELDY
    assert "crypto" not in KRZYWA_Z_KSIAG_GIELDY, (
        "crypto liczy krzywą SAM z własnych wypełnień od miesięcy — "
        "przestawienie go na księgi jest zmianą produkcji, nie porządkowaniem")
    assert zrodlo_obowiazujace("equity") == ZRODLO_KSIEGI_GIELDY
    assert zrodlo_obowiazujace("crypto") == ZRODLO_PETLA


def test_migracja_0058_dopuszcza_DOKLADNIE_te_zrodla_co_kod():
    """`CHECK` w bazie i `ZRODLA` w Pythonie to jedna lista w dwóch miejscach.

    Rozjazd byłby cichy w jedną stronę i głośny w drugą: wartość znana kodowi
    i nieznana bazie wywraca CAŁĄ transakcję cyklu przy pierwszym zapisie,
    a wartość znana bazie i nieznana kodowi jest martwą gałęzią, o której nikt
    się nie dowie.
    """
    assert MIGRACJA.exists(), f"brak {MIGRACJA.name}"
    tresc = MIGRACJA.read_text(encoding="utf-8")
    check = re.search(r"CHECK\s*\(zrodlo IS NULL\s*OR zrodlo IN \(([^)]*)\)",
                      tresc)
    assert check, "migracja nie ogranicza wartości kolumny `zrodlo`"
    w_bazie = set(re.findall(r"'([a-z_]+)'", check.group(1)))
    assert w_bazie == set(ZRODLA), (
        f"CHECK dopuszcza {sorted(w_bazie)}, a kod zna {sorted(ZRODLA)}")


def test_migracja_0058_NIE_usuwa_i_NIE_zgaduje_historii():
    """Kopia zapasowa nie jest częścią tej naprawy, więc nie może być potrzebna.

    Usunięcie wiersza krzywej jest zmianą HISTORII, od której liczą się szczyt
    i obsunięcie — krokiem nieodwracalnym, wymagającym kopii, której brak musi
    operację PRZERWAĆ. Ta migracja tego nie robi i nie ma prawa zacząć:
    naprawa mechanizmu jej nie potrzebuje.

    Dopisanie źródła wierszom istniejącym byłoby drugim wariantem tego samego
    błędu — zamienieniem niewiedzy w potwierdzenie po heurystyce.
    """
    tresc = MIGRACJA.read_text(encoding="utf-8")
    kod = "\n".join(l for l in tresc.splitlines() if not l.lstrip().startswith("--"))
    assert "DELETE" not in kod.upper(), "migracja usuwa wiersze krzywej kapitału"
    assert not re.search(r"UPDATE\s+exec\.equity_point", kod, re.I), (
        "migracja dopisuje źródło wierszom sprzed siebie, czyli je ZGADUJE")
    assert "SET NOT NULL" not in kod.upper() and "DEFAULT" not in kod.upper(), (
        "domyślne źródło wpisywałoby jednemu pisarzowi źródło drugiego przy "
        "pierwszym INSERCIE, który o kolumnie zapomni")


# ================================================== PISARZ 1: loop.seed_equity

class KursorZasiewu:
    """Atrapa dla `seed_equity`: pusta seria i zapamiętane instrukcje."""

    def __init__(self):
        self.sql: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return None                       # seria pusta — zasiew ma prawo wejść

    def wstawienia(self) -> list[tuple[str, tuple | None]]:
        return [(s, p) for s, p in self.sql if "INSERT INTO exec.equity_point" in s]


def test_seed_equity_NIE_zasiewa_serii_pisanej_z_ksiag():
    """KANAREK GŁÓWNY, warstwa pisarza.

    `seed_equity` stoi w `run_once` PRZED bramką `--wymagaj-modelu`, więc
    wykonywał się także wtedy, gdy silnik nie mógł zawrzeć ani jednej
    transakcji — i zasiewał `nav = 1.0` do serii, którą pisze `kapital_gieldy`.
    """
    from services.engine.loop import seed_equity

    c = KursorZasiewu()
    seed_equity(c, "equity", "demo")
    assert c.wstawienia() == [], (
        "pętla equity zasiała punkt krzywej, którą piszą księgi giełdy — "
        "seria dostała dwie definicje kapitału naraz")


def test_KANAREK_seed_equity_NADAL_zasiewa_crypto_i_podpisuje_zasiew():
    """Kanarek odwrotny: poprawka nie może wyłączyć zasiewu w ogóle.

    Bez tego test wyżej przechodziłby także dla `return` na pierwszej linii —
    czyli dla silnika crypto bez punktu zerowego krzywej, w którym obsunięcie
    i strata dzienna nie miałyby od czego się liczyć przez pierwszą dobę.
    """
    from services.engine.loop import seed_equity

    c = KursorZasiewu()
    seed_equity(c, "crypto", "demo")
    wstawienia = c.wstawienia()
    assert len(wstawienia) == 1, "crypto stracił punkt zerowy krzywej"
    sql, params = wstawienia[0]
    assert "zrodlo" in sql, "zasiew nie podpisuje się w kolumnie `zrodlo`"
    assert params[-1] == ZRODLO_ZASIEW, (
        f"zasiew podpisał się jako {params[-1]!r}, a `max(nav)` rozpoznaje "
        f"punkt zasiany WYŁĄCZNIE po tej wartości")


# ============================================== PISARZ 2: loop.update_equity

class KursorRekonstrukcji:
    """Atrapa dla `update_equity` — zna te zapytania, które ta funkcja robi."""

    def __init__(self):
        self.sql: list[tuple[str, tuple | None]] = []
        self._odp = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "AS realized FROM exec.position" in sql:
            self._odp = {"realized": Decimal("0")}
        elif "FROM exec.position p JOIN ref.instrument" in sql:
            self._odp = []
        elif "FROM exec.cash_flow" in sql:
            self._odp = {"wplaty": Decimal("0"), "units": Decimal("0")}
        else:
            self._odp = None

    def fetchone(self):
        return self._odp if isinstance(self._odp, dict) else None

    def fetchall(self):
        return self._odp if isinstance(self._odp, list) else []

    def wstawienia(self) -> list[tuple[str, tuple | None]]:
        return [(s, p) for s, p in self.sql if "INSERT INTO exec.equity_point" in s]


def test_update_equity_NIE_dopisuje_do_serii_pisanej_z_ksiag():
    """Trzeci pisarz też musi trafić w tę samą zasadę, a nie tylko drugi.

    Dziś `update_equity` dla equity milczy z innego powodu — bramka
    `--wymagaj-modelu` kończy cykl wcześniej. To nie jest zabezpieczenie,
    tylko okoliczność: bramka jest przełącznikiem chartu i można ją zdjąć
    jedną linią w `values.yaml`, nie dotykając kodu.
    """
    from services.engine.loop import update_equity

    c = KursorRekonstrukcji()
    update_equity(c, "equity", "demo", datetime.now(timezone.utc))
    assert c.wstawienia() == [], (
        "rekonstrukcja z naszego dziennika dopisała punkt do krzywej, "
        "którą piszą księgi giełdy")


def test_KANAREK_update_equity_NADAL_pisze_crypto_i_podpisuje_petle():
    from services.engine.loop import update_equity

    c = KursorRekonstrukcji()
    w = update_equity(c, "crypto", "demo", datetime.now(timezone.utc))
    wstawienia = c.wstawienia()
    assert len(wstawienia) == 1, "crypto stracił krzywą kapitału"
    assert wstawienia[0][1][-1] == ZRODLO_PETLA
    assert w is not None, "wycena musi wrócić do wołającego niezależnie od zapisu"


def test_wycena_wraca_takze_wtedy_gdy_punkt_nie_jest_zapisywany():
    """Pomijamy INSERT, nie pomiar.

    Wołający potrzebuje `Wycena` (ekspozycja per instrument, braki świeżych
    cen) do decyzji i do limitów — to nie jest krzywa kapitału i nie ma powodu
    tego gubić razem z zapisem.
    """
    from services.engine.loop import Wycena, update_equity

    c = KursorRekonstrukcji()
    w = update_equity(c, "equity", "demo", datetime.now(timezone.utc))
    assert isinstance(w, Wycena)


# ======================================= PISARZ 3: kapital_gieldy.zapisz_punkt

class KursorKsiag:
    def __init__(self):
        self.sql: list[tuple[str, tuple | None]] = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return {"wplaty": Decimal("0"), "units": Decimal("0")}

    def wstawienia(self) -> list[tuple[str, tuple | None]]:
        return [(s, p) for s, p in self.sql if "INSERT INTO exec.equity_point" in s]


STAN_KONTA = {"equity": Decimal("100000"), "cash": Decimal("100000"),
              "ekspozycja": Decimal("0")}


def test_zapisz_punkt_ODMAWIA_silnikowi_ktory_liczy_krzywa_sam():
    """Drugi koniec tej samej zasady.

    `--engine` przyjmuje dowolny napis bez `choices=`, więc `--engine crypto`
    wycelowałby ten proces w serię pisaną przez `loop.update_equity`.

    BŁĄD, A NIE CICHE POMINIĘCIE — w odróżnieniu od `seed_equity`. Ten proces
    został URUCHOMIONY po to, żeby pisać krzywą, więc cykl bez zapisu biłby
    puls i wyglądałby na udany, nie robiąc tego jednego, do czego istnieje.
    """
    from services.engine.kapital_gieldy import zapisz_punkt

    c = KursorKsiag()
    with pytest.raises(RuntimeError) as e:
        zapisz_punkt(c, "crypto", "demo", STAN_KONTA, Decimal("0"))
    assert "definicje kapitalu" in str(e.value) or "definicje kapitału" in str(e.value)
    assert c.wstawienia() == []


def test_KANAREK_zapisz_punkt_NADAL_pisze_equity_i_podpisuje_ksiegi():
    from services.engine.kapital_gieldy import zapisz_punkt

    c = KursorKsiag()
    zapisz_punkt(c, "equity", "demo", STAN_KONTA, Decimal("0"))
    wstawienia = c.wstawienia()
    assert len(wstawienia) == 1, "equity stracił krzywą kapitału"
    assert wstawienia[0][1][-1] == ZRODLO_KSIEGI_GIELDY


# ======================================== SZCZYT: sedno szkody z ustalenia 12
#
# Atrapa krzywej jest WSPÓLNA z `tests/test_swiezy_rachunek.py` (tam mieszka
# razem z kernelem) i odtwarza regułę `_ZASIEW_NIE_NALEZY`. Druga kopia
# rozjechałaby się przy pierwszej zmianie zapytania, a rozjazd byłby
# niewidoczny: atrapa, która nie zna zapytania, oddaje pustkę.

from tests.test_swiezy_rachunek import kernel as kernel_krzywej  # noqa: E402

LIMIT_DD = {"max_drawdown": {"id": 2, "value": Decimal("10"), "unit": "pct",
                             "action": "killswitch"}}


def _p(wiek_h, equity, nav, zrodlo):
    return {"wiek_h": wiek_h, "equity": Decimal(str(equity)),
            "nav": Decimal(str(nav)), "wplaty": Decimal("100000"),
            "zrodlo": zrodlo}


def _szczyt_nav(punkty) -> Decimal | None:
    k, _ = kernel_krzywej(punkty, limity=LIMIT_DD)
    _, szczyt, *_ = k._wynik()
    return szczyt


def test_szczyt_NIE_MOZE_byc_podniesiony_punktem_ZASIANYM():
    """TO JEST SEDNO SZKODY. Bez tego wszystko powyżej jest kosmetyką.

    Seria w miniaturze dokładnie taka jak `equity/demo`: jeden punkt zasiany
    z `nav = 1.0` (nikt go nie zmierzył) i odczyty z ksiąg giełdy, które mówią,
    że rachunek jest 8% pod wodą.

    Przed poprawką `max(nav)` — liczone BEZ ograniczenia czasowego — brało
    1.0 z zasiewu, więc obsunięcie wychodziło 8% i rosło dalej wraz z każdym
    gorszym odczytem, aż do progu 10% i kill-switcha, którego dla equity nie
    ma kto zwolnić. Po poprawce szczytem jest najwyższy punkt Z KSIĄG.
    """
    seria = [_p(100, 100_000, "1.0", ZRODLO_ZASIEW),
             _p(80, 95_000, "0.95", ZRODLO_KSIEGI_GIELDY),
             _p(0, 92_000, "0.92", ZRODLO_KSIEGI_GIELDY)]

    assert _szczyt_nav(seria) == Decimal("0.95"), (
        "szczytem serii pisanej z ksiąg giełdy został punkt ZASIANY — "
        "`nav = 1.0` wpisane na sztywno zostaje szczytem NA ZAWSZE, bo "
        "`max(nav)` nie ma ograniczenia czasowego")


def test_zasiew_nie_podnosi_szczytu_TAKZE_w_pieniadzach():
    """Ta sama szkoda w drugiej mierze.

    Gdy `nav` jest niepełne, reguły spadają na `equity` (`_nav_wiarygodne`),
    a `max(equity)` ma dokładnie ten sam brak ograniczenia czasowego. Zasiane
    100 000 byłoby wtedy trwałym szczytem w pieniądzach.
    """
    from services.engine.risk import RiskKernel  # noqa: F401  (czytelność importu)

    seria = [_p(100, 100_000, "1.0", ZRODLO_ZASIEW),
             _p(80, 95_000, "0.95", ZRODLO_KSIEGI_GIELDY),
             _p(0, 92_000, "0.92", ZRODLO_KSIEGI_GIELDY)]
    k, _ = kernel_krzywej(seria, limity=LIMIT_DD)
    _, peak, _ = k._equity()
    assert peak == Decimal("95000"), (
        f"szczyt w pieniądzach wyszedł {peak} — zasiane 100 000 zostało "
        f"trwałym szczytem serii, której nie zasiew zakłada")


def test_KANAREK_dla_serii_wlasnej_zasiew_NADAL_JEST_szczytem():
    """KANAREK ODWROTNY, i jest ważniejszy niż wygląda.

    Bez niego poprawka mogłaby brzmieć „wyrzucamy zasiewy ze szczytu" — a to
    dla silnika liczącego krzywą SAM byłoby błędem, i to jednostronnym.
    Tam zasiew jest legalnym POCZĄTKIEM krzywej (`units == wplaty == equity`
    w chwili zero, więc `nav = 1.0` jest prawdą z definicji). Wyrzucenie go
    obniża szczyt, a niższy szczyt to MNIEJSZE obsunięcie — nigdy większe.
    Dokładnie ten błąd naprawiała migracja 0042 i opisuje
    `RiskState._nav_wiarygodne`.
    """
    wlasna = [_p(100, 100_000, "1.0", ZRODLO_ZASIEW),
              _p(80, 95_000, "0.95", ZRODLO_PETLA),
              _p(0, 92_000, "0.92", ZRODLO_PETLA)]
    assert _szczyt_nav(wlasna) == Decimal("1.0"), (
        "zasiew przestał być szczytem serii, którą sam zakłada — obsunięcie "
        "rachunku, który od początku tylko tracił, liczy się teraz od punktu "
        "PO stracie i wychodzi zaniżone")


def test_wiersz_sprzed_migracji_0058_wchodzi_do_szczytu():
    """`zrodlo IS NULL` znaczy „nie wiemy", a nie „nie liczy się".

    To nie jest dbałość o szczegół: na produkcji `equity/demo` NULL mają
    WSZYSTKIE 367 punktów. Gdyby warunek napisać na `=` zamiast
    `IS NOT DISTINCT FROM`, `NULL = 'zasiew'` dałoby NULL, `FILTER` wyrzuciłby
    każdy taki wiersz, szczyt wyszedłby NULL i spadł na wartość bieżącą —
    czyli obsunięcie 0,00% ZAWSZE. Naprawa zamieniona w ciszę.
    """
    seria = [_p(100, 111_000, "1.11", None),          # sprzed migracji
             _p(80, 100_000, "1.0", ZRODLO_ZASIEW),
             _p(0, 92_000, "0.92", ZRODLO_KSIEGI_GIELDY)]
    assert _szczyt_nav(seria) == Decimal("1.11"), (
        "szczyt pominął wiersz sprzed migracji 0058 — obsunięcie liczone od "
        "okrojonej historii jest zawsze mniejsze albo równe prawdziwemu, "
        "czyli błąd jednostronny w stronę dalszego handlu (migracja 0042)")


def test_zapytania_o_szczyt_pytaja_o_NULL_bezpiecznie():
    """Sam napis w zapytaniu, bo to on decyduje o powyższym.

    `= 'zasiew'` i `IS NOT DISTINCT FROM 'zasiew'` różnią się WYŁĄCZNIE dla
    NULL, więc podmiana jednego na drugie przechodzi każdy test na danych
    z wypełnionym źródłem — i wywraca miarę na produkcji, gdzie NULL są
    wszystkie wiersze.
    """
    for nazwa in ("SQL_NAV_AGREGAT", "SQL_EQUITY_SZCZYT"):
        sql = " ".join(getattr(stan_kapitalu, nazwa).split())
        assert ZRODLO_ZASIEW in sql, f"{nazwa} nie odróżnia punktu zasianego"
        assert "IS NOT DISTINCT FROM '" + ZRODLO_ZASIEW + "'" in sql, (
            f"{nazwa} porównuje `zrodlo` przez `=`, więc dla wiersza sprzed "
            f"migracji 0058 (NULL) całe `NOT (...)` daje NULL i FILTER "
            f"wyrzuca ten wiersz ze szczytu")


def test_kazde_zapytanie_planu_przyjmuje_DOKLADNIE_engine_i_mode():
    """Niezmiennik `stan_kapitalu.PLAN` — dwa parametry, ani jednego więcej.

    `wznowienie.stan_konta` wykonuje CAŁY plan w pętli, podstawiając zawsze
    tę samą parę. Zapytanie z trzecim `%s` nie wywali się na typie — wywali
    się na liczbie parametrów, w procesie, który biegnie raz na dobę.
    """
    for klucz, sql in stan_kapitalu.PLAN:
        assert sql.count("%s") == 2, (
            f"{klucz}: {sql.count('%s')} parametrów, a plan podaje (engine, mode)")


# ===================================================== PRAWDZIWY POSTGRES
#
# Atrapa wyżej ODTWARZA regułę szczytu, więc nie jest dowodem na to, że
# zapytanie ją realizuje — dokładnie tak samo jak atrapa, która oddaje kolumnę,
# której SELECT nie pobiera. Ten jeden test pyta samego Postgresa.
#
#     docker run -d --name trader-krzywa-test -e POSTGRES_DB=trader \
#         -e POSTGRES_USER=trader -e POSTGRES_PASSWORD=trader_dev \
#         -p 55432:5432 timescale/timescaledb-ha:pg17
#     DATABASE_URL=postgresql://trader:trader_dev@localhost:55432/trader \
#         .venv/bin/python -B db/migrate.py
#     TEST_DB_URL=postgresql://trader:trader_dev@localhost:55432/trader \
#         .venv/bin/python -B -m pytest -q tests/test_jedna_definicja_kapitalu.py

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazową bazę z zastosowanymi migracjami")


@wymaga_bazy
def test_PRAWDZIWA_BAZA_szczyt_pomija_zasiew_tylko_w_serii_z_ksiag():
    """To samo co testy na atrapie, ale na prawdziwym Postgresie i w ROLLBACK.

    Sprawdza OBIE strony reguły oraz zachowanie dla `zrodlo IS NULL` — czyli
    to, czego atrapa nie może udowodnić, bo sama tę regułę zawiera.
    """
    import psycopg
    from psycopg.rows import dict_row

    wiersze = [
        # (mode, nav, equity, zrodlo)
        ("demo", "1.00", "100000", ZRODLO_ZASIEW),
        ("demo", "0.95", "95000", ZRODLO_KSIEGI_GIELDY),
        ("demo", "0.92", "92000", ZRODLO_KSIEGI_GIELDY),
        ("live", "1.00", "100000", ZRODLO_ZASIEW),
        ("live", "0.95", "95000", ZRODLO_PETLA),
    ]
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        cur = conn.cursor()
        for i, (tryb, nav, eq, zr) in enumerate(wiersze):
            cur.execute(
                "INSERT INTO exec.equity_point (engine, mode, event_time, equity,"
                " cash, unrealized, realized_cum, exposure, nav, units,"
                " wplaty_netto, zrodlo)"
                " VALUES ('equity',%s, now() - make_interval(hours => %s), %s, %s,"
                " 0, 0, 0, %s, 100000, 100000, %s)",
                (tryb, len(wiersze) - i, eq, eq, nav, zr))

        cur.execute(stan_kapitalu.SQL_NAV_AGREGAT, ("equity", "demo"))
        assert cur.fetchone()["szczyt"] == Decimal("0.95"), (
            "Postgres NIE realizuje reguły, którą odtwarza atrapa: punkt "
            "zasiany został szczytem serii pisanej z ksiąg")

        cur.execute(stan_kapitalu.SQL_NAV_AGREGAT, ("equity", "live"))
        assert cur.fetchone()["szczyt"] == Decimal("1.00"), (
            "zasiew przestał być szczytem serii, którą sam zakłada")

        # `zrodlo IS NULL` w serii, która MA pisarza z ksiąg.
        cur.execute(
            "INSERT INTO exec.equity_point (engine, mode, event_time, equity,"
            " cash, unrealized, realized_cum, exposure, nav, units,"
            " wplaty_netto, zrodlo)"
            " VALUES ('equity','demo', now() - interval '99 hours', 111000,"
            " 111000, 0, 0, 0, 1.11, 100000, 100000, NULL)")
        cur.execute(stan_kapitalu.SQL_NAV_AGREGAT, ("equity", "demo"))
        assert cur.fetchone()["szczyt"] == Decimal("1.11"), (
            "wiersz sprzed migracji 0058 wypadł ze szczytu — patrz "
            "test_wiersz_sprzed_migracji_0058_wchodzi_do_szczytu")

        cur.execute(stan_kapitalu.SQL_EQUITY_SZCZYT, ("equity", "demo"))
        assert cur.fetchone()["peak"] == Decimal("111000")
        conn.rollback()
