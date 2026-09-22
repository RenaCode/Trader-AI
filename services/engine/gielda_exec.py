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

from services.engine.egzekucja import co_z_wypelnieniem, rozlicz_prowizje
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

    # ILE NAPRAWDE MAMY I ILE TO KOSZTOWALO. Na spocie kupno placi prowizje
    # w kupionym aktywie, wiec zlecenie na 1,0 daje nam 0,999. Zapis brutto
    # tworzy pozycje NIE DO SPRZEDANIA: wyjscie prosi o calosc, gielda odrzuca
    # za brak salda, plan wygasa i nastepna swieca powtarza to samo bez konca.
    ilosc_netto, prowizja_kwot = rozlicz_prowizje(
        r.ilosc, cena, prowizja, stan.prowizja_w,
        plan.get("base_ccy") or "", plan["quote_ccy"])
    uwaga = None
    rodzaj_zwrotny, powod_zwrotny = r.rodzaj, r.powod

    if plan["trigger"]["rodzaj"] == "wyjscie":
        cur.execute("SELECT id, side, qty, avg_price FROM exec.position"
                    " WHERE id=%s AND closed_at IS NULL",
                    (plan["trigger"].get("position_id"),))
        poz = cur.fetchone()
        if poz:
            # WIERSZ ZLECENIA JUZ ISTNIEJE - powstal przy zlozeniu wyjscia na
            # gielde, dokladnie tak jak przy wejsciu. Podajemy go, zeby jedno
            # wyjscie nie zostawilo w `exec.order` dwoch zlecen; wypelnienie
            # dopisuje sie do niego.
            #
            # PROWIZJA IDZIE SUROWA, razem z waluta z gieldy. Wejscie przelicza
            # ja przez `rozlicz_prowizje`, bo kupno na spocie placi prowizje
            # w KUPIONYM aktywie i musi ona zejsc z ilosci, ktora naprawde mamy.
            # Sprzedaz takiego problemu nie ma - wychodzi z niej waluta kwotowana
            # - wiec przeliczanie byloby tu zgadywaniem kursu bez potrzeby.
            zapisz_wyjscie(cur, poz, cena, prowizja, teraz, plan["decision_id"],
                           ilosc=r.ilosc, meta=meta,
                           order_id=id_zlecenia_w_bazie,
                           planned_order_id=plan["id"],
                           venue_order_id=stan.id_gieldy,
                           client_order_id=id_w,
                           venue_fill_id=f"{stan.id_gieldy}",
                           prowizja_ccy=prowizja_ccy)
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
                       plan["side"], ilosc_netto, cena, prowizja_kwot, teraz,
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
                          kategoria: str, h, verbose: bool = True,
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

    `h` JEST WYMAGANY, bo od niego zależy nazwa cechy ATR — a od ATR pasmo
    ceny. Wartość domyślna byłaby tu cichym „przy innym barze pasmo znika".
    """
    st = {"zlozone": 0, "odrzucone": 0}
    filtry = filtry_z_bazy(cur, engine)
    # NAZWA CECHY Z HORYZONTU, NIE NA SZTYWNO.
    #
    # Wpisane wprost `atr_pct__4h` dzialalo wylacznie dla domyslnego bara. Przy
    # `--bar 1h` podzapytanie oddawalo NULL, `atr_pct` schodzilo do None, a
    # `zlecenia.prog_pasma` zwracalo wtedy pasmo AWARYJNE (SUFIT_PASMA = 0,25)
    # zamiast policzyc wlasciwe ~0,105. Pasmo przestawalo wykluczac cokolwiek
    # ponizej rzedu wielkosci - czyli tracilo swoja funkcje - i robilo to cicho,
    # bo brak cechy wyglada identycznie jak zmiennosc nieznana.
    #
    # `Horizon.name` jest tu jedynym zrodlem prawdy o tej nazwie; tak samo
    # wyprowadza ja `loop.atr_percentile_80` (services/engine/loop.py:394).
    nazwa_atr = h.name("atr_pct")
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy, i.base_ccy,"
        # ATR do pasma ceny. Nieznana zmiennosc nie wylacza pasma - daje pasmo
        # awaryjne - ale zawezenie go realna zmiennoscia pary jest cala wartoscia
        # tego progu: stala liczba jest albo krzykliwa, albo bezuzyteczna.
        "       (SELECT fv.value FROM feat.feature_value fv"
        "         JOIN feat.feature_def fd ON fd.id = fv.feature_def_id"
        "        WHERE fv.instrument_id = p.instrument_id AND fd.name = %s"
        "        ORDER BY fv.event_time DESC LIMIT 1) AS atr_pct"
        "  FROM journal.planned_order p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        # SILNIK I TRYB Z DECYZJI, KTORA PLAN ZRODZILA - patrz komentarz
        # przy `rozstrzygnij_oczekujace` nizej.
        "  JOIN journal.decision d ON d.id = p.decision_id"
        " WHERE d.engine = %s AND d.mode = %s"
        "   AND p.status = 'pending'"
        "   AND NOT EXISTS (SELECT 1 FROM exec.order o WHERE o.planned_order_id = p.id)"
        "   AND (NOT %s OR p.trigger->>'rodzaj' = 'wyjscie')"
        " ORDER BY p.id", (nazwa_atr, engine, mode, tylko_wyjscia))
    for plan in cur.fetchall():
        f = filtry.get(plan["instrument_id"], Filtry())
        if plan.get("atr_pct") is None and verbose:
            # BRAK CECHY DLA ZNANEGO HORYZONTU ZOSTAWIA SLAD. Pasmo awaryjne
            # jest szersze od policzonego, wiec ta sciezka nie odrzuca zlecen,
            # tylko przepuszcza wiecej - a to jest dokladnie ten rodzaj awarii,
            # ktory bez linijki na ekranie wyglada jak spokojny dzien.
            print(f"  UWAGA: {plan['symbol']} — brak {nazwa_atr} w feat."
                  f"feature_value; pasmo ceny będzie AWARYJNE (szersze)")
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

    PLANY BIERZEMY TYLKO SWOJE — patrz komentarz przy zapytaniu niżej.
    """
    from services.engine.loop import termin_rozstrzygniecia

    # `nie_zdjete` i `wyjscie_bez_pozycji` SA W SLOWNIKU OD POCZATKU, z zerem.
    # Statystyka idzie do heartbeatu silnika; klucz pojawiajacy sie dopiero przy
    # awarii nie da sie odroznic od klucza, ktorego nikt nie liczy.
    st = {"wypelnione": 0, "niewypelnione": 0, "czekaja": 0, "czesciowe": 0,
          "nie_do_zamkniecia": 0, "nie_zdjete": 0, "wyjscie_bez_pozycji": 0}
    filtry = filtry_z_bazy(cur, engine)
    # SILNIK I TRYB Z DECYZJI, KTORA PLAN ZRODZILA.
    #
    # `journal.planned_order` nie ma kolumn engine/mode (0005, linia 68), wiec
    # sam status 'pending' wybieral KAZDY plan w bazie - takze cudzego silnika
    # i cudzego trybu. Asymetria byla jednostronna i wlasnie dlatego grozna:
    # `RiskKernel._open_exposure` dochodzi do tych samych wierszy przez
    # `JOIN journal.decision d ... WHERE d.engine=%s AND d.mode=%s`
    # (services/engine/risk.py:393-402), wiec limit ekspozycji widzi wylacznie
    # WLASNE plany, a egzekutor wykonywal WSZYSTKIE. Dopoki petla jest jedna
    # (values.yaml:298) nie ma to skutku; adapter Alpaki i `--silniki
    # crypto,equity` sa juz w repozytorium, wiec druga petla zapalilaby to
    # przy pierwszym wspolnym cyklu: zlecenia silnika equity poszlyby na NASZ
    # rachunek, a ich pozycje zaksiegowalyby sie jako crypto.
    #
    # KSZTALT JOIN-a JEST CELOWO TEN SAM CO W `_open_exposure`. Denormalizacja
    # engine/mode do `planned_order` byloby prostsze w zapisie, ale to osobna
    # decyzja (migracja + backfill + zgodnosc z dzisiejszymi wierszami), a nie
    # poprawka przy okazji; dopoki jej nie ma, obie strony musza czytac te sama
    # regule z tego samego miejsca, inaczej powstanie druga obok istniejacej.
    cur.execute(
        "SELECT p.id, p.decision_id, p.instrument_id, p.side, p.qty, p.limit_price,"
        "       p.trigger, p.valid_until, i.symbol, i.quote_ccy, i.base_ccy"
        "  FROM journal.planned_order p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        "  JOIN journal.decision d ON d.id = p.decision_id"
        " WHERE d.engine = %s AND d.mode = %s"
        "   AND p.status = 'pending' ORDER BY p.id", (engine, mode))
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
                         engine: str = "crypto", mode: str = "demo",
                         verbose: bool = True) -> tuple[int, int]:
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

    ZWRACANA PARA LICZY WYŁĄCZNIE STANY, KTÓRE ZNAMY: `zdjete` to zlecenia
    zdjęte z arkusza, `wypelnione` — te, o których giełda powiedziała, że już
    weszły. Zlecenie, którego stanu NIE UDAŁO SIĘ ODCZYTAĆ, nie trafia do
    żadnej z nich: doliczenie go gdziekolwiek byłoby zaraportowaniem sukcesu
    tam, gdzie nie wiemy nawet, czy mamy pozycję.

    DLATEGO SAMA PARA NIE WYSTARCZA DO OCENY PRZEBIEGU i funkcja dopisuje
    brakującą liczbę na ekran sama. „Zdjęto 3" przy pięciu rozpatrzonych
    zleceniach czyta się jak komplet, a znaczy „3 z 5, o dwóch nie wiemy nic" —
    i to jest różnica między kill-switchem, który zadziałał, a takim, który
    zostawił dwa żywe zlecenia w arkuszu. Trwałym śladem po każdym z nich jest
    `reject_reason` w `exec.order`, zapisany BEZ zmiany statusu.
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
    # `nieznane` NIE JEST TRZECIM ELEMENTEM ZWRACANEJ KROTKI, tylko liczba
    # do komunikatu. Krotka jest czescia interfejsu tej funkcji i jej
    # rozszerzenie nalezy do osobnej decyzji; pominiecie tej liczby na ekranie
    # nie naleza do zadnej - bez niej operator nie ma jak odroznic "zdjeto
    # wszystko" od "zdjeto tyle, ile sie udalo".
    zdjete, wypelnione, nieznane = 0, 0, 0
    zlecenia = cur.fetchall()
    for o in zlecenia:
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
        except ExchangeError as e:
            # NIEUDANY ODCZYT TO NIE JEST "NIC NIE WESZLO".
            #
            # Poprzednia wersja ustawiala tu `stan = None` i szla dalej, czyli
            # w galaz anulujaca - a ta zapisuje `exec.order='cancelled'`
            # i `planned_order='cancelled'`. Tym jednym zapisem obsluga bledu
            # przewracala rozumowanie stojace 15 linii wyzej: plan przestaje
            # byc 'pending', wiec `rozstrzygnij_oczekujace` juz NIGDY go nie
            # zaksieguje, a wypelnienie, ktore moglo wejsc, przepada
            # bezpowrotnie. Na gieldzie pozycja, w dzienniku nic.
            #
            # O stanie tego zlecenia nie wiemy NICZEGO - ani ze weszlo, ani ze
            # nie. Jedynym ruchem, ktory niczego nie przesadza, jest NIE
            # ZAPISYWAC STATUSU: `exec.order` zostaje na 'new'/'partial',
            # wiec zapytanie na gorze zlapie to zlecenie w nastepnym cyklu
            # i ponowi probe, a plan zostaje 'pending', wiec rozstrzyganie
            # wciaz go widzi i zaksieguje to, co naprawde weszlo.
            #
            # Anulowania TEZ nie probujemy. Bybit na zlecenie juz wypelnione
            # oddaje 110001, ktore adapter zamienia na `return False` bez
            # wyjatku - odpowiedz nie do odroznienia od "nie bylo czego
            # zdejmowac". Nie doniosloby to wiec niczego, czego nie wiemy,
            # a przy zleceniu czesciowo wypelnionym zdjeloby reszte w oparciu
            # o zgadywanie.
            #
            # Powod zapisujemy BEZ zmiany statusu - tym samym wzorcem, co
            # nieudane anulowanie nizej. Bez tego jedynym sladem po nieznanym
            # stanie bylaby linia na ekranie, a ekran nikogo nie obudzi.
            cur.execute(
                "UPDATE exec.order SET reject_reason=%s, updated_at=now()"
                " WHERE client_order_id=%s AND mode::text=%s",
                (f"kill-switch: stanu zlecenia nie udalo sie odczytac ({e})"
                 f" - status BEZ ZMIANY, ponowimy w nastepnym cyklu",
                 o["client_order_id"], mode))
            if verbose:
                print(f"  UWAGA: {o['symbol']} {o['client_order_id']} — stanu"
                      f" zlecenia nie udało się odczytać ({e}); zostaje"
                      f" w arkuszu i do rozstrzygnięcia w następnym cyklu")
            # NIE LICZYMY GO ANI JAKO ZDJETE, ANI JAKO WYPELNIONE. Obie te
            # liczby ida do komunikatu kill-switcha; zlecenie o nieznanym
            # stanie w ktorejkolwiek z nich opisywaloby jako rozstrzygniete
            # cos, czego nie rozstrzygnelismy. Osobny licznik idzie na ekran
            # nizej - inaczej ta sama cisza wrocilaby w postaci zawyzonego
            # poczucia, ze kill-switch zamknal sprawe.
            nieznane += 1
            continue
        # `stan is None` ZNACZY JUZ TYLKO JEDNO: gielda nie zna tego zlecenia,
        # czyli nie weszlo. Nieudany odczyt wyszedl wyzej przez `continue`,
        # wiec ta sama wartosc nie miesza juz dwoch roznych rzeczy - tak samo
        # jak w `rozstrzygnij_plan`, gdzie brak stanu wygasza plan.
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
            # FILTR PO TRYBIE, bo klucz unikalny to (mode, client_order_id),
            # a nie sam identyfikator. `tai-p77` z demo i `tai-p77` z live to
            # dwa rozne wiersze i oba pasuja do samego `client_order_id`:
            # zapis bez trybu dopisalby powod takze cudzemu zleceniu.
            cur.execute("UPDATE exec.order SET reject_reason=%s, updated_at=now()"
                        " WHERE client_order_id=%s AND mode::text=%s",
                        (f"anulowanie po kill-switchu nie powiodlo sie: {e}",
                         o["client_order_id"], mode))
            continue
        cur.execute("UPDATE exec.order SET status='cancelled', updated_at=now(),"
                    " reject_reason=%s WHERE client_order_id=%s AND mode::text=%s",
                    (powod, o["client_order_id"], mode))
        if o["planned_order_id"]:
            cur.execute("UPDATE journal.planned_order SET status='cancelled',"
                        " resolved_at=now() WHERE id=%s", (o["planned_order_id"],))
        zdjete += 1

    # PODSUMOWANIE Z MIANOWNIKIEM, nie sama liczba zdjetych. Wywolujacy
    # (`loop.run_once`) wypisuje "zdjeto z gieldy N zlecen" z samego `zdjete`
    # i nie ma skad wiedziec, ilu zlecen ta liczba nie obejmuje.
    if nieznane and verbose:
        print(f"  UWAGA: kill-switch NIE ROZSTRZYGNĄŁ {nieznane} z "
              f"{len(zlecenia)} rozpatrzonych zleceń — stanu nie udało się "
              f"odczytać, więc NIE MA ich w liczbie zdjętych; zostają "
              f"w arkuszu giełdy do następnego cyklu")
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
        "SELECT o.id, o.client_order_id, o.status, i.symbol, i.lot_size,"
        # ILE JUZ ZAKSIEGOWALISMY z tego zlecenia. Bez tej liczby jedynym
        # sygnalem bylby status wiersza - a status mowi "bylo czesciowe",
        # nie "ile z tego trafilo do dziennika".
        "       coalesce((SELECT sum(f.qty) FROM exec.fill f"
        "                  WHERE f.order_id = o.id), 0) AS zaksiegowane"
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

        # ILE WYPELNILO SIE PONAD TO, CO JUZ ZAKSIEGOWANE.
        #
        # Warunek brzmial tu wczesniej `o["status"] != "partial"` i gubil przez
        # to CALA reszte zlecenia czesciowo wypelnionego: wiersz `partial` mial
        # juz zaksiegowane pierwsze wypelnienie, wiec kazde nastepne bylo
        # wyciszane. Reszta dopelniala sie po rozstrzygnieciu planu, wiersz
        # przechodzil na `filled` jako "domkniety" - i roznica miedzy dziennikiem
        # a rachunkiem zostawala BEZ SLADU. Risk kernel liczy wtedy ekspozycje
        # mniejsza niz faktyczna, a to jest wielkosc, na ktorej stoi kill-switch.
        #
        # Pytanie brzmi "czy jest wypelnienie, ktorego nie ma w dzienniku",
        # a nie "czy wiersz byl czesciowy". Status odpowiada na drugie.
        zaksiegowane = Decimal(str(o.get("zaksiegowane") or 0))
        niezaksiegowane = stan.wypelniono - zaksiegowane
        # Ponizej polowy kroku ilosci nie da sie kupic ani sprzedac, wiec taka
        # roznica to zaokraglenie, nie wypelnienie. Bez kroku porownujemy scisle.
        krok = Decimal(str(o.get("lot_size") or 0))
        poza_dziennikiem = niezaksiegowane > (krok / 2 if krok > 0 else Decimal(0))
        opis_roznicy = (
            f"pozycja poza dziennikiem: {niezaksiegowane} "
            f"(giełda {stan.wypelniono}/{stan.ilosc}, dziennik {zaksiegowane}) "
            f"— wyłapie to uzgodnienie z giełdą")

        if stan.status in ("cancelled", "rejected", "filled"):
            # ZLECENIE SIE JUZ NIE RUSZY. Zostaje tylko pytanie, czy zostawilo
            # po sobie wypelnienie, ktorego nie ma w dzienniku. Nie ksiegujemy
            # go tutaj - ksiegowanie nalezy do rozstrzygania i robienie tego
            # w dwoch miejscach rozjechaloby sie przy pierwszej zmianie.
            if poza_dziennikiem:
                cur.execute(
                    "UPDATE exec.order SET status=%s, updated_at=now(),"
                    " reject_reason=%s WHERE id=%s",
                    (stan.status, f"sprzątanie: {opis_roznicy}", o["id"]))
                st["wypelnione_po_fakcie"] += 1
                if verbose:
                    print(f"  [sprzątanie] {o['symbol']}: WYPEŁNIŁO SIĘ po fakcie "
                          f"{stan.wypelniono}/{stan.ilosc}, poza dziennikiem "
                          f"{niezaksiegowane}")
            else:
                cur.execute("UPDATE exec.order SET status=%s, updated_at=now()"
                            " WHERE id=%s", (stan.status, o["id"]))
                st["domkniete"] += 1
            continue

        # ZLECENIE NADAL ZYJE - reszte trzeba zdjac z arkusza NIEZALEZNIE od
        # tego, czy cos juz sie z niego wypelnilo. To sa dwie osobne sprawy
        # i potraktowanie ich jako jednej zostawialo zywa reszte w arkuszu.
        try:
            await adapter.anuluj_zlecenie(symbol=o["symbol"],
                                          id_wlasne=o["client_order_id"],
                                          kategoria=kategoria)
        except ExchangeError as e:
            if verbose:
                print(f"  [sprzątanie] {o['symbol']}: anulowanie nadal nie "
                      f"wychodzi ({e}) — spróbujemy w następnym cyklu")
            continue
        powod = "sprzątanie: zlecenie przeżyło swój plan i zostało zdjęte"
        if poza_dziennikiem:
            powod += f"; UWAGA: {opis_roznicy}"
            st["wypelnione_po_fakcie"] += 1
        cur.execute("UPDATE exec.order SET status='cancelled', updated_at=now(),"
                    " reject_reason=%s WHERE id=%s", (powod, o["id"]))
        st["zdjete"] += 1
        if verbose:
            print(f"  [sprzątanie] {o['symbol']}: zdjęte z arkusza"
                  + (f" (poza dziennikiem {niezaksiegowane})"
                     if poza_dziennikiem else ""))
    return st
