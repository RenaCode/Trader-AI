"""Krzywa kapitalu: JEDEN komplet zapytan i JEDNA regula wyboru miary.

DLACZEGO TO JEST OSOBNY MODUL

Migracja 0031 rozdzielila dwa pojecia kapitalu (patrz `risk_rules.RiskState`):
`equity` to PIENIADZE na rachunku, `nav` to wartosc jednostki uczestnictwa.
Obsuniecie i strate dzienna liczy sie od `nav`, bo wplata nie jest zyskiem.

Rozdzielenie przeszlo do `RiskKernel`, ale NIE przeszlo do `wznowienie.py`.
Bezpiecznik zatrzymywal wiec silnik na jednej liczbie, a dozorca zdejmowal
hamulec na drugiej - i rozjazd byl JEDNOSTRONNY, zawsze w strone "zwolnij za
wczesnie". Scenariusz w liczbach, na regule sprzed poprawki:

    nav sprzed doby 1,000, teraz 0,975  ->  strata 2,5% >= limit 2%  ->  STOP
    w tej samej dobie wlasciciel doplaca 5% kapitalu (kapital.py)
    equity: 100 000 -> 105 000 x 0,975 = 102 375  ->  strata 2,4% < 2,5%

Przy histerezie 1% wystarczylo, zeby `warunek_ustal` zameldowalo ustanie
przyczyny w dniu, w ktorym rachunek naprawde tracil 2,5%. Zadne z ogniw nie
bylo bledne z osobna; bledna byla DRUGA KOPIA pomiaru.

Dlatego zapytania stoja tutaj jako stale, a wybor miary robi `zloz_stan()`
przez `RiskState.wynik*` - czyli przez te sama wlasnosc, ktorej uzywa
`evaluate_account`. Nowa regula (albo nowy fallback) zmienia sie w jednym
miejscu dla obu czytelnikow.

DLACZEGO PLAN ZAPYTAN, A NIE FUNKCJA CZYTAJACA BAZE: kernel czyta kursorem
synchronicznym, wznowienie asynchronicznym `fetch_one`. Wspolna moze byc wiec
TRESC zapytan i sklejenie wyniku, ale nie sposob ich wykonania.
"""
from __future__ import annotations

from decimal import Decimal

from services.engine.risk_rules import RiskState

# ===================================================================== ZRODLA
#
# KTO ZAPISAL PUNKT KRZYWEJ - bo do 2026-09-22 nie dalo sie tego dowiedziec,
# a od tego dnia do `exec.equity_point` pisza TRZY procesy z TRZEMA roznymi
# definicjami kapitalu:
#
#   'zasiew'        `loop.seed_equity` - STALA. Wpisuje START_EQUITY i `nav=1.0`
#                   na sztywno. Dla silnika, ktory krzywa liczy SAM, jest to
#                   prawda z definicji (units == wplaty == equity w chwili zero),
#                   wiec jest to legalny POCZATEK jego krzywej.
#   'petla_silnika' `loop.update_equity` - REKONSTRUKCJA z naszego dziennika:
#                   wplaty + zrealizowany + niezrealizowany z `exec.position`.
#   'ksiegi_gieldy' `kapital_gieldy.zapisz_punkt` - ODCZYT z ksiag gieldy:
#                   equity, gotowka i ekspozycja policzone przez gielde.
#
# Czytelnicy (`RiskKernel`, dozorca ryzyka, `wznowienie.stan_konta`) biora
# `max`, ostatni i sprzed doby BEZ pytania, kto zapisal - wiec zlanie dwoch
# definicji w jednej serii nie objawia sie bledem, tylko miara liczona z dwoch
# roznych rzeczy naraz. Kolumna `zrodlo` (migracja 0058) jest po to, zeby to
# pytanie w ogole dalo sie zadac.
#
# NULL ZNACZY "WIERSZ SPRZED MIGRACJI 0058, ZRODLA NIE ZNAMY" - i wchodzi do
# kazdej miary. Wykluczenie go byloby powtorzeniem bledu migracji 0031, ktora
# dodala kolumne bez danych: szczyt liczony od dnia migracji jest zawsze
# mniejszy albo rowny prawdziwemu, czyli obsuniecie wychodzi ZANIZONE.
ZRODLO_ZASIEW = "zasiew"
ZRODLO_PETLA = "petla_silnika"
ZRODLO_KSIEGI_GIELDY = "ksiegi_gieldy"
ZRODLA = (ZRODLO_ZASIEW, ZRODLO_PETLA, ZRODLO_KSIEGI_GIELDY)

