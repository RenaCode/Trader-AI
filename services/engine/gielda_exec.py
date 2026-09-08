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

    # CENA ODNIESIENIA Z KSIEGI, W KTOREJ ZLECENIE WYLADUJE.
    #
    # Cena limitu powstaje ze swiec podmiotu GLOBALNEGO - to jego ksiega
    # posluzyla do trenowania modelu. Sprawdzanie jej wzgledem tej samej ksiegi
    # byloby porownaniem liczby z sama soba i przeszloby zawsze, takze dla ticka
    # zepsutego. Rozjazd miedzy ksiegami jest zmierzony i duzy: w 16,8% swiec
    # przekracza cala runde kosztowa.
    #
    # Brak odczytu NIE BLOKUJE zlecenia, ale zostawia slad - inaczej awaria
    # jednego endpointu zatrzymywalaby handel, a to gorsze lekarstwo niz choroba.
    # `ocena_pasma` przy braku odniesienia zwraca None i mowi o tym wprost.
    odniesienie = await adapter.cena_odniesienia(plan["symbol"], kategoria)
    atr = float(plan["atr_pct"]) if plan.get("atr_pct") is not None else None

    ilosc, cena, powod = sprawdz_zlecenie(
        Decimal(plan["qty"]), Decimal(plan["limit_price"]), f,
        kupno=kupno, wyjscie=wyjscie,
        cena_odniesienia=odniesienie, atr_pct=atr)
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
    """Pyta giełdę o los planu i zapisuje wynik. Zwraca (rodzaj, komunikat).

    Rodzaj to zwykle wynik `co_z_wypelnieniem` ('pelne', 'czesciowe', 'brak',
    'nie_do_zamkniecia'), ale dwa przypadki mają własne nazwy, bo NIE SĄ
    rozstrzygnięciem planu i muszą być widoczne w statystyce cyklu:

      * 'nie_zdjete'          — nic nie weszło, ale zlecenia nie udało się
                                anulować; zostaje w arkuszu giełdy, więc plan
                                zostaje `pending` i wracamy do niego w następnym
                                cyklu,
      * 'wyjscie_bez_pozycji' — sprzedaż wykonała się na giełdzie, a w bazie nie
                                ma otwartej pozycji, którą miała zamknąć.
    """
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

    # NIEUDANE ANULOWANIE ZAPAMIETUJEMY, bo decyduje o losie planu nizej.
    # Sam dopisek do powodu trafia tylko na ekran, a ekran nikogo nie obudzi.
    anulowanie_padlo: ExchangeError | None = None
    anulowano = False
    if r.anuluj_reszte and stan.status not in ("filled", "cancelled", "rejected"):
        try:
            await adapter.anuluj_zlecenie(symbol=plan["symbol"], id_wlasne=id_w,
                                          kategoria=kategoria)
            anulowano = True
        except ExchangeError as e:
            # Nieudane anulowanie NIE UNIEWAZNIA reszty zapisu. Wypelnienie, ktore
            # juz weszlo, JEST NASZE - udawanie, ze rozstrzygniecia nie bylo,
            # zgubiloby pozycje, ktora naprawde mamy.
            anulowanie_padlo = e
            r = type(r)(r.rodzaj, r.ilosc, r.anuluj_reszte, r.ulamek,
                        r.powod + f"; UWAGA: anulowanie reszty nie powiodło się ({e})")

    meta = {"execution": "gielda", "kind": kind, "order": "limit",
            "ulamek_wypelnienia": str(r.ulamek), "rodzaj": r.rodzaj,
            "status_gieldy": stan.status, "venue_order_id": stan.id_gieldy}

    if r.rodzaj == "brak":
        if anulowanie_padlo is not None:
            # PLAN NIE WYGASA, DOPOKI ZLECENIE LEZY W ARKUSZU.
            #
            # Nic nie weszlo, wiec nie ma czego ksiegowac - ale zlecenie NADAL
            # JEST NA GIELDZIE i nadal moze sie wypelnic po cenie, ktorej zadna
            # decyzja juz nie popiera. Wygaszenie planu w tym miejscu zrywaloby
            # jedyna nitke prowadzaca do tego zlecenia: rozstrzyganie szuka
            # planow o statusie 'pending', wiec plan 'expired' nie zostalby juz
            # nigdy dotkniety, a jedyne inne miejsce ponawiajace anulowanie
            # (`anuluj_wejscia`) rusza wylacznie po kill-switchu.
            #
            # Skutek byl kumulatywny, a nie jednorazowy: nastepna swieca widzi
            # brak pozycji, sygnal wciaz trzyma, wiec powstaje NOWY plan i NOWE
            # zlecenie - i tak co swiece. Skok ceny wypelnia wszystkie naraz,
            # czyli dokladnie ta nadmiarowa ekspozycja, ktorej risk kernel nie
            # policzyl, bo w dzienniku nie bylo ani jednej pozycji.
            #
            # Zostawiony 'pending' plan wraca tu w kazdym cyklu i PONAWIA
            # anulowanie az do skutku. Gdyby giełda nie dala go zdjac przez
            # dluzszy czas, dozor zglosi to sam: `sprawdz_egzekucje` alarmuje
            # o planach 'pending' po terminie ich waznosci.
            cur.execute(
                "UPDATE exec.order SET status=%s, updated_at=%s, reject_reason=%s,"
                " raw = raw || %s WHERE planned_order_id=%s",
                (stan.status, teraz,
                 f"anulowanie przy rozstrzygnięciu nie powiodło się: {anulowanie_padlo}",
                 json.dumps(dict(meta, anulowanie="nie_powiodlo_sie",
                                 plan="pozostaje pending — ponowimy w następnym cyklu")),
                 plan["id"]))
            return "nie_zdjete", (r.powod + " — ZLECENIE ZOSTAJE W ARKUSZU GIEŁDY,"
                                  " plan pozostaje do rozstrzygnięcia i anulowanie"
                                  " zostanie ponowione")

        cur.execute("UPDATE journal.planned_order SET status='expired',"
                    " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
        # WIERSZ ZLECENIA TEZ ZAMYKAMY. Zostawiony na 'new' opisywalby zlecenie
        # otwarte, ktorego juz nie ma: kill-switch probowalby je co raz zdejmowac,
        # a czujka zdrowia egzekucji liczylaby je jako przyjete i wiszace.
        cur.execute(
            "UPDATE exec.order SET status=%s, updated_at=%s, raw = raw || %s"
            " WHERE planned_order_id=%s",
            (stan.status if stan.status in ("cancelled", "rejected") else "cancelled",
             teraz, json.dumps(dict(meta, anulowanie="zdjete" if anulowano
                                    else f"niepotrzebne (giełda: {stan.status})")),
             plan["id"]))
        return r.rodzaj, r.powod

    cena = stan.srednia_cena or Decimal(plan["limit_price"])
    prowizja = stan.prowizja
    # WALUTA PROWIZJI Z GIELDY, NIE Z ZALOZENIA. Kupno na spocie placi prowizje
    # w kupionym aktywie, wiec wpisanie waluty kwotowanej zanizalo koszt wejscia
    # o dwa rzedy wielkosci - a przy przewadze mierzonej w dziesiatych czesciach
    # procenta to nie jest zaokraglenie.
    prowizja_ccy = stan.prowizja_w or plan["quote_ccy"]
    uwaga = None
    rodzaj_zwrotny, powod_zwrotny = r.rodzaj, r.powod

    if plan["trigger"]["rodzaj"] == "wyjscie":
        cur.execute("SELECT id, side, qty, avg_price FROM exec.position"
                    " WHERE id=%s AND closed_at IS NULL",
                    (plan["trigger"].get("position_id"),))
        poz = cur.fetchone()
        if poz:
            zapisz_wyjscie(cur, poz, cena, prowizja, teraz, plan["decision_id"],
                           ilosc=r.ilosc)
        else:
            # SPRZEDAZ SIE WYKONALA, A NIE MA CZEGO ZAMKNAC.
            #
            # Aktywo wyszlo z rachunku naprawde - niezaleznie od tego, czy
            # znajdziemy pozycje, ktorej dotyczylo. Milczace przejscie dalej
            # (samo `if poz:` bez `else`) zapisywalo plan jako 'triggered',
            # czyli "rozstrzygniety pomyslnie", nie zostawiajac ani linijki
            # na ekranie, ani wiersza w bazie. Rozjazd z gielda ujawnialby sie
            # dopiero przy uzgodnieniu sald i wygladal jak blad uzgodnienia.
            #
            # DOKSIEGOWAC SIE TEGO NIE DA I NIE WOLNO ZGADYWAC. Zamkniecie
            # dowolnej innej otwartej pozycji na tym instrumencie przypisaloby
            # sprzedaz cudzej decyzji i sfalszowalo jej wynik. Zapisujemy wiec
            # to, co wiemy, tam, gdzie ktos tego szuka: w wierszu zlecenia
            # (`reject_reason` + `raw`), i zwracamy wlasny rodzaj, zeby petla
            # wypisala to na ekranie i policzyla w statystyce cyklu, ktora idzie
            # do heartbeatu silnika.
            #
            # Plan zostaje 'triggered', bo zlecenie FAKTYCZNIE sie wykonalo.
            # 'pending' oznaczaloby "czekamy na gielde" i wracaloby tu co cykl,
            # odczytujac w kolko to samo wypelnienie - bez szansy na zmiane.
            uwaga = ("wyjście wykonane na giełdzie BEZ otwartej pozycji"
                     f" (position_id={plan['trigger'].get('position_id')}):"
                     f" sprzedano {r.ilosc} @ {cena}, nie ma czego zamknąć")
            meta = dict(meta, anomalia="wyjscie_bez_pozycji",
                        position_id=plan["trigger"].get("position_id"),
                        sprzedano=str(r.ilosc), cena_wykonania=str(cena),
                        prowizja=str(prowizja))
            rodzaj_zwrotny = "wyjscie_bez_pozycji"
            powod_zwrotny = uwaga + f" ({r.powod})"
    else:
        zapisz_wejscie(cur, engine, mode, plan["instrument_id"], plan["decision_id"],
                       plan["side"], r.ilosc, cena, prowizja, teraz,
                       plan["quote_ccy"], meta, planned_order_id=plan["id"],
                       venue_order_id=stan.id_gieldy, client_order_id=id_w,
                       venue_fill_id=f"{stan.id_gieldy}",
                       order_id=id_zlecenia_w_bazie,
                       prowizja_ccy=prowizja_ccy)

    if anulowanie_padlo is not None:
        # RESZTA ZLECENIA ZOSTAJE ZYWA NA GIELDZIE.
        #
        # Wypelnienie juz zaksiegowalismy - jest nasze. Ale reszta nadal lezy
        # w arkuszu i nadal moze wejsc po cenie, ktorej zadna decyzja nie
        # popiera. Plan idzie na 'triggered', bo wracanie do niego odczytywaloby
        # w kolko to samo wypelnienie; wiersz ZLECENIA zostaje wiec otwarty
        # i to on jest nitka, po ktorej sprzatanie (`posprzataj_zlecenia`)
        # wraca tu w kazdym cyklu, a nie dopiero po kill-switchu.
        cur.execute(
            "UPDATE exec.order SET status=%s, updated_at=%s, reject_reason=%s,"
            " raw = raw || %s WHERE planned_order_id=%s",
            (stan.status, teraz,
             f"anulowanie reszty nie powiodlo sie: {anulowanie_padlo}",
             json.dumps(dict(meta, anulowanie="nie_powiodlo_sie",
                             reszta="zostaje w arkuszu — sprzątanie ponowi")),
             plan["id"]))

    cur.execute("UPDATE journal.planned_order SET status='triggered',"
                " resolved_at=%s WHERE id=%s", (teraz, plan["id"]))
    # `COALESCE`, zeby zapis bez uwagi nie kasowal powodu wpisanego wczesniej
    # (np. przez kill-switch albo poprzednie nieudane anulowanie).
    cur.execute("UPDATE exec.order SET status=%s, updated_at=%s,"
                " reject_reason = COALESCE(%s, reject_reason), raw = raw || %s"
                " WHERE planned_order_id=%s",
                (stan.status, teraz, uwaga, json.dumps(meta), plan["id"]))

    return rodzaj_zwrotny, powod_zwrotny


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
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy,"
        # ATR do pasma ceny. Nieznana zmiennosc nie wylacza pasma - daje pasmo
        # awaryjne - ale zawezenie go realna zmiennoscia pary jest cala wartoscia
        # tego progu: stala liczba jest albo krzykliwa, albo bezuzyteczna.
        "       (SELECT fv.value FROM feat.feature_value fv"
        "         JOIN feat.feature_def fd ON fd.id = fv.feature_def_id"
        "        WHERE fv.instrument_id = p.instrument_id AND fd.name = %s"
        "        ORDER BY fv.event_time DESC LIMIT 1) AS atr_pct"
        "  FROM journal.planned_order p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.status = 'pending'"
        "   AND NOT EXISTS (SELECT 1 FROM exec.order o WHERE o.planned_order_id = p.id)"
        "   AND (NOT %s OR p.trigger->>'rodzaj' = 'wyjscie')"
        " ORDER BY p.id", ("atr_pct__4h", tylko_wyjscia))
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

    # `nie_zdjete` i `wyjscie_bez_pozycji` SA W SLOWNIKU OD POCZATKU, z zerem.
    # Statystyka idzie do heartbeatu silnika; klucz pojawiajacy sie dopiero przy
    # awarii nie da sie odroznic od klucza, ktorego nikt nie liczy.
    st = {"wypelnione": 0, "niewypelnione": 0, "czekaja": 0, "czesciowe": 0,
          "nie_do_zamkniecia": 0, "nie_zdjete": 0, "wyjscie_bez_pozycji": 0}
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
        elif rodzaj == "nie_zdjete":
            # NIE JEST TO ANI WYPELNIENIE, ANI ROZSTRZYGNIECIE. Zlecenie zostalo
            # na gieldzie, plan wraca tu w nastepnym cyklu. Doliczenie go do
            # `niewypelnione` mowiloby "zamkniete i nic nie weszlo", czyli
            # dokladnie to, czego jeszcze nie wiemy.
            st["nie_zdjete"] += 1
        else:
            st["wypelnione"] += 1
            if rodzaj in st:
                st[rodzaj] += 1
        if verbose:
            print(f"  [{rodzaj}] {plan['symbol']} {plan['side']} — {powod}")
    return st


