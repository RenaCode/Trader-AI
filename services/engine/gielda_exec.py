"""Egzekucja na giełdzie: składanie planów i rozstrzyganie ich z giełdy.

CO SIĘ ZMIENIA WOBEC RACHUNKU PAPIEROWEGO

Rachunek papierowy rozstrzyga plan patrząc na świecę: jeśli cena dotknęła
limitu, uznaje zlecenie za wypełnione w całości. To założenie jest optymistyczne
w dwie strony naraz — zakłada wypełnienie, którego mogło nie być, i zakłada
całość tam, gdzie weszła część.

Tutaj rozstrzyga GIEŁDA. Pytamy ją o stan zlecenia po NASZYM identyfikatorze
i przyjmujemy to, co odpowie: ilość, cenę i prowizję. Żadna z tych trzech liczb
nie jest już naszym założeniem.

DLACZEGO IDENTYFIKATOR JEST WYPROWADZANY Z PLANU

`tai-p{id planu}` jest deterministyczny, więc ponowne złożenie tego samego planu
trafia w to samo zlecenie zamiast tworzyć drugie. Identyfikator z czasu albo
losowy zniósłby całą ochronę: przy każdej próbie powstawałoby nowe zlecenie,
a dwa zlecenia to podwójna pozycja — objaw nie wyglądający na awarię, tylko na
strategię, która weszła mocniej, niż zakładał risk kernel.

CZEGO TEN MODUŁ NIE ROBI

Nie decyduje, czy wejść. Decyzja jest już podjęta i zapisana w dzienniku, zanim
cokolwiek tutaj się wydarzy. Ten moduł wykonuje ją i wiernie zapisuje, co z niej
wyszło — łącznie z tym, że nie wyszło nic.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from services.engine.egzekucja import co_z_wypelnieniem
from services.engine.loop import zapisz_wejscie, zapisz_wyjscie
from services.engine.zlecenia import Filtry, sprawdz_zlecenie
from services.exchanges.base import ExchangeError


def filtry_z_bazy(cur, engine: str) -> dict[int, Filtry]:
    """Reguły giełdy per instrument, wprost z `ref.instrument`.

    Brak reguły zostaje None i blokuje zlecenie na tym instrumencie. Zamiana go
    na wartość domyślną byłaby zgadywaniem cudzej reguły — a odrzucone zlecenie
    wygląda w nocnym logu jak brak sygnału.
    """
    cur.execute(
        "SELECT id, lot_size, tick_size, min_notional, (meta->>'min_ilosc') AS min_ilosc"
        " FROM ref.instrument WHERE engine::text = %s AND active", (engine,))
    out = {}
    for r in cur.fetchall():
        out[r["id"]] = Filtry(
            krok_ilosci=r["lot_size"], krok_ceny=r["tick_size"],
            min_wartosc=r["min_notional"],
            min_ilosc=Decimal(r["min_ilosc"]) if r["min_ilosc"] else None)
    return out


def id_zlecenia(plan_id: int) -> str:
    """Nasz identyfikator zlecenia. Z PLANU, nie z czasu i nie losowy."""
    return f"tai-p{plan_id}"


async def zloz_plan(cur, adapter, plan: dict, f: Filtry, *, engine: str,
                    mode: str, kategoria: str) -> tuple[bool, str]:
    """Składa jeden plan na giełdzie. Zwraca (czy_zlozono, komunikat).

    ODMOWA ZŁOŻENIA JEST ZAPISYWANA, nie pomijana. Plan, który nie poszedł na
    giełdę, musi mieć w dzienniku powód — inaczej jedyną różnicą między
    „giełda odrzuciła" a „nikt nie próbował" jest cisza.
    """
    wyjscie = plan["trigger"]["rodzaj"] == "wyjscie"
    kupno = plan["side"] == "buy"
    ilosc, cena, powod = sprawdz_zlecenie(
        Decimal(plan["qty"]), Decimal(plan["limit_price"]), f,
        kupno=kupno, wyjscie=wyjscie)
    if powod:
        cur.execute("UPDATE journal.planned_order SET status='rejected',"
                    " resolved_at=now() WHERE id=%s", (plan["id"],))
        cur.execute(
            "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
            " instrument_id, client_order_id, side, order_type, qty, limit_price,"
            " status, reject_reason, submitted_at, raw)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,'rejected',%s,now(),%s)",
            (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
             id_zlecenia(plan["id"]), plan["side"], plan["qty"], plan["limit_price"],
             powod, json.dumps({"execution": "gielda", "etap": "przed_zlozeniem"})))
        return False, powod

    try:
        z = await adapter.zloz_zlecenie(
            symbol=plan["symbol"], strona="Buy" if kupno else "Sell",
            ilosc=ilosc, cena_limitu=cena, id_wlasne=id_zlecenia(plan["id"]),
            kategoria=kategoria)
    except ExchangeError as e:
        # NIE OZNACZAMY PLANU JAKO ODRZUCONEGO. Zlecenie moglo wejsc mimo bledu
        # odpowiedzi; rozstrzygniecie w nastepnym cyklu zapyta gielde po naszym
        # identyfikatorze i dowie sie prawdy. Oznaczenie go teraz odrzuconym
        # byloby zapisaniem nieprawdy o stanie, ktorego nie znamy.
        cur.execute(
            "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
            " instrument_id, client_order_id, side, order_type, qty, limit_price,"
            " status, reject_reason, submitted_at, raw)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,'unknown',%s,now(),%s)",
            (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
             id_zlecenia(plan["id"]), plan["side"], ilosc, cena, str(e),
             json.dumps({"execution": "gielda", "etap": "odpowiedz_nie_dotarla"})))
        return False, f"odpowiedź nie dotarła ({e}) — stan sprawdzimy po naszym id"

    cur.execute(
        "INSERT INTO exec.order (decision_id, planned_order_id, engine, mode,"
        " instrument_id, venue_order_id, client_order_id, side, order_type, qty,"
        " limit_price, status, submitted_at, raw)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'limit',%s,%s,%s,now(),%s)",
        (plan["decision_id"], plan["id"], engine, mode, plan["instrument_id"],
         z.id_gieldy, z.id_wlasne, plan["side"], ilosc, cena, z.status,
         json.dumps({"execution": "gielda", "juz_istnialo": z.juz_istnialo})))
    return True, ("odnalezione u giełdy (idempotencja)" if z.juz_istnialo
                  else f"złożone {ilosc} @ {cena}")


async def rozstrzygnij_plan(cur, adapter, plan: dict, f: Filtry, *, engine: str,
                            mode: str, kategoria: str, teraz: datetime,
                            kind: str) -> tuple[str, str]:
    """Pyta giełdę o los planu i zapisuje wynik. Zwraca (rodzaj, komunikat)."""
    id_w = id_zlecenia(plan["id"])
    stan = await adapter.stan_zlecenia(id_w, kategoria=kategoria)

    if stan is None:
        # GIELDA NIE ZNA TEGO ZLECENIA. Skoro probowalismy je zlozyc, a ona go
        # nie zna, to nie weszlo - i to jest jedyna bezpieczna interpretacja.
        cur.execute("UPDATE journal.planned_order SET status='expired',"
                    " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
        return "brak", "giełda nie zna tego zlecenia — plan wygasa"

    r = co_z_wypelnieniem(stan.wypelniono, Decimal(plan["qty"]),
                          Decimal(plan["limit_price"]), f)

    if r.anuluj_reszte and stan.status not in ("filled", "cancelled", "rejected"):
        try:
            await adapter.anuluj_zlecenie(symbol=plan["symbol"], id_wlasne=id_w,
                                          kategoria=kategoria)
        except ExchangeError as e:
            # Nieudane anulowanie NIE UNIEWAZNIA reszty zapisu. Zlecenie zostaje
            # otwarte na gieldzie i wylapie je uzgodnienie - a udawanie, ze
            # rozstrzygniecia nie bylo, zgubiloby wypelnienie, ktore JUZ jest nasze.
            r = type(r)(r.rodzaj, r.ilosc, r.anuluj_reszte, r.ulamek,
                        r.powod + f"; UWAGA: anulowanie reszty nie powiodło się ({e})")

    if r.rodzaj == "brak":
        cur.execute("UPDATE journal.planned_order SET status='expired',"
                    " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
        return r.rodzaj, r.powod

    cena = stan.srednia_cena or Decimal(plan["limit_price"])
    prowizja = stan.prowizja
    meta = {"execution": "gielda", "kind": kind, "order": "limit",
            "ulamek_wypelnienia": str(r.ulamek), "rodzaj": r.rodzaj,
            "status_gieldy": stan.status, "venue_order_id": stan.id_gieldy}

    if plan["trigger"]["rodzaj"] == "wyjscie":
        cur.execute("SELECT id, side, qty, avg_price FROM exec.position"
                    " WHERE id=%s AND closed_at IS NULL",
                    (plan["trigger"].get("position_id"),))
        poz = cur.fetchone()
        if poz:
            zapisz_wyjscie(cur, poz, cena, prowizja, teraz, plan["decision_id"],
                           ilosc=r.ilosc)
    else:
        zapisz_wejscie(cur, engine, mode, plan["instrument_id"], plan["decision_id"],
                       plan["side"], r.ilosc, cena, prowizja, teraz,
                       plan["quote_ccy"], meta, planned_order_id=plan["id"],
                       venue_order_id=stan.id_gieldy, client_order_id=id_w,
                       venue_fill_id=f"{stan.id_gieldy}")

    cur.execute("UPDATE journal.planned_order SET status='triggered',"
                " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
    cur.execute("UPDATE exec.order SET status=%s, updated_at=%s, raw = raw || %s"
                " WHERE planned_order_id=%s",
                (stan.status, teraz, json.dumps(meta), plan["id"]))
    return r.rodzaj, r.powod