# KTORA DEFINICJA KAPITALU OBOWIAZUJE KTORY SILNIK. JEDNO MIEJSCE.
#
# DECYZJA (2026-09-26): dla toru `equity` obowiazuja KSIEGI GIELDY.
#
# Uzasadnienie: to jedyne zrodlo, ktorego nie musimy zgadywac. Konto paper
# Alpaki jest prawdziwym rachunkiem z prawdziwa wycena - equity, gotowka
# i wartosc pozycji licza sie tam po stronie gieldy. Dwie pozostale definicje
# sa rekonstrukcjami z NASZEGO dziennika, a tor equity nie ma w nim ani jednego
# wypelnienia: `seed_equity` twierdzi `nav = 1.0`, czego nikt nie zmierzyl,
# a `update_equity` przeliczaloby w kolko te sama zgadnieta wartosc startowa.
#
# CENA TEJ DECYZJI, zeby nie byla ukryta:
#
#  1. ZGADYWANIE NIE ZNIKA, tylko sie zwęża. Ksiegi daja `equity`, nie daja
#     PRZEPLYWOW, wiec `units` i `wplaty_netto` nadal biora sie ze stalej
#     START_EQUITY (`kapital_gieldy.ZGADNIETA_ODCHYLKA_MAX` istnieje wlasnie po
#     to). `nav = equity / units` jest wiec tak dobre jak ta jedna liczba -
#     ale jest to JEDNA zgadnieta liczba, sprawdzana co cykl wobec ksiag,
#     zamiast trzech definicji, ktorych nikt nie porownywal. Zgadywanie znika
#     dopiero po zapisaniu rzeczywistego przeplywu w `exec.cash_flow` i to jest
#     osobna zmiana DANYCH produkcyjnych, nie kodu.
#  2. KRZYWA ZALEZY OD PROCESU, KTORY NIE JEST SILNIKIEM. Gdy `kapital-equity`
#     stanie, krzywa zamarza. Wykrywa to `risk.POMIAR_STARY_PO_MIN`, ale prog
#     15 min dobrano dla petli budzacej sie co 300 s ("trzy pominiete cykle"),
#     a `kapital-equity` chodzi co 900 s - dla equity jeden pominiety cykl
#     wystarczy, zeby pomiar byl "stary". To jest stan JUZ ISTNIEJACY (ksiegi
#     pisza co 900 s od 2026-09-22), nie skutek tej decyzji, ale ta decyzja
#     czyni go trwalym i dlatego jest tu nazwany. Osobna decyzja: prog wieku
#     pomiaru per silnik.
#
# DOPISANIE SILNIKA DO TEGO ZBIORU jest zmiana definicji kapitalu i wymaga
# odpowiedzi na jedno pytanie: kto od tej chwili pisze jego krzywa. Pilnuje
# tego `loop.seed_equity`, `loop.update_equity` i `kapital_gieldy.zapisz_punkt` -
# wszystkie trzy pytaja o ten zbior, zeby dwa pisarze nie mogli trafic do
# jednej serii.
KRZYWA_Z_KSIAG_GIELDY = frozenset({"equity"})


def zrodlo_obowiazujace(engine: str) -> str:
    """Ktore zrodlo pisze krzywe tego silnika. Odpowiedz jest JEDNA."""
    return ZRODLO_KSIEGI_GIELDY if engine in KRZYWA_Z_KSIAG_GIELDY else ZRODLO_PETLA


# SZCZYT NIE MOZE BYC PUNKTEM ZASIANYM W SERII, DO KTOREJ ZASIEW NIE NALEZY.
#
# To jest trwalsza polowa szkody z ustalenia 12 i ostrzejsza niz strata dzienna.
# `max(nav)` liczy sie BEZ OGRANICZENIA CZASOWEGO, po calej historii serii.
# Zasiane `nav = 1.0` - twierdzenie "rachunek jest dokladnie rowny wplatom",
# ktorego nikt nie zmierzyl - zostaje wiec szczytem NA ZAWSZE. Przy limicie
# `max_drawdown` 10% dla equity/demo konto paper ponizej 90% zgadnietej wplaty
# daje kill-switch, ktory NIE MIJA Z DOBA i nie mija nigdy, bo przyczyna jest
# arytmetyczna, a dla `engine='equity'` nie ma procesu zdolnego go zwolnic
# (dozorca chodzi z `--engine crypto`).
#
# REGULA, a nie "wyrzucamy zasiewy": punkt zasiany wchodzi do szczytu, DOPOKI
# seria nie ma pisarza z ksiag gieldy. Dla silnika, ktory krzywa liczy sam,
# zasiew jest jej legalnym poczatkiem i wyrzucenie go zanizaloby obsuniecie -
# czyli zrobiloby dokladnie ten blad jednostronny, przed ktorym broni
# `RiskState._nav_wiarygodne`. Dla serii pisanej z ksiag zasiew jest punktem
# z INNEJ definicji kapitalu i do szczytu nie ma prawa wejsc.
#
# DWIE WARSTWY, SWIADOMIE. `seed_equity` nie zasieje juz silnika z ksiag
# (warstwa pierwsza, pisarz), a gdyby taki punkt jednak powstal - recznie,
# ze starszego kodu, z przywroconej kopii - szczyt go nie zobaczy (warstwa
# druga, czytelnik). Jedna warstwa tu nie wystarcza, bo wada z ustalenia 12
# przezyla trzy tygodnie wlasnie jako punkt JUZ ZAPISANY.
# `IS NOT DISTINCT FROM`, NIE `=`. Dla wiersza sprzed migracji 0058 `zrodlo`
# jest NULL, a `NULL = 'zasiew'` daje NULL, wiec cale `NOT (...)` byloby NULL
# i `FILTER` WYRZUCILBY ten wiersz ze szczytu. Na produkcji equity/demo NULL
# maja wszystkie 367 punktow, wiec szczyt wyszedlby NULL, spadlby na wartosc
# biezaca i obsuniecie wynosiloby 0,00% ZAWSZE - naprawa zamieniona w cisze.
_ZASIEW_NIE_NALEZY = (
    " AND NOT (s.zrodlo IS NOT DISTINCT FROM '" + ZRODLO_ZASIEW + "'"
    "          AND k.z_ksiag)")
