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

# Kazde zapytanie przyjmuje dokladnie (engine, mode).
SQL_EQUITY_BIEZ = ("SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
                   " ORDER BY event_time DESC LIMIT 1")
SQL_EQUITY_SZCZYT = ("SELECT max(equity) AS peak FROM exec.equity_point"
                     " WHERE engine=%s AND mode=%s")
SQL_EQUITY_DOBA = ("SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
                   " AND event_time <= now() - interval '24 hours'"
                   " ORDER BY event_time DESC LIMIT 1")
SQL_EQUITY_PIERWSZY = ("SELECT equity FROM exec.equity_point WHERE engine=%s AND mode=%s"
                       " ORDER BY event_time ASC LIMIT 1")

SQL_NAV_BIEZ = ("SELECT nav, wplaty_netto FROM exec.equity_point WHERE engine=%s AND mode=%s"
                "   AND nav IS NOT NULL ORDER BY event_time DESC LIMIT 1")
SQL_NAV_AGREGAT = ("SELECT max(nav) AS szczyt,"
                   "       count(*) FILTER (WHERE nav IS NULL) AS bez_nav"
                   "  FROM exec.equity_point WHERE engine=%s AND mode=%s")
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


def stan_ryzyka(wiersze: dict[str, dict | None]) -> RiskState | None:
    """Wiersze z PLAN -> `RiskState` w czesci dotyczacej krzywej kapitalu.

    Pozostale pola (ekspozycja, zlecenia, kill-switch) zostaja puste: ten stan
    sluzy do policzenia WYNIKU, a nie do wydania werdyktu o zleceniu.

    FALLBACKI SA TU, a nie u wolajacego. Brak punktu sprzed doby znaczy, ze
    cala krzywa jest mlodsza niz doba - odniesieniem jest wtedy jej POCZATEK.
    Spadek na punkt biezacy dawal `doba == wynik`, czyli strate dzienna 0,00%
    zawsze; to jest miara, ktora przestala reagowac na wejscie.
    """
    biez = wiersze.get("equity_biez") or {}
    if biez.get("equity") is None:
        return None
    equity = _dec(biez["equity"])

    szczyt = _dec((wiersze.get("equity_szczyt") or {}).get("peak")) or equity
    doba_row = wiersze.get("equity_doba") or wiersze.get("equity_pierwszy") or {}
    doba = _dec(doba_row.get("equity")) or equity

    nav_biez = wiersze.get("nav_biez") or {}
    nav = _dec(nav_biez.get("nav"))
    if nav is None:
        # Bez `nav` reguly spadaja na kapital - zachowanie sprzed migracji 0031.
        return RiskState(equity=equity, peak=szczyt, day_ago=doba,
                         exposure=Decimal(0), per_instrument={}, orders_last_hour=0)

    agr = wiersze.get("nav_agregat") or {}
    nav_szczyt = _dec(agr.get("szczyt")) or nav
    # None traktujemy jak luke: brak odpowiedzi o pelnosc nie jest odpowiedzia
    # "pelna". Patrz `RiskState._nav_wiarygodne`.
    od_poczatku = agr.get("bez_nav") == 0
    nav_doba_row = wiersze.get("nav_doba") or wiersze.get("nav_pierwszy") or {}
    nav_doba = _dec(nav_doba_row.get("nav")) or nav

    return RiskState(
        equity=equity, peak=szczyt, day_ago=doba,
        exposure=Decimal(0), per_instrument={}, orders_last_hour=0,
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
