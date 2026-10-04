"""Wpłaty i wypłaty — jedyna droga, którą pieniądze wchodzą i wychodzą.

DLACZEGO OSOBNY BYT, A NIE KOLUMNA

Do 2026-09-07 kapitał był stałą plus wynik handlu, więc pojęcia wpłaty nie było
w ogóle. Dołożenie środków musiałoby wtedy zmienić `START_EQUITY` — czyli
liczbę, na której mierzono cały dotychczasowy wynik, i to wstecz.

Przepływ zapisany osobno rozdziela dwie rzeczy, które ta stała mieszała:
ile pieniędzy jest na rachunku (`equity`) od tego, jak radzi sobie strategia
(`nav`). Obsunięcie i strata dzienna liczą się z drugiego, więc przelew nie
udaje wyniku.

JEDNOSTKI UCZESTNICTWA

Wpłata kupuje jednostki po bieżącym `nav`, wypłata je umarza. `nav` przy tym
się nie rusza — dokładnie tak, jak fundusz liczy wynik niezależnie od napływów.
Liczbę jednostek zapisujemy w chwili przepływu, bo późniejsze odtworzenie
wymagałoby znajomości `nav` sprzed miesięcy.

PRZEPŁYW TO ZMIANA RYZYKA (przegląd 2026-10-04, T-7)

Kapitał crypto/demo liczy się z przepływów (host demo nie wystawia sald), więc
wpis wpłaty podnosi KAŻDY limit liczony od kapitału: łączną ekspozycję, nominał
pozycji, budżet momentum. Do tej poprawki wystarczała sama sesja: wpłata
900 000 na rachunku ~100 000 dawała pozycje po ~50 000, czyli połowę realnego
rachunku na jedną pozycję. Teraz: hasło (licznik prób wspólny z logowaniem),
sufit wpłaty równy bieżącemu kapitałowi, tryb wyłącznie `demo` i alarm
krytyczny do właściciela.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import audit, current_user, require_csrf, wymagaj_wlasciciela
from ..db import execute, fetch_all, fetch_one
from ..notifications import powiadom_o_bezpieczenstwie
from ..typy import Silnik, Tryb
from .auth import potwierdz_haslo

router = APIRouter(prefix="/api/engines/{engine}/kapital", tags=["kapital"])


class PrzeplywBody(BaseModel):
    # Dodatnia = wpłata, ujemna = wypłata. Jedno pole zamiast kwoty i typu:
    # dwa pola, które muszą się zgadzać, prędzej czy później się rozjadą.
    amount: Decimal = Field(gt=Decimal("-1e12"), lt=Decimal("1e12"),
                            description="dodatnia = wpłata, ujemna = wypłata")
    # Wylacznie `demo`. Wpis `live` dzis niczego nie uruchamia, ale wylacza
    # bezpiecznik `BrakKapitalu` (loop.py) dla przyszlego trybu live - `live`
    # wejdzie tu razem z trybem live i osobna decyzja.
    mode: Literal["demo"] = "demo"
    note: str = Field(min_length=3, max_length=300)
    # HASLO, BO PRZEPLYW ZMIENIA WIELKOSC POZYCJI - jak przy zwolnieniu
    # kill-switcha (risk.py).
    haslo: str = Field(min_length=1, max_length=1024)


@router.get("/przeplywy")
async def przeplywy(engine: Silnik, mode: Tryb = Query("demo"),
                     limit: int = Query(50, le=500)):
    rows = await fetch_all(
        "SELECT c.id, c.event_time, c.amount, c.units, c.nav, c.note, u.login AS kto"
        " FROM exec.cash_flow c LEFT JOIN app.user u ON u.id = c.created_by"
        " WHERE c.engine::text = %s AND c.mode::text = %s"
        " ORDER BY c.event_time DESC LIMIT %s", (engine, mode, limit))
    return [
        {"id": r["id"], "at": r["event_time"].isoformat(),
         "amount": float(r["amount"]), "units": float(r["units"]),
         "nav": float(r["nav"]), "note": r["note"], "kto": r["kto"],
         "rodzaj": "wpłata" if r["amount"] > 0 else "wypłata"}
        for r in rows
    ]


@router.post("/przeplywy",
             dependencies=[Depends(require_csrf), Depends(wymagaj_wlasciciela)])
async def dodaj_przeplyw(engine: Silnik, body: PrzeplywBody, request: Request,
                         user: dict = Depends(current_user)):
    """Zapisuje wpłatę albo wypłatę i przelicza jednostki po bieżącym nav.

    HASLO PRZED CZYMKOLWIEK INNYM, jak w `risk.zwolnij_killswitch`: 403, nie
    401 (front na 401 wylogowuje, a sesja jest wazna).
    """
    await potwierdz_haslo(user, body.haslo, request, akcja="kapital.przeplyw",
                          status=403, komunikat="Hasło jest nieprawidłowe")
    if body.amount == 0:
        raise HTTPException(400, "Kwota zerowa nie jest przepływem")

    punkt = await fetch_one(
        "SELECT nav, equity FROM exec.equity_point"
        " WHERE engine::text = %s AND mode::text = %s AND nav IS NOT NULL"
        " ORDER BY event_time DESC LIMIT 1", (engine, body.mode))
    # Pierwszy przepływ na rachunku, którego krzywej jeszcze nie policzono:
    # jednostka warta jeden, bo nie było czego wycenić.
    nav = Decimal(punkt["nav"]) if punkt else Decimal(1)
    if nav <= 0:
        raise HTTPException(409, "Wartość jednostki nie jest dodatnia — "
                                 "przepływ zapisany teraz byłby bez sensu")

    stan = await fetch_one(
        "SELECT COALESCE(sum(amount), 0) AS wplaty FROM exec.cash_flow"
        " WHERE engine::text = %s AND mode::text = %s", (engine, body.mode))
    wplaty = Decimal(stan["wplaty"] or 0)
    kapital = Decimal(punkt["equity"]) if punkt else wplaty

    # WYPŁATA NIE MOŻE PRZEKROCZYĆ TEGO, CO JEST.
    #
    # Rachunek ujemny nie jest stanem, z którego system potrafi wyjść: wszystkie
    # miary procentowe dzielą przez kapitał, a `nav` przestaje mieć znaczenie.
    # Lepiej odmówić zapisu niż policzyć obsunięcie z liczby ujemnej.
    if body.amount < 0 and -body.amount > kapital:
        raise HTTPException(
            400, f"Wypłata {-body.amount:.2f} przekracza kapitał {kapital:.2f}")

    # WPŁATA NAJWYŻEJ DO PODWOJENIA KAPITAŁU (T-7). Pierwsza wpłata na
    # rachunku bez przepływów nie ma sufitu - to jest zasilenie startowe, nie
    # ma od czego go liczyć. Większe zasilenie wymaga dwóch kroków, czyli dwóch
    # haseł i dwóch alarmów.
    if body.amount > 0 and wplaty > 0 and body.amount > kapital:
        raise HTTPException(
            400, f"Wpłata {body.amount:.2f} przekracza bieżący kapitał {kapital:.2f}; "
                 f"większe zasilenie zapisz w dwóch krokach z uzasadnieniem")

    units = body.amount / nav
    await execute(
        "INSERT INTO exec.cash_flow (engine, mode, amount, units, nav, note, created_by)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (engine, body.mode, body.amount, units, nav, body.note.strip(), user["id"]))

    await audit("kapital.przeplyw", user_id=user["id"], entity="exec.cash_flow",
                before={"wplaty_netto": float(wplaty), "nav": float(nav)},
                after={"amount": float(body.amount), "units": float(units),
                       "note": body.note.strip()}, request=request)

    # ALARM KRYTYCZNY: zmiana kapitalu zmienia wielkosc kazdej kolejnej pozycji.
    # W tle, jak przy kill-switchu - martwy kanal nie wydluza odpowiedzi.
    ip = request.client.host if request is not None and request.client else "?"
    rodzaj = "Wpłata" if body.amount > 0 else "Wypłata"
    netto = wplaty + body.amount
    for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
        await powiadom_o_bezpieczenstwie(
            u["id"], "kapital.przeplyw",
            f"{rodzaj} {abs(body.amount):.2f} zapisana ({engine}/{body.mode})",
            f"Zapisał: {user['login']} (IP {ip}).\n\nKwota: {body.amount:.2f}\n"
            f"Wpłaty netto po zapisie: {netto:.2f}\n\nNotatka: {body.note.strip()}\n\n"
            f"Limity i wielkość pozycji liczą się od kapitału, więc zmieniają się "
            f"od najbliższego cyklu.",
            engine=engine, severity="critical",
            data={"kto": user["login"], "amount": float(body.amount),
                  "wplaty_netto": float(netto), "mode": body.mode})

    return {"ok": True, "units": float(units), "nav": float(nav),
            "wplaty_netto": float(netto),
            "message": ("Zapisano. Obsunięcie i strata dzienna liczą się z wartości "
                        "jednostki, więc ten przelew ich nie ruszy.")}