async def anuluj_wejscia(cur, adapter, *, kategoria: str, powod: str,
                         engine: str = "crypto", mode: str = "demo") -> tuple[int, int]:
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
        "   AND o.engine::text = %s AND o.mode::text = %s"
        "   AND coalesce(p.trigger->>'rodzaj', 'wejscie') <> 'wyjscie'",
        (engine, mode))
    zdjete, wypelnione = 0, 0
    for o in cur.fetchall():
        # NAJPIERW PYTAMY GIELDE, POTEM ANULUJEMY.
        #
        # Nasz status jest stary: wiersz siedzi na 'new' przez cale dwie swiece,
        # bo nikt go nie odswieza miedzy zlozeniem a rozstrzygnieciem. Zlecenie
        # moglo sie w tym czasie wypelnic.
        #
        # Zapisanie takiego zlecenia jako anulowanego GUBI WYPELNIENIE
        # BEZPOWROTNIE: rozstrzyganie szuka planow o statusie 'pending', wiec
        # juz nigdy go nie zaksieguje. Trzymalibysmy aktywo, ktorego dziennik
        # nie widzi - a po zwolnieniu kill-switcha sygnal nadal dodatni dalby
        # DRUGIE wejscie. Na gieldzie dwie pozycje, w dzienniku jedna.
        #
        # Nie mozna tez polegac na odpowiedzi samego anulowania: Bybit na
        # zlecenie juz wypelnione odpowiada kodem 110001, ktory adapter
        # traktuje jako "nie bylo czego zdejmowac" - nie do odroznienia od
        # zlecenia, ktorego nigdy nie bylo.
        try:
            stan = await adapter.stan_zlecenia(o["client_order_id"],
                                               kategoria=kategoria)
        except ExchangeError:
            stan = None
        if stan is not None and stan.wypelniono > 0:
            # Reszte i tak zdejmujemy, zeby nie doszlo wiecej - ale planu NIE
            # ruszamy, zeby rozstrzygniecie zaksiegowalo to, co weszlo.
            try:
                await adapter.anuluj_zlecenie(symbol=o["symbol"],
                                              id_wlasne=o["client_order_id"],
                                              kategoria=kategoria)
            except ExchangeError:
                pass
            wypelnione += 1
            continue
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
    return zdjete, wypelnione


