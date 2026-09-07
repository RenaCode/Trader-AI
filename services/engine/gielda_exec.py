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
            symbol=plan["symbol"], strona=plan["side"],
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

    # WIERSZ ZLECENIA JUZ ISTNIEJE - powstal przy zlozeniu. Bez tego odczytu
    # `zapisz_wejscie` zalozylby drugi i jeden plan mialby w exec.order dwa
    # zlecenia: tabela przestalaby znaczyc "jedno zlecenie", a czujka zdrowia
    # egzekucji widzialaby dwa razy wiecej przyjec, niz bylo.
    cur.execute("SELECT id, qty FROM exec.order WHERE planned_order_id = %s"
                " ORDER BY id LIMIT 1", (plan["id"],))
    wiersz = cur.fetchone()
    id_zlecenia_w_bazie = wiersz["id"] if wiersz else None

    # ULAMEK LICZYMY OD ILOSCI FAKTYCZNIE WYSLANEJ, nie od ilosci z planu.
    #
    # Plan trzyma ilosc SPRZED zaokraglenia do kroku gieldy, a na gielde poszla
    # ilosc zaokraglona w dol. Gielda nigdy nie odda wiecej, niz zamowiono, wiec
    # warunek "wypelniono >= planowano" nie bylby spelniony NIGDY - kazde pelne
    # wypelnienie zapisywaloby sie jako czesciowe.
    #
    # Skutki tego byly trzy i kazdy inny: zbedne anulowanie przy kazdym wejsciu,
    # wyjscie z calej pozycji zostawiajace pylek (bo zapisz_wyjscie dostawaloby
    # ilosc mniejsza niz qty i nie zamykalo wiersza), oraz systematycznie
    # falszywy `ulamek_wypelnienia` - czyli jedyna deklarowana miara rozjazdu
    # backtestu z produkcja.
    wyslano = Decimal(wiersz["qty"]) if wiersz else Decimal(plan["qty"])

    r = co_z_wypelnieniem(stan.wypelniono, wyslano,
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
                       venue_fill_id=f"{stan.id_gieldy}",
                       order_id=id_zlecenia_w_bazie)

    cur.execute("UPDATE journal.planned_order SET status='triggered',"
                " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
    cur.execute("UPDATE exec.order SET status=%s, updated_at=%s, raw = raw || %s"
                " WHERE planned_order_id=%s",
                (stan.status, teraz, json.dumps(meta), plan["id"]))

    return r.rodzaj, r.powod


async def zloz_oczekujace(cur, adapter, *, engine: str, mode: str,
                          kategoria: str, verbose: bool = True,
                          tylko_wyjscia: bool = False) -> dict:
    """Wysyła na giełdę plany, które jeszcze tam nie poszły.

    ROZPOZNANIE PO BRAKU WIERSZA ZLECENIA, nie po fladze na planie. Flaga
    mogłaby skłamać: proces mógł ją ustawić i paść przed wysłaniem albo wysłać
    i paść przed jej ustawieniem. Wiersz w `exec.order` powstaje w tej samej
    transakcji co wysłanie, więc jego brak znaczy dokładnie „nie próbowaliśmy".

    A gdyby mimo to zlecenie u giełdy było — złożenie jest idempotentne po
    naszym identyfikatorze i po prostu je odnajdzie.

    `tylko_wyjscia` obsługuje stan po zadziałaniu kill-switcha: nowych pozycji
    nie otwieramy, ale zamykanie istniejących MUSI iść dalej. Zatrzymanie
    wszystkiego bez wyjątku uwięziłoby pozycje w chwili, w której system właśnie
    stwierdził, że ryzyko jest za duże.
    """
    st = {"zlozone": 0, "odrzucone": 0}
    filtry = filtry_z_bazy(cur, engine)
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy"
        "  FROM journal.planned_order p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.status = 'pending'"
        "   AND NOT EXISTS (SELECT 1 FROM exec.order o WHERE o.planned_order_id = p.id)"
        "   AND (NOT %s OR p.trigger->>'rodzaj' = 'wyjscie')"
        " ORDER BY p.id", (tylko_wyjscia,))
    for plan in cur.fetchall():
        f = filtry.get(plan["instrument_id"], Filtry())
        ok, msg = await zloz_plan(cur, adapter, plan, f, engine=engine,
                                  mode=mode, kategoria=kategoria)
        st["zlozone" if ok else "odrzucone"] += 1
        if verbose:
            print(f"  [{'złożone' if ok else 'ODRZUCONE'}] {plan['symbol']} "
                  f"{plan['side']} — {msg}")
    return st