_SERIA = (
    "WITH s AS (SELECT nav, equity, zrodlo FROM exec.equity_point"
    "            WHERE engine=%s AND mode=%s),"
    "     k AS (SELECT EXISTS (SELECT 1 FROM s"
    "                           WHERE zrodlo = '" + ZRODLO_KSIEGI_GIELDY + "')"
    "                  AS z_ksiag)")

# Kazde zapytanie przyjmuje dokladnie (engine, mode).
SQL_EQUITY_BIEZ = ("SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
                   " ORDER BY event_time DESC LIMIT 1")
SQL_EQUITY_SZCZYT = (_SERIA + " SELECT max(s.equity) FILTER (WHERE true"
                     + _ZASIEW_NIE_NALEZY + ") AS peak"
                     " FROM s CROSS JOIN k")
SQL_EQUITY_DOBA = ("SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
                   " AND event_time <= now() - interval '24 hours'"
                   " ORDER BY event_time DESC LIMIT 1")
SQL_EQUITY_PIERWSZY = ("SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
                       " ORDER BY event_time ASC LIMIT 1")

SQL_NAV_BIEZ = ("SELECT nav, wplaty_netto FROM exec.equity_point WHERE engine=%s AND mode=%s"
                "   AND nav IS NOT NULL ORDER BY event_time DESC LIMIT 1")
SQL_NAV_AGREGAT = (_SERIA + " SELECT max(s.nav) FILTER (WHERE true"
                   + _ZASIEW_NIE_NALEZY + ") AS szczyt,"
                   "       count(*) FILTER (WHERE s.nav IS NULL) AS bez_nav"
                   "  FROM s CROSS JOIN k")
SQL_NAV_DOBA = ("SELECT nav FROM exec.equity_point WHERE engine=%s AND mode=%s"
                "   AND nav IS NOT NULL AND event_time <= now() - interval '24 hours'"
                " ORDER BY event_time DESC LIMIT 1")
SQL_NAV_PIERWSZY = ("SELECT nav FROM exec.equity_point WHERE engine=%s AND mode=%s"
                    "   AND nav IS NOT NULL ORDER BY event_time ASC LIMIT 1")

# Kolejnosc bez znaczenia - wolacz wykonuje komplet i podpisuje wyniki kluczami.
PLAN = (
    ("equity_biez", SQL_EQUITY_BIEZ),
    ("equity_szczyt", SQL_EQUITY_SZCZYT),
    ("equity_doba", SQL_EQUITY_DOBA),
    ("equity_pierwszy", SQL_EQUITY_PIERWSZY),
    ("nav_biez", SQL_NAV_BIEZ),
    ("nav_agregat", SQL_NAV_AGREGAT),
    ("nav_doba", SQL_NAV_DOBA),
    ("nav_pierwszy", SQL_NAV_PIERWSZY),
)


def _dec(wartosc) -> Decimal | None:
    return None if wartosc is None else Decimal(str(wartosc))