async def posprzataj_zlecenia(cur, adapter, *, engine: str, mode: str,
                              kategoria: str, verbose: bool = True) -> dict:
    """Zamyka zlecenia, które przeżyły swój plan. Zwraca statystykę.

    DLACZEGO TO MUSI ISTNIEĆ OSOBNO

    Plan jest rozstrzygnięty i nikt do niego nie wróci — rozstrzyganie szuka
    planów `pending`. Zlecenie może jednak dalej żyć na giełdzie, i to na dwa
    sposoby, z których żaden nie miał dotąd sprzątacza:

      * anulowanie reszty przy wypełnieniu CZĘŚCIOWYM nie powiodło się. Reszta
        leży w arkuszu i wypełni się po cenie, której żadna decyzja już nie
        popiera — a risk kernel jej nie policzy, bo w dzienniku pozycja ma
        wielkość samego wypełnienia,
      * odpowiedź na złożenie nie dotarła i wiersz został na `unknown`. Jeżeli
        giełda tego zlecenia nie zna, wiersz jest już tylko fałszywym alarmem:
        czujka zdrowia egzekucji zgłasza go CODZIENNIE jako „kandydata na
        pozycję-widmo", a lista rośnie bez końca i uczy odbiorcę ignorować.

    Dotąd jedynym miejscem ponawiającym anulowanie było `anuluj_wejscia`, które
    rusza WYŁĄCZNIE po kill-switchu. Zlecenie porzucone przy zwykłym błędzie
    sieci czekało więc na obsunięcie kapitału, żeby ktoś je zdjął.

    PYTAMY GIEŁDĘ, ZANIM COKOLWIEK ZAPISZEMY. Nasz status jest stary z definicji
    — to on właśnie okazał się nieprawdziwy.
    """
    st = {"zdjete": 0, "domkniete": 0, "wypelnione_po_fakcie": 0}
    cur.execute(
        "SELECT o.id, o.client_order_id, o.status, i.symbol"
        "  FROM exec.order o"
        "  JOIN ref.instrument i ON i.id = o.instrument_id"
        "  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id"
        " WHERE o.status IN ('new','partial','unknown')"
        "   AND o.engine::text = %s AND o.mode::text = %s"
        "   AND o.client_order_id IS NOT NULL"
        # WYLACZNIE zlecenia, ktorych plan JUZ sie rozstrzygnal. Zlecenia
        # czekajace na swoja swiece nie sa sierotami i ruszanie ich zabieraloby
        # rozstrzyganiu jego wlasna prace.
        "   AND (p.id IS NULL OR p.status::text <> 'pending')",
        (engine, mode))

    for o in cur.fetchall():
        try:
            stan = await adapter.stan_zlecenia(o["client_order_id"],
                                               kategoria=kategoria)
        except ExchangeError as e:
            if verbose:
                print(f"  [sprzątanie] {o['symbol']}: nie da się odczytać stanu ({e})")
            continue

        if stan is None:
            # GIELDA GO NIE ZNA. Zlecenie albo nigdy nie powstalo, albo dawno
            # zniknelo - w obu przypadkach wiersz otwarty jest juz tylko
            # zrodlem falszywego alarmu.
            cur.execute(
                "UPDATE exec.order SET status='cancelled', updated_at=now(),"
                " reject_reason=%s WHERE id=%s",
                ("sprzątanie: giełda nie zna tego zlecenia — wiersz zamknięty, "
                 "żeby przestał być zgłaszany jako o nieznanym stanie", o["id"]))
            st["domkniete"] += 1
            continue

        if stan.wypelniono > 0 and o["status"] != "partial":
            # WYPELNILO SIE JUZ PO ROZSTRZYGNIECIU. Nie ksiegujemy tego tutaj -
            # ksiegowanie nalezy do rozstrzygania i robienie tego w dwoch
            # miejscach rozjechaloby sie przy pierwszej zmianie. Zglaszamy,
            # bo jest to realna pozycja poza dziennikiem.
            cur.execute(
                "UPDATE exec.order SET status=%s, updated_at=now(), reject_reason=%s"
                " WHERE id=%s",
                (stan.status,
                 f"sprzątanie: zlecenie wypełniło się PO rozstrzygnięciu planu "
                 f"({stan.wypelniono}/{stan.ilosc}) — pozycja poza dziennikiem, "
                 f"wyłapie ją uzgodnienie z giełdą", o["id"]))
            st["wypelnione_po_fakcie"] += 1
            if verbose:
                print(f"  [sprzątanie] {o['symbol']}: WYPEŁNIŁO SIĘ po fakcie "
                      f"{stan.wypelniono}/{stan.ilosc}")
            continue

        if stan.status in ("cancelled", "rejected", "filled"):
            cur.execute("UPDATE exec.order SET status=%s, updated_at=now()"
                        " WHERE id=%s", (stan.status, o["id"]))
            st["domkniete"] += 1
            continue

        try:
            await adapter.anuluj_zlecenie(symbol=o["symbol"],
                                          id_wlasne=o["client_order_id"],
                                          kategoria=kategoria)
        except ExchangeError as e:
            if verbose:
                print(f"  [sprzątanie] {o['symbol']}: anulowanie nadal nie "
                      f"wychodzi ({e}) — spróbujemy w następnym cyklu")
            continue
        cur.execute("UPDATE exec.order SET status='cancelled', updated_at=now(),"
                    " reject_reason=%s WHERE id=%s",
                    ("sprzątanie: zlecenie przeżyło swój plan i zostało zdjęte",
                     o["id"]))
        st["zdjete"] += 1
        if verbose:
            print(f"  [sprzątanie] {o['symbol']}: zdjęte z arkusza")
    return st
