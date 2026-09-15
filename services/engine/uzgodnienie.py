"""Uzgodnienie z giełdą: czy nasz obraz stanu zgadza się z jej.

DLACZEGO TO MUSI ISTNIEĆ ZANIM SILNIK ZACZNIE SKŁADAĆ ZLECENIA

Na rachunku papierowym uzgodnienie jest tautologią — sami zapisujemy pozycje
i sami je odczytujemy, więc zawsze się zgadza. Na żywym rachunku nasz obraz
i obraz giełdy to dwa różne stany, które mogą się rozjechać na kilka sposobów,
i żaden z nich nie objawia się awarią:

  * zlecenie weszło, a odpowiedź nie dotarła — mamy pozycję, o której nie wiemy
  * zlecenie odrzucono po naszym zapisie — wiemy o pozycji, której nie ma
  * wypełnienie częściowe zapisane jako całkowite — mamy połowę tego, co myślimy
  * ktoś zamknął pozycję ręcznie w panelu giełdy

Pozycja-widmo jest przy tym groźniejsza niż brakująca: risk kernel liczy z niej
ekspozycję, więc blokuje wejścia, których nic nie blokuje. System stoi i nie ma
o tym pojęcia.

CO TO SPRAWDZA NA SPOCIE

Na spocie „pozycja" to po prostu saldo aktywa. Porównujemy więc ilość, którą
według `exec.position` trzymamy, z saldem, które raportuje giełda — per aktywo.

TOLERANCJA JEST KONIECZNA, nie wygodna. Giełda zaokrągla ilości do kroku
instrumentu, a prowizja bywa pobierana w kupionym aktywie. Porównanie na
dokładną równość zgłaszałoby rozjazd przy każdej transakcji.

DWA PROGI, BO SĄ DWA RÓŻNE ŹRÓDŁA SZUMU — i jeden z nich nie ma odniesienia

Sama różnica WZGLĘDNA nie wystarcza i psuje się dokładnie tam, gdzie boli.
Gdy po jednej stronie jest zero, mianownikiem porównania jest sama resztka,
więc KAŻDA niezerowa ilość daje względną 1,0 — tysiąc razy ponad tolerancję.
Resztka warta siedem centów wygląda wtedy identycznie jak zniknięcie całej
pozycji, a `killswitch.triggered` chodzi bez wyciszenia co dziesięć minut.
Skutek nie jest kosmetyczny: CallMeBot ma własny limit tempa, więc prawdziwy
kill-switch i `engine.down` giną za ścianą alarmów o pyle.

Dlatego różnica musi przekroczyć OBA progi, żeby zostać rozjazdem:

  * bezwzględny — ile aktywa trzeba mieć, żeby to w ogóle była pozycja.
    Bierze się z reguł instrumentu (`ref.instrument.lot_size`, `min_notional`),
    czyli z tego samego źródła, którym giełda mierzy zlecenia,
  * względny — ile aktywa może zjeść księgowanie prowizji. Bierze się
    z CENNIKA (`koszty.py`), bo stawki zmienia regulamin i progi VIP.

Żaden z nich nie jest stałą wpisaną w ten plik. Stała byłaby trzecim miejscem,
w którym trzyma się reguły giełdy — i pierwszym, które by się rozjechało.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.koszty import CENNIK, koszty  # noqa: E402
from services.exchanges.base import ExchangeError  # noqa: E402

# Rodzaj instrumentu, którym chodzi silnik — decyduje o wierszu CENNIKA.
# Ten sam argument co przy `mozliwosci.py --stawki --kind spot_eu`: podmiot
# unijny ma takera 0,250%, czyli DWA I PÓŁ RAZA drożej niż globalny, a próg
# policzony z cudzego wiersza cennika nie opisuje rachunku, który uzgadniamy.
KIND_DOMYSLNY = "spot_eu"


def tolerancja_wzgledna(kind: str = KIND_DOMYSLNY) -> Decimal:
    """Ile PROCENT różnicy jeszcze nie jest rozjazdem. Z CENNIKA, nie ze stałej.

    CO TA TOLERANCJA MA POCHŁONĄĆ, a czego nie

    Po naprawieniu `rozlicz_prowizje` obie strony porównania liczą NETTO, więc
    w kodzie poprawnym asymetrii nie ma wcale. Tolerancja zabezpiecza przypadek,
    w którym jedna strona przestaje odejmować prowizję pobraną w kupionym
    aktywie — a to jest realne, bo `/v5/order/*` potrafi nie oddać `feeCurrency`,
    które `/v5/execution/list` oddaje. Wtedy różnica wynosi DOKŁADNIE jedną
    stawkę prowizji.

    DLACZEGO TAKER, A NIE MAKER. Nie wiemy, która stawka zaksięgowała się po
    której stronie, więc próg musi pokryć droższą. Poprzednia stała 0,001 leżała
    dokładnie na stawce makera i była 2,5x MNIEJSZA od takera zmierzonego
    u giełdy (0,250% na podmiocie unijnym) — czyli alarmowała przy każdym
    kupnie zleceniem rynkowym. Komentarz w `egzekucja.rozlicz_prowizje` sam
    przyznawał, że próg leży „DOKŁADNIE na granicy".

    DLACZEGO RAZY DWA. Granica jest złym miejscem na próg: giełda zaokrągla
    prowizję do precyzji aktywa, więc faktyczna różnica bywa odrobinę WIĘKSZA
    niż stawka nominalna, a jeden wiersz pozycji potrafi przejść więcej niż
    jedno księgowanie (wejście plus wyjście częściowe). Mnożnik 2 daje margines
    na jedno dodatkowe zdarzenie i odsuwa próg od równości, na której stał.

    Szum od zaokrąglania ilości do kroku instrumentu NIE jest już tu liczony —
    od tego jest próg bezwzględny, który mierzy go dokładnie, a nie w procentach.
    """
    return Decimal(str(koszty(kind).taker)) * 2


class Nieuzgadnialne(RuntimeError):
    """Uzgodnienia nie da sie przeprowadzic — to NIE jest zgodnosc."""


def rozjazdy(nasze: dict[str, Decimal], gieldy: dict[str, Decimal],
             tolerancja: Decimal | None = None,
             progi: dict[str, Decimal] | None = None) -> list[dict]:
    """Porównanie dwóch obrazów stanu. CZYSTA FUNKCJA — bez bazy i bez sieci.

    Zwraca listę różnic; pusta lista znaczy zgodność. Rozdzielenie porównania
    od pobierania jest tu warunkiem sprawdzalności: inaczej test tej logiki
    wymagałby giełdy, a giełda w teście to już nie jest test tej logiki.

    Aktywa nieznane jednej ze stron TEŻ są rozjazdem. Saldo, o którym nie wiemy,
    jest tak samo groźne jak pozycja, której nie ma — tyle że w drugą stronę.

    `progi` to próg BEZWZGLĘDNY per aktywo: ilość, poniżej której różnica nie
    jest pozycją, bo żadne zlecenie nie mogłoby jej ani stworzyć, ani usunąć.
    Buduje go `progi_bezwzgledne()` z reguł instrumentu. Brak wpisu znaczy
    próg zerowy — czyli zachowanie sprzed tej poprawki, i dlatego wołający
    z bazą MUSI go podać.

    ILOŚĆ UJEMNA NIE JEST ROZJAZDEM ILOŚCIOWYM, TYLKO BŁĘDEM POMIARU. Na spocie
    nie da się mieć -0,5 BTC. Ujemna wartość po którejkolwiek stronie znaczy,
    że sam pomiar jest nieprawdziwy — najczęściej dlatego, że okno rejestru
    giełdy obcięło kupno i zostawiło samą sprzedaż (patrz `pozycje_z_wypelnien`).
    Nazwanie tego „pozycją-widmo" wysyłałoby człowieka w złą stronę: kazałoby
    szukać aktywa, którego giełda nie ma, podczas gdy naprawdę brakuje POŁOWY
    REJESTRU. Dostaje więc własny rodzaj i przechodzi PRZED oboma progami —
    progi mierzą wielkość różnicy, a tu zła jest sama liczba.
    """
    if tolerancja is None:
        tolerancja = tolerancja_wzgledna()
    progi = progi or {}
    wynik = []
    for aktywo in sorted(set(nasze) | set(gieldy)):
        u_nas = nasze.get(aktywo, Decimal(0))
        u_nich = gieldy.get(aktywo, Decimal(0))
        roznica = u_nich - u_nas
        odniesienie = max(abs(u_nas), abs(u_nich))

        if u_nas < 0 or u_nich < 0:
            wynik.append({
                "aktywo": aktywo,
                "u_nas": u_nas,
                "u_gieldy": u_nich,
                "roznica": roznica,
                "wzgledna": (abs(roznica) / odniesienie if odniesienie
                             else Decimal(0)),
                "rodzaj": "ilosc-ujemna",
            })
            continue

        # PRÓG BEZWZGLĘDNY PRZED WZGLĘDNYM, bo tylko on ma sens przy zerze.
        #
        # Przy `u_nas = 0` mianownikiem względnej jest sama resztka, więc każda
        # niezerowa ilość daje 1,0 — i jedna nieusuwalna drobina na rachunku
        # zamienia uzgodnienie w nadajnik alarmów krytycznych bez końca.
        prog = progi.get(aktywo, Decimal(0))
        if abs(roznica) <= prog:
            continue

        if odniesienie == 0:
            continue
        wzgledna = abs(roznica) / odniesienie
        if wzgledna <= tolerancja:
            continue
        wynik.append({
            "aktywo": aktywo,
            "u_nas": u_nas,
            "u_gieldy": u_nich,
            "roznica": roznica,
            "wzgledna": wzgledna,
            # Kierunek nazwany wprost, bo to on decyduje, co jest grozniejsze.
            "rodzaj": ("pozycja-widmo" if u_nas > u_nich else "saldo-nieznane"),
        })
    return wynik


# --------------------------------------------------------- prog bezwzgledny

async def progi_bezwzgledne(fetch_all, engine: str) -> dict[str, Decimal]:
    """Ile aktywa musi się różnić, żeby to była POZYCJA. Z reguł instrumentu.

    DLACZEGO Z BAZY, A NIE ZE STAŁEJ

    Próg wpisany w ten plik byłby zgadywaniem cudzych reguł. `ref.instrument`
    trzyma je tak, jak podała je giełda (`services/ingest/instrumenty.py`),
    więc zmiana kroku czy minimum wartości przesuwa próg sama — a to są liczby,
    które giełda zmienia bez uprzedzenia i per instrument.

    RESZTKA JEST NIEUSUWALNA Z DWÓCH NIEZALEŻNYCH POWODÓW i próg musi pokryć
    oba, dlatego bierzemy z nich WIĘKSZY:

      * `lot_size` — zlecenie na ilość mniejszą niż krok nie istnieje.
        `zlecenia.do_kroku_w_dol` zaokrągla sprzedaż W DÓŁ (i słusznie: w górę
        łamałoby limit risk kernela po cichu), więc po każdym wyjściu zostaje
        reszta z dzielenia przez krok. Z definicji reszty jest ona ZAWSZE
        mniejsza od kroku — czyli ten próg pokrywa ją co do jednego przypadku,
        a nie „zwykle".
      * `min_notional` — zlecenie warte mniej niż minimum giełdy jest
        odrzucane. Resztka powyżej kroku, ale warta pięciu centów, jest równie
        trwała: nie da się jej sprzedać ani dziś, ani nigdy.

    Drugi warunek jest w walucie kwotowanej, więc wymaga ceny. Bierzemy ostatnią
    świecę minutową i tylko wtedy, gdy jest ŚWIEŻA. Cena sprzed trzech dni
    przeliczona na ilość daje próg oderwany od rynku, a błąd w stronę progu za
    dużego jest tu najgorszy z możliwych: wycisza rozjazd, który był prawdziwy.
    Brak świeżej ceny zostawia sam `lot_size` — mniej, ale nadal poprawnie.

    KIERUNEK ZAOKRĄGLEŃ MIĘDZY INSTRUMENTAMI TEGO SAMEGO AKTYWA. Jedno aktywo
    bazowe może mieć kilka par. Bierzemy NAJMNIEJSZY krok i NAJWYŻSZĄ cenę,
    czyli w obu członach wariant dający próg MNIEJSZY: resztka jest naprawdę
    nie do sprzedania dopiero wtedy, gdy nie da się jej sprzedać na ŻADNEJ
    z par, na których stoi.
    """
    rows = await fetch_all(
        "SELECT i.base_ccy AS aktywo, min(i.lot_size) AS lot,"
        "       max(i.min_notional) AS min_wartosc, max(c.close) AS cena"
        "  FROM ref.instrument i"
        "  LEFT JOIN LATERAL ("
        "        SELECT k.close FROM market.candle k"
        "         WHERE k.instrument_id = i.id AND k.timeframe = '1m'"
        "           AND k.event_time > now() - interval '1 day'"
        "         ORDER BY k.event_time DESC LIMIT 1) c ON true"
        " WHERE i.active AND i.engine::text = %s AND i.base_ccy IS NOT NULL"
        " GROUP BY i.base_ccy", (engine,))

    progi: dict[str, Decimal] = {}
    for r in rows:
        kandydaci = [Decimal(str(r["lot"]))] if r["lot"] else []
        if r["min_wartosc"] and r["cena"] and Decimal(str(r["cena"])) > 0:
            kandydaci.append(Decimal(str(r["min_wartosc"]))
                             / Decimal(str(r["cena"])))
        if kandydaci:
            progi[r["aktywo"].upper()] = max(kandydaci)
    return progi



# --------------------------------------------------------------- z wypelnien

def pozycje_z_wypelnien(wypelnienia, baza_symbolu: dict[str, str]) -> dict[str, Decimal]:
    """Pozycja odtworzona z REJESTRU TRANSAKCJI GIELDY. Czysta funkcja.

    TO NIE JEST TO SAMO PYTANIE CO SALDO i mieszanie ich byloby najgorszym
    mozliwym uproszczeniem:

      * saldo odpowiada „ile aktywa naprawde trzymamy" — lapie takze reczne
        zamkniecie w panelu, przelew, wyplate i wszystko, co nie jest handlem,
      * wypelnienia odpowiadaja „czy nasz dziennik zgadza sie z tym, co gielda
        zapisala jako nasze transakcje" — lapia zlecenie, ktore weszlo bez
        naszej wiedzy, i zlecenie zapisane u nas, a nieistniejace u niej.

    Odtworzenie jest wiec sprawdzeniem SLABSZYM, ale niezaleznym. Na hoscie,
    ktory sald nie wystawia, jest jedynym dostepnym — i lepszym niz nic,
    pod warunkiem ze nikt go nie przedstawi jako uzgodnienia sald.

    PROWIZJA POBRANA W KUPIONYM AKTYWIE JEST ODEJMOWANA. Na spocie Bybit
    potrafi pobrac ja wlasnie tak; pominiecie tego zanizyloby odtworzona
    pozycje dokladnie o wartosc prowizji przy kazdym kupnie.

    WARTOSC UJEMNA ZOSTAJE W WYNIKU I JEST W TYM CELOWA. Na spocie nie da sie
    miec ujemnej ilosci, wiec ujemny wynik odtworzenia znaczy jedno: okno
    rejestru urwalo kupno i zostawilo sprzedaz. Odfiltrowanie takiej wartosci
    (`v > 0` zamiast `v != 0`) zamienialoby BLAD POMIARU w cichy brak wpisu,
    czyli w „aktywa nie mamy" — a to jest zdanie prawdziwie brzmiace i falszywe.
    `pokrycie_wypelnien` lapie ten przypadek tylko wtedy, gdy mamy otwarta
    pozycje; przy pustym dzienniku strazy nie ma zadnej. Ujemna wartosc musi
    wiec dojsc do `rozjazdy`, ktore nazywa ja `ilosc-ujemna`.
    """
    stan: dict[str, Decimal] = {}
    for w in wypelnienia:
        baza = baza_symbolu.get(w.symbol)
        if baza is None:
            # Symbol spoza naszego uniwersum. NIE POMIJAMY GO PO CICHU tylko
            # wtedy, gdy dotyczy aktywa, ktore mamy w dzienniku - a tego tutaj
            # nie wiemy. Pomijamy, a rozjazd i tak wyjdzie po stronie „u nas".
            continue
        znak = Decimal(1) if w.strona.lower() == "buy" else Decimal(-1)
        stan[baza] = stan.get(baza, Decimal(0)) + znak * w.ilosc
        if w.prowizja_w and w.prowizja_w.upper() == baza.upper():
            stan[baza] -= w.prowizja
    return {k: v for k, v in stan.items() if v != 0}


def pokrycie_wypelnien(wypelnienia, najstarsze_otwarcie) -> str | None:
    """Czy rejestr gieldy siega tak daleko wstecz, jak nasz dziennik.

    NAJGROZNIEJSZY PRZYPADEK TEJ SCIEZKI. Gielda przechowuje wypelnienia przez
    ograniczony czas i potrafi zignorowac zbyt stary `startTime`, oddajac
    milczaco samo okno retencji. Odtworzenie z takiego okna pomija wszystko,
    co bylo wczesniej — czyli zaniza pozycje i produkuje falszywy alarm
    „pozycja-widmo" o pozycji, ktora naprawde istnieje.

    Zwraca powod, gdy pokrycia brak; None, gdy jest.
    """
    if najstarsze_otwarcie is None:
        return None                      # nie mamy pozycji — nie ma czego pokrywac
    if not wypelnienia:
        return ("gielda nie oddala zadnego wypelnienia, a nasz dziennik ma "
                f"pozycje otwarta {najstarsze_otwarcie:%Y-%m-%d %H:%M} — "
                "odtworzenie nie pokrywa naszej historii")
    najstarsze_u_gieldy = min(w.czas for w in wypelnienia)
    if najstarsze_u_gieldy > najstarsze_otwarcie:
        return (f"rejestr gieldy zaczyna sie {najstarsze_u_gieldy:%Y-%m-%d %H:%M}, "
                f"a nasza najstarsza otwarta pozycja pochodzi z "
                f"{najstarsze_otwarcie:%Y-%m-%d %H:%M} — okno retencji nie "
                "pokrywa naszej historii, wiec odtworzona pozycja bylaby zanizona")
    return None

async def aktywa_uniwersum(fetch_all, engine: str) -> set[str]:
    """Aktywa bazowe WSZYSTKICH instrumentow silnika, nie tylko trzymanych.

    DLACZEGO NIE WYSTARCZY LISTA NASZYCH POZYCJI

    Filtr zbudowany z tego, co mamy w dzienniku, wycina dokladnie ten przypadek,
    dla ktorego uzgodnienie istnieje: aktywo, o ktorym gielda wie, a my NIE.
    A to jest wlasnie kierunek "saldo-nieznane" - pozycja, ktorej nikt nie
    pilnuje, bo system o niej nie slyszal.

    `rozjazdy()` obsluguje ten przypadek poprawnie i jej docstring mowi wprost,
    ze aktywo znane jednej stronie tez jest rozjazdem. Wywolujacy to
    uniewazniał, wycinajac je dwie linijki wczesniej.

    Nadal NIE porownujemy wszystkiego, co gielda trzyma: waluta kwotowana
    i resztki po operacjach spoza naszego uniwersum nie sa pozycjami i ich
    obecnosc nie jest rozjazdem.
    """
    rows = await fetch_all(
        "SELECT DISTINCT base_ccy FROM ref.instrument"
        " WHERE active AND engine::text = %s", (engine,))
    return {r["base_ccy"].upper() for r in rows if r["base_ccy"]}


async def nasze_pozycje(fetch_all, engine: str, mode: str) -> dict[str, Decimal]:
    """Ile którego aktywa trzymamy według naszego dziennika."""
    rows = await fetch_all(
        "SELECT i.base_ccy AS aktywo, sum(p.qty) AS ilosc"
        " FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NULL"
        " GROUP BY i.base_ccy", (engine, mode))
    return {r["aktywo"]: Decimal(str(r["ilosc"])) for r in rows if r["ilosc"]}


async def kontekst_wypelnien(fetch_all, engine: str, mode: str):
    """Mapa symbol->aktywo bazowe i data najstarszego otwarcia. Z bazy, nie z gieldy."""
    rows = await fetch_all(
        "SELECT i.symbol, i.base_ccy FROM ref.instrument i WHERE i.active")
    baza = {r["symbol"]: r["base_ccy"] for r in rows}
    r = await fetch_all(
        "SELECT min(p.opened_at) AS od FROM exec.position p"
        " WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NULL",
        (engine, mode))
    return baza, (r[0]["od"] if r else None)


async def uzgodnij(fetch_all, notify, adapter, engine: str = "crypto",
                   mode: str = "demo", sucho: bool = False,
                   kind: str = KIND_DOMYSLNY) -> int:
    """Porównuje nasz obraz z giełdą i zgłasza różnice.

    Zwraca liczbę rozjazdów. Zero znaczy zgodność, a nie brak sprawdzenia —
    różnicę widać w komunikacie.

    `kind` wskazuje wiersz CENNIKA, z którego wychodzi tolerancja względna;
    musi być ten sam, którym chodzi silnik (`loop.py --kind spot_eu`).
    """
    nasze = await nasze_pozycje(fetch_all, engine, mode)
    try:
        salda = {b.currency.upper(): Decimal(str(b.total))
                 for b in await adapter.fetch_balances()}
        zrodlo = "salda"
    except ExchangeError as e:
        # HOST BEZ SALD NIE JEST HOSTEM ZGODNYM. Zwrocenie zera powiedzialoby
        # "sprawdzone i w porzadku" o czyms, czego sprawdzic sie nie da. Tak
        # zachowuje sie demo podmiotu unijnego: 404 na kazdej sciezce do salda.
        #
        # Zostaje sciezka SLABSZA, ale niezalezna: odtworzenie pozycji
        # z rejestru transakcji gieldy. Odpowiada na wezsze pytanie i dlatego
        # jest OSOBNO NAZWANA az do komunikatu wlacznie.
        if e.code != "404":
            raise Nieuzgadnialne(
                f"odczyt sald nie powiodl sie ({e}); uzgodnienie NIEWYKONALNE") from e
        baza, najstarsze = await kontekst_wypelnien(fetch_all, engine, mode)
        try:
            wyp = await adapter.wypelnienia(
                od_ms=int(najstarsze.timestamp() * 1000) if najstarsze else None)
        except ExchangeError as e2:
            raise Nieuzgadnialne(
                f"ten host nie wystawia sald ({e}), a rejestru wypelnien nie da "
                f"sie pobrac ({e2}) — uzgodnienie NIEWYKONALNE") from e2
        brak = pokrycie_wypelnien(wyp, najstarsze)
        if brak:
            raise Nieuzgadnialne(f"ten host nie wystawia sald, a {brak}")
        salda = pozycje_z_wypelnien(wyp, baza)
        zrodlo = "wypelnienia"

    # ZAKRES Z UNIWERSUM, NIE Z DZIENNIKA.
    #
    # Pierwsza wersja filtrowała po `set(nasze)`, czyli po aktywach, które JUŻ
    # znamy — i wycinała przez to dokładnie ten przypadek, dla którego całe
    # uzgodnienie istnieje: aktywo, o którym giełda wie, a my nie. Sprawdzenie
    # meldowało wtedy zgodność, mając przed sobą pozycję-widmo.
    #
    # Waluta kwotowana i resztki spoza uniwersum nadal odpadają — nie są
    # pozycjami i ich obecność nie jest rozjazdem.
    interesujace = (await aktywa_uniwersum(fetch_all, engine)) | set(nasze)
    salda = {k: v for k, v in salda.items() if k in interesujace}

    # PROGI Z BAZY, NIE ZE STALEJ — patrz `progi_bezwzgledne` i
    # `tolerancja_wzgledna`. Oba pytania zadajemy TUTAJ, zeby samo porownanie
    # zostalo czysta funkcja dwoch slownikow.
    progi = await progi_bezwzgledne(fetch_all, engine)
    r = rozjazdy(nasze, salda, tolerancja_wzgledna(kind), progi)
    if not r and not nasze and not salda:
        # PUSTO PO OBU STRONACH TO NIE JEST UZGODNIENIE. Zgadzaja sie dwa puste
        # zbiory, czyli nie porownano niczego. Komunikat "zgodne" powiedzialby
        # o tym stanie dokladnie tyle samo, co o stanie faktycznie sprawdzonym -
        # a to na jego podstawie decydujemy o dopuszczeniu trybu do handlu.
        print(f"uzgodnienie {engine}/{mode}: brak otwartych pozycji po obu "
              f"stronach — ścieżka działa, ale NIC NIE ZOSTAŁO PORÓWNANE")
        return 0
    if not r:
        if zrodlo == "salda":
            print(f"uzgodnienie {engine}/{mode}: zgodne z saldami giełdy "
                  f"({len(nasze)} aktywów w dzienniku)")
        else:
            # NIGDY nie piszemy tu "zgodne z saldami". Sprawdzone zostalo
            # wezsze pytanie i komunikat musi to niesc, bo to on trafia do
            # decyzji o dopuszczeniu trybu do handlu.
            print(f"uzgodnienie {engine}/{mode}: zgodne z REJESTREM TRANSAKCJI "
                  f"giełdy ({len(nasze)} aktywów) — UWAGA: ten host nie wystawia "
                  f"sald, więc ręczne zamknięcie w panelu i przelewy pozostają "
                  f"niesprawdzone")
        return 0

    for x in r:
        print(f"  ROZJAZD {x['rodzaj']} {x['aktywo']}: u nas {x['u_nas']}, "
              f"u giełdy {x['u_gieldy']} (różnica {x['roznica']:+})")

    if not sucho:
        tresc = "\n".join(
            f"{x['aktywo']}: u nas {x['u_nas']}, u giełdy {x['u_gieldy']} "
            f"({x['rodzaj']})" for x in r)
        # KAZDY RODZAJ MA WLASNE ZDANIE, bo kazdy wysyla czlowieka gdzie indziej.
        # Wspolny akapit o pozycji-widmie kazalby przy `ilosc-ujemna` szukac
        # aktywa na gieldzie, podczas gdy szukac trzeba brakujacej polowy
        # rejestru — czyli w zupelnie innym miejscu i z innym wnioskiem.
        wyjasnienia = {
            "pozycja-widmo": ("pozycja-widmo blokuje wejścia, których nic nie "
                              "blokuje"),
            "saldo-nieznane": "saldo nieznane to ekspozycja poza limitami",
            "ilosc-ujemna": ("ilość ujemna jest NIEMOŻLIWA na spocie — to nie "
                             "jest stan rachunku, tylko BŁĄD POMIARU: rejestr "
                             "giełdy oddał sprzedaż bez odpowiadającego jej "
                             "kupna, więc odtworzenie liczy z urwanego okna "
                             "i żadnej z pozostałych liczb nie wolno ufać"),
        }
        obecne = [wyjasnienia[k] for k in
                  ("pozycja-widmo", "saldo-nieznane", "ilosc-ujemna")
                  if any(x["rodzaj"] == k for x in r)]
        for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
            await notify(
                u["id"], "killswitch.triggered",
                f"Nasz stan nie zgadza się z giełdą ({engine})",
                f"{tresc}\n\n" + "; ".join(obecne) +
                ". Wymaga wyjaśnienia PRZED kolejnym zleceniem.",
                engine=engine, severity="critical",
                data={"rozjazdy": [{k: str(v) for k, v in x.items()} for x in r]})
    return len(r)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--sucho", action="store_true")
    # `--kind` MUSI byc ten sam, ktorym chodzi silnik (values.yaml,
    # workers.crypto: `--kind spot_eu`). Ten sam argument, ktory uzasadnia te
    # linie przy `mozliwosci.py --stawki`: prog policzony z wiersza CENNIKA,
    # ktorego nikt nie uzywa, opisuje inny rachunek niz uzgadniany.
    ap.add_argument("--kind", default=KIND_DOMYSLNY,
                    choices=tuple(sorted(CENNIK)),
                    help="rodzaj instrumentu — wiersz CENNIKA dla tolerancji")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_all, open_pool
    from services.api.notifications import notify
    from services.api.security import SecretBox
    from services.exchanges.bybit import BybitAdapter
    from services.exchanges.mozliwosci import wybierz_klucz

    await open_pool()
    try:
        # NAJNOWSZY KLUCZ, TEN SAM CO U SILNIKA — i tu waży to więcej niż
        # gdziekolwiek indziej. Uzgodnienie istnieje po to, żeby wyłapać
        # rozjazd między naszym obrazem stanu a obrazem giełdy. Pytanie
        # zadane starym kluczem trafia na INNY RACHUNEK niż ten, którym
        # handluje `loop.zbuduj_adapter`, więc narzędzie do wykrywania
        # rozjazdu samo by go produkowało: zgłaszałoby pozycje-widma
        # i salda nieznane, których nie ma, albo — groźniej — meldowałoby
        # zgodność pustego rachunku, gdy na handlowanym coś zostało otwarte.
        cred, ostrzezenie = wybierz_klucz(await fetch_all(
            "SELECT c.id, c.key_hint, c.created_at, c.key_enc, c.secret_enc,"
            "       v.demo_host, v.live_host"
            " FROM app.api_credential c"
            " JOIN ref.venue v ON v.id = c.venue_id"
            " WHERE v.code = 'bybit' AND c.mode::text = %s AND c.active"
            " ORDER BY c.created_at DESC, c.id DESC", (args.mode,)), args.mode)
        if ostrzezenie:
            print(f"uzgodnienie: UWAGA: {ostrzezenie}")
        if not cred:
            # BRAK KLUCZA TO NIE JEST ZGODNOSC. Milczace zero powiedzialoby
            # "sprawdzone i w porzadku" o czyms, czego nie sprawdzono.
            print(f"uzgodnienie: brak aktywnego klucza dla trybu {args.mode} — "
                  f"nie ma z czym porównać")
            return 0
        box = SecretBox()
        # HOST Z ref.venue, nie z tablicy w adapterze.
        #
        # Podmiot unijny ma wlasne hosty, a klucz z bybit.eu odbity od hosta
        # globalnego wraca z kodem 10003 - komunikatem nie do odroznienia od
        # zlego klucza. Adapter ostrzega przed tym w komentarzu, a mimo to
        # pierwsza wersja tego modulu podala sam tryb i dostala "API key is
        # invalid" na kluczu, ktory jest w porzadku.
        adapter = BybitAdapter(api_key=box.decrypt(bytes(cred["key_enc"])),
                               api_secret=box.decrypt(bytes(cred["secret_enc"])),
                               mode=args.mode,
                               host=cred["demo_host"] if args.mode == "demo"
                               else cred["live_host"])
        try:
            return await uzgodnij(fetch_all, notify, adapter,
                                  engine=args.engine, mode=args.mode,
                                  sucho=args.sucho, kind=args.kind)
        except Nieuzgadnialne as e:
            print(f"uzgodnienie {args.engine}/{args.mode}: NIEWYKONALNE — {e}")
            if args.mode == "demo":
                print(f"uzgodnienie: tryb demo (internal paper trading) — nie traktuję NIEWYKONALNE jako awarii czujki")
                return 0
            return 2
        finally:
            await adapter.close()
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