async def rozstrzygnij_oczekujace(cur, adapter, *, engine: str, mode: str,
                                  h, now, kategoria: str, kind: str,
                                  verbose: bool = True) -> dict:
    """Pyta giełdę o los planów, którym minęła świeca rozstrzygająca.

    TERMIN BIERZEMY Z TEJ SAMEJ FUNKCJI CO RACHUNEK PAPIEROWY. Gdyby jedna
    ścieżka pytała wcześniej, backtest i produkcja oglądałyby inny wycinek
    świecy — a różnica byłaby niewidoczna, bo obie wersje wyglądałyby poprawnie.
    """
    from services.engine.loop import termin_rozstrzygniecia

    st = {"wypelnione": 0, "niewypelnione": 0, "czekaja": 0, "czesciowe": 0,
          "nie_do_zamkniecia": 0}
    filtry = filtry_z_bazy(cur, engine)
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy"
        "  FROM journal.planned_order p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.status = 'pending' ORDER BY p.id")
    for plan in cur.fetchall():
        _, czekamy = termin_rozstrzygniecia(plan, h, now)
        if czekamy:
            st["czekaja"] += 1
            continue
        f = filtry.get(plan["instrument_id"], Filtry())
        rodzaj, powod = await rozstrzygnij_plan(
            cur, adapter, plan, f, engine=engine, mode=mode,
            kategoria=kategoria, teraz=now, kind=kind)
        if rodzaj == "brak":
            st["niewypelnione"] += 1
        else:
            st["wypelnione"] += 1
            if rodzaj in st:
                st[rodzaj] += 1
        if verbose:
            print(f"  [{rodzaj}] {plan['symbol']} {plan['side']} — {powod}")
    return st


async def anuluj_wejscia(cur, adapter, *, kategoria: str, powod: str) -> int:
    """Zdejmuje z giełdy oczekujące zlecenia WEJŚCIA. Wyjścia zostawia.

    KILL-SWITCH, KTORY ZOSTAWIA ZLECENIA NA GIELDZIE, NIE JEST KILL-SWITCHEM.
    Wstrzymanie wejść zatrzymuje podejmowanie NOWYCH decyzji, ale zlecenie
    złożone minutę wcześniej nadal leży w arkuszu i nadal może się wypełnić —
    już po tym, jak system uznał, że handlować nie wolno. Pozycja powstałaby
    wtedy wbrew działającemu zabezpieczeniu i bez żadnej decyzji za sobą.

    ALE WYJSCIA ZOSTAJA, I TO JEST ROWNIE WAZNE. Kill-switch ma wyprowadzić
    z ryzyka, a nie uwięzić w nim. Zdjęcie zlecenia sprzedaży zostawiłoby
    otwartą pozycję bez czegokolwiek, co ją zamknie — czyli bezpiecznik
    zwiększyłby ekspozycję zamiast ją ściąć.
    """
    cur.execute(
        "SELECT o.client_order_id, i.symbol, o.planned_order_id"
        "  FROM exec.order o"
        "  JOIN ref.instrument i ON i.id = o.instrument_id"
        "  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id"
        " WHERE o.status IN ('new','partial','unknown')"
        "   AND o.client_order_id IS NOT NULL"
        "   AND coalesce(p.trigger->>'rodzaj', 'wejscie') <> 'wyjscie'")
    zdjete = 0
    for o in cur.fetchall():
        try:
            await adapter.anuluj_zlecenie(symbol=o["symbol"],
                                          id_wlasne=o["client_order_id"],
                                          kategoria=kategoria)
        except ExchangeError as e:
            # NIEUDANE ANULOWANIE JEST ZAPISYWANE, nie pomijane. Zlecenie zostaje
            # w arkuszu, wiec musi zostac slad - inaczej "kill-switch zadzialal"
            # znaczyloby cos innego, niz sie wydaje.
            cur.execute("UPDATE exec.order SET reject_reason=%s, updated_at=now()"
                        " WHERE client_order_id=%s",
                        (f"anulowanie po kill-switchu nie powiodlo sie: {e}",
                         o["client_order_id"]))
            continue
        cur.execute("UPDATE exec.order SET status='cancelled', updated_at=now(),"
                    " reject_reason=%s WHERE client_order_id=%s",
                    (powod, o["client_order_id"]))
        if o["planned_order_id"]:
            cur.execute("UPDATE journal.planned_order SET status='cancelled',"
                        " resolved_at=now() WHERE id=%s", (o["planned_order_id"],))
        zdjete += 1
    return zdjete