def _albo(zmierzone: Decimal | None, gdy_brak: Decimal) -> Decimal:
    """Pomiar, a gdy go NIE BYLO - wartosc zastepcza. ZERO JEST POMIAREM.

    Stalo tu cztery razy `_dec(...) or equity` / `or nav`. `Decimal(0)` jest
    falsy, wiec ZMIERZONE zero bylo podmieniane wartoscia biezaca - dokladnie
    ten blad, ktory `RiskState.exposure` usunal swiadomie i z uzasadnieniem:
    zero jest poprawna zmierzona wartoscia, wiec nie da sie go odroznic od
    braku pomiaru.

    KIERUNEK AWARII JEST OTWARTY, nie zachowawczy, i to jest cala tresc tej
    funkcji. Gdyby `doba` wynioslo 0 i zostalo podmienione na `equity`, strata
    dzienna wyliczylaby sie jako (equity - equity) / equity = 0,00% - czyli
    reguła `max_daily_loss` NIE ZAPALILABY SIE w dobie, w ktorej rachunek
    zostal wyzerowany. Tak samo `nav_doba`.

    Osiagalnosc jest bliska zeru (equity = 0 to wyzerowany rachunek), ale
    zostawienie czterech kopii wzorca, ktory regula tego projektu nazwala
    bledem, zaprasza do skopiowania go dalej - a nastepny raz moze byc
    w miejscu, w ktorym zero jest zwyczajne.
    """
    return gdy_brak if zmierzone is None else zmierzone


def stan_ryzyka(wiersze: dict[str, dict | None]) -> RiskState | None:
    """Wiersze z PLAN -> `RiskState` w czesci dotyczacej krzywej kapitalu.

    Pozostale pola (ekspozycja, zlecenia, kill-switch) zostaja puste: ten stan
    sluzy do policzenia WYNIKU, a nie do wydania werdyktu o zleceniu.

    Ekspozycja idzie jako `None`, nie `Decimal(0)` - patrz `RiskState`. Zero
    bylo tu nieprawda, ktorej nie da sie odroznic od pomiaru: gdyby ktos
    kiedykolwiek podal ten stan do `evaluate_limits`, `max_total_exposure`
    przepuscilby kazde zlecenie, milczac. Teraz taki wolacz dostaje `ValueError`.

    FALLBACKI SA TU, a nie u wolajacego. Brak punktu sprzed doby znaczy, ze
    cala krzywa jest mlodsza niz doba - odniesieniem jest wtedy jej POCZATEK.
    Spadek na punkt biezacy dawal `doba == wynik`, czyli strate dzienna 0,00%
    zawsze; to jest miara, ktora przestala reagowac na wejscie.
    """
    biez = wiersze.get("equity_biez") or {}
    if biez.get("equity") is None:
        return None
    equity = _dec(biez["equity"])

    szczyt = _albo(_dec((wiersze.get("equity_szczyt") or {}).get("peak")), equity)
    doba_row = wiersze.get("equity_doba") or wiersze.get("equity_pierwszy") or {}
    doba = _albo(_dec(doba_row.get("equity")), equity)

    nav_biez = wiersze.get("nav_biez") or {}
    nav = _dec(nav_biez.get("nav"))
    if nav is None:
        # Bez `nav` reguly spadaja na kapital - zachowanie sprzed migracji 0031.
        return RiskState(equity=equity, peak=szczyt, day_ago=doba,
                         exposure=None, per_instrument=None, orders_last_hour=0)

    agr = wiersze.get("nav_agregat") or {}
    nav_szczyt = _albo(_dec(agr.get("szczyt")), nav)
    # None traktujemy jak luke: brak odpowiedzi o pelnosc nie jest odpowiedzia
    # "pelna". Patrz `RiskState._nav_wiarygodne`.
    od_poczatku = agr.get("bez_nav") == 0
    nav_doba_row = wiersze.get("nav_doba") or wiersze.get("nav_pierwszy") or {}
    nav_doba = _albo(_dec(nav_doba_row.get("nav")), nav)

    return RiskState(
        equity=equity, peak=szczyt, day_ago=doba,
        exposure=None, per_instrument=None, orders_last_hour=0,
        nav=nav, nav_peak=nav_szczyt, nav_day_ago=nav_doba,
        wplaty_netto=_dec(nav_biez.get("wplaty_netto")),
        nav_od_poczatku=od_poczatku)


def zloz_stan(wiersze: dict[str, dict | None]) -> dict | None:
    """Trzy liczby, ktorymi mierzy sie WYNIK - dokladnie te, ktore widzi kernel.

    `jednostka` mowi, ktora miara wygrala. Bez tego pola opis w dzienniku
    zwolnienia ("strata dzienna 2,40%") nie pozwala odroznic procentu liczonego
    z `nav` od procentu liczonego z `equity` - a to wlasnie ta roznica potrafi
    zdjac hamulec dzien za wczesnie.
    """
    st = stan_ryzyka(wiersze)
    if st is None:
        return None
    return {"wynik": float(st.wynik),
            "szczyt": float(st.wynik_szczyt),
            "doba": float(st.wynik_doba),
            "jednostka": st.wynik_w_jednostkach}
