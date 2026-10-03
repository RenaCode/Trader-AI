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
pozycji, a alarm krytyczny chodzi bez wyciszenia co dziesięć minut.
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
from datetime import timedelta
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.koszty import CENNIK, koszty  # noqa: E402
from services.exchanges.base import ExchangeError  # noqa: E402
# `recv_window` gieldy jest tu jedyna liczba opisujaca dopuszczalny rozjazd
# zegarow. Wlasna kopia bylaby druga - i pierwsza, ktora by sie rozjechala.
from services.exchanges.bybit import RECV_WINDOW  # noqa: E402

# Rodzaj instrumentu, którym chodzi silnik — decyduje o wierszu CENNIKA.
# Ten sam argument co przy `mozliwosci.py --stawki --kind spot_eu`: podmiot
# unijny ma takera 0,250%, czyli DWA I PÓŁ RAZA drożej niż globalny, a próg
# policzony z cudzego wiersza cennika nie opisuje rachunku, który uzgadniamy.
KIND_DOMYSLNY = "spot_eu"

# Kod powiadomienia o rozjezdzie (`notify.event_type`, migracja 0071).
KOD_ZDARZENIA = "reconcile.mismatch"


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


# ---------------------------------------------------------- zlecenia w locie

# ZLECENIE WYPELNIONE NA GIELDZIE, A JESZCZE NIE ZAKSIEGOWANE, TO NIE ROZJAZD
# (2026-10-03, audyt 3 T3).
#
# Ksiegujemy wypelnienie dopiero przy rozstrzyganiu planu (`gielda_exec.
# rozstrzygnij_plan`), czyli w najblizszym cyklu silnika PO wypelnieniu - a do
# tej chwili `exec.order.status` stoi na `new`. Uzgodnienie, ktore wpadnie w te
# minuty, widzi saldo bez naszej pozycji i krzyczalo `critical`:
#
#     NEAR 01.10: alarm 07:20:51, zaksiegowane 07:21:27 (zlecenie 81)
#     BTC  02.10: alarm 05:30:52, zaksiegowane 05:33:02 (zlecenie 83)
#
# Dwa alarmy krytyczne na cztery kupna w 72 h, oba z roznica rowna CALEMU
# zleceniu. Alarm krytyczny, ktory w polowie przypadkow jest falszywy, uczy go
# ignorowac - takze wtedy, gdy jest prawdziwy.
#
# DLACZEGO NIE GOLA KARENCJA CZASOWA. Karencja oslepialaby uzgodnienie na
# kazdy rozjazd z ostatnich minut, takze prawdziwy. Rozpoznanie zlecen w locie
# zwalnia WYLACZNIE roznice, ktora te zlecenia moga wyjasnic: na tym aktywie,
# w ich kierunku i nie wieksza niz ich ilosc. Pozycja-widmo przy otwartym
# kupnie nadal jest alarmem, bo kupno nie moze ZMNIEJSZYC salda.
#
# KIEDY ZLECENIE PRZESTAJE BYC „W LOCIE". Plan ma `valid_until`; po nim petla
# rozstrzyga go w najblizszym cyklu (co 300 s, `loop.py --interval`). Dwa
# cykle zapasu - potem `new` nie jest juz lotem, tylko rozstrzygnieciem, ktore
# nie nastapilo, i rozjazd wraca jako alarm.
KARENCJA_PO_WAZNOSCI_S = 2 * 300


def w_locie(r: list[dict], zlecenia: list[dict],
            progi: dict[str, Decimal] | None = None) -> tuple[list[dict], list[dict]]:
    """Dzieli rozjazdy na prawdziwe i wyjasnione zleceniami w locie. CZYSTA FUNKCJA.

    `zlecenia`: wiersze z `aktywo`, `strona` ('buy'/'sell') i `ilosc` -
    nierozstrzygniete zlecenia gieldowe silnika. Roznica `u_gieldy - u_nas`
    jest wyjasniona, gdy miesci sie w [-suma sprzedazy, +suma kupna] (plus prog
    bezwzgledny aktywa): kupno moze saldo tylko podniesc, sprzedaz tylko
    obnizyc, i zadne nie wiecej niz o swoja ilosc. `ilosc-ujemna` nie jest
    nigdy wyjasniona - to blad pomiaru, nie lot.
    """
    progi = progi or {}
    kupno: dict[str, Decimal] = {}
    sprzedaz: dict[str, Decimal] = {}
    for z in zlecenia:
        a = (z["aktywo"] or "").upper()
        cel = kupno if z["strona"] == "buy" else sprzedaz
        cel[a] = cel.get(a, Decimal(0)) + Decimal(str(z["ilosc"]))
    prawdziwe, wyjasnione = [], []
    for x in r:
        a = x["aktywo"].upper()
        prog = progi.get(x["aktywo"], Decimal(0))
        if (x["rodzaj"] != "ilosc-ujemna" and (a in kupno or a in sprzedaz)
                and -sprzedaz.get(a, Decimal(0)) - prog
                <= x["roznica"] <= kupno.get(a, Decimal(0)) + prog):
            wyjasnione.append(x)
        else:
            prawdziwe.append(x)
    return prawdziwe, wyjasnione


async def zlecenia_w_locie(fetch_all, engine: str, mode: str) -> list[dict]:
    """Nierozstrzygniete zlecenia gieldowe silnika, ktorych plan jeszcze zyje.

    Dwie doby od zlozenia to bezpiecznik dla planu bez `valid_until`: zaden
    plan silnika nie zyje dluzej niz swieca (4 h), wiec starsze `new` to
    zawieszone rozstrzygniecie, a nie lot - i nie moze niczego tlumaczyc.
    """
    return list(await fetch_all(
        "SELECT i.base_ccy AS aktywo, o.side AS strona, o.qty AS ilosc"
        "  FROM exec.order o"
        "  JOIN ref.instrument i ON i.id = o.instrument_id"
        "  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id"
        " WHERE o.engine::text = %s AND o.mode::text = %s"
        "   AND o.status IN ('new', 'partial', 'unknown')"
        "   AND o.raw->>'execution' = 'gielda'"
        "   AND (p.valid_until IS NULL"
        "        OR p.valid_until > now() - make_interval(secs => %s))"
        "   AND o.submitted_at > now() - interval '2 days'",
        (engine, mode, KARENCJA_PO_WAZNOSCI_S)))


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
        # KAZDY Z TRZECH AGREGATOW WYBIERA WARIANT DAJACY PROG MNIEJSZY, bo prog
        # ma odpowiadac na pytanie "czy resztki NIE DA SIE sprzedac na ZADNEJ
        # z par". Dla `min_notional` wariantem mniejszym jest MINIMUM, mimo ze
        # drugi czlon progu rosnie razem z nim: czlon ten brzmi
        # `min_wartosc / cena`, wiec najnizsze minimum wartosci daje najnizszy
        # prog. Do 2026-09-22 stalo tu `max` - a prog za duzy WYCISZA rozjazd,
        # ktory byl prawdziwy (ostrzezenie dwa akapity wyzej). Nie bylo to
        # teoretyczne: migracja 0023 zostawila pary USDT jako `active`, zdejmujac
        # im tylko `tradeable`, wiec `GROUP BY i.base_ccy` naprawde laczy dwie
        # pary tego samego aktywa o roznych minimach.
        "SELECT i.base_ccy AS aktywo, min(i.lot_size) AS lot,"
        "       min(i.min_notional) AS min_wartosc, max(c.close) AS cena"
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
    potrafi pobrac ja wlasnie tak; pominiecie tego ZAWYZYLOBY odtworzona
    pozycje dokladnie o wartosc prowizji przy kazdym kupnie — `execQty` gieldy
    jest BRUTTO, a na rachunku ladujemy netto.

    TRZY STANY, NIE DWA — I TO JEST SEDNO TEJ FUNKCJI

    Do 2026-09-23 funkcja liczyla jedna liczbe `znak*ilosc - prowizja` i miala
    na nia dwie odpowiedzi: dodatnia znaczyla „tyle trzymamy", ujemna —
    „rejestr urwany". Stan trzeci nie mial jak sie wypowiedziec, bo zero bylo
    odfiltrowane, a przy domknietej rundzie jedyna pozostala liczba jest ujemna:
    czlony ilosciowe kupna i sprzedazy znosza sie do zera i zostaje sam czlon
    prowizyjny. Zmierzone na produkcji 2026-09-23: ETHUSDC 0,3395 kupione
    i sprzedane w tym samym oknie, prowizja makera 0,3395 x 0,1% = 0,0003395
    pobrana w ETH -> odtworzenie oddawalo -0,0003395, `rozjazdy` nazywalo to
    `ilosc-ujemna`, a komunikat kazal szukac brakujacej polowy rejestru, ktora
    BYLA NA MIEJSCU. Poszlo 18 powiadomien `critical` w 2 h 40 min.

    Rozdzielamy wiec ILOSC od PROWIZJI i rozstrzygamy ZNAKIEM SAMEJ ILOSCI, bo
    to dwa rozne pytania i tylko pierwsze mowi cokolwiek o rejestrze:

      * `q < 0`  — w oknie jest sprzedaz bez swojego kupna. Zdanie O REJESTRZE:
                   okno naprawde jest urwane. Znak ZOSTAJE i dochodzi do
                   `rozjazdy` jako `ilosc-ujemna` — przy KAZDEJ ilosci, takze
                   ponizej `lot_size`. Zadnego progu tu nie ma i byc nie moze:
                   prog mierzy WIELKOSC roznicy, a tu zla jest sama liczba.
      * `q > 0`  — trzymamy `q` pomniejszone o prowizje bazowa. Pomiar.
      * `q == 0` — runda w oknie sie domknela. ZMIERZONE ZERO, nie brak pomiaru
                   — dlatego aktywo ZOSTAJE w wyniku z wartoscia 0, tak samo
                   jak `rozjazd_z_ulamkow` oddaje None zamiast zera przy braku
                   obserwacji. Klucz obecny z zerem znaczy „rejestr mowi, ze
                   nie mamy nic"; klucz NIEOBECNY znaczy „aktywa w oknie nie
                   bylo i rejestr nie mowi o nim nic". Obie odpowiedzi daja
                   w `rozjazdy` te sama liczbe, ale to nie sa te same zdania
                   i `uzgodnij` wypisuje pierwsze z nich osobno.

    DLACZEGO `max(q - f, 0)`, A NIE `q - f`. Przy `q >= 0` prowizja pomniejsza
    to, CO TRZYMAMY, i nie moze zabrac wiecej, niz w ogole bylo. Obciecie bladzi
    przy tym WYLACZNIE w strone alarmu: podnosi strone gieldy, wiec gdy cokolwiek
    trzymamy, roznica idzie w kierunku `pozycja-widmo`. Cisza zapada tylko wtedy,
    gdy obie strony mowia „nie mamy nic".

    CZEGO TU CELOWO NIE MA: progu bezwzglednego na resztce ujemnej. Prog
    `lot_size` nie wyciszylby nawet dzisiejszego alarmu (0,0003395 > 0,00001),
    a prog `min_notional/cena` wyciszylby razem z nim urwane okno przy ilosci
    0,0005 ETH — czyli BLAD POMIARU. To jest ta sama pomylka, ktora w pierwszej
    rundzie audytu ukryla pyl pozycji przed nadzorem, tylko postawiona odwrotna
    strona.
    """
    ilosci: dict[str, Decimal] = {}
    prowizje: dict[str, Decimal] = {}
    for w in wypelnienia:
        baza = baza_symbolu.get(w.symbol)
        if baza is None:
            # Symbol spoza naszego uniwersum. NIE POMIJAMY GO PO CICHU tylko
            # wtedy, gdy dotyczy aktywa, ktore mamy w dzienniku - a tego tutaj
            # nie wiemy. Pomijamy, a rozjazd i tak wyjdzie po stronie „u nas".
            continue
        znak = Decimal(1) if w.strona.lower() == "buy" else Decimal(-1)
        ilosci[baza] = ilosci.get(baza, Decimal(0)) + znak * w.ilosc
        if w.prowizja_w and w.prowizja_w.upper() == baza.upper():
            prowizje[baza] = prowizje.get(baza, Decimal(0)) + w.prowizja

    stan: dict[str, Decimal] = {}
    for baza, q in ilosci.items():
        f = prowizje.get(baza, Decimal(0))
        stan[baza] = (q - f) if q < 0 else max(q - f, Decimal(0))
    return stan


def pozycje_brutto(wiersze) -> list[dict]:
    """Pozycje zaksięgowane BRUTTO — czyli takie, których nie da się sprzedać.

    CZYSTA FUNKCJA — bez bazy i bez sieci. Wiersze daje `zaksiegowane_brutto`.

    DLACZEGO TO JEST OSOBNE SPRAWDZENIE, A NIE PRÓG W `rozjazdy`

    `rozjazdy` porównuje dwie sumy przez tolerancję, a tolerancja jest tu
    zbudowana DOKŁADNIE na wysokość jednej prowizji — jej własny docstring mówi
    to wprost: „zabezpiecza przypadek, w którym jedna strona przestaje odejmować
    prowizję pobraną w kupionym aktywie". Różnica brutto/netto ma dokładnie tę
    wysokość, więc jest dla tamtej metody NIEWIDOCZNA Z ZAŁOŻENIA, i to podwójnie:

        SOLUSDC, pozycja 32 (stan na 2026-09-24)
          u nas          12.138       (exec.position.qty, BRUTTO)
          z rejestru     12.125862    (execQty − execFee w SOL)
          różnica         0.012138
          próg bezwzględny  max(lot 0.001, min_notional 5 / 117.24) = 0.0427
          tolerancja        2 × taker(spot_eu) = 0.5%  =  0.0607
                                        różnica przechodzi POD OBOMA

    Obniżenie któregokolwiek z tych progów byłoby lekarstwem gorszym od choroby:
    próg bezwzględny wycisza pył po sprzedaży zaokrąglonej w dół, a tolerancja
    wycisza asymetrię księgowania prowizji, której `/v5/order/*` nie pozwala
    rozstrzygnąć. Obie mierzą WIELKOŚĆ różnicy, a tutaj wielkość nie ma
    znaczenia: 0,012 SOL fantomu blokuje sprzedaż CAŁYCH 12,138.

    TO SPRAWDZENIE NIE MA WIĘC PROGU, BO NIE PORÓWNUJE SUM — porównuje ILOŚĆ
    Z KSIĄG z GÓRNĄ GRANICĄ tego, co giełda mogła nam wydać:

        maksimum, jakie mogliśmy dostać  =  `exec.order.qty` − prowizja bazowa

    Gdy giełda pobrała prowizję w kupionym aktywie (`fee_ccy = base_ccy`),
    wypełnienie w całości MUSI dać mniej, niż zamówiliśmy — dokładnie o tę
    prowizję. `exec.position.qty` większe od tej granicy opisuje więc aktywo,
    którego na rachunku NIE MA, i każda sprzedaż całości padnie na brak salda.
    Żadna giełda ani żaden host nie jest do tego potrzebny, więc sprawdzenie
    działa także na `api-demo.bybit.eu`, który sald nie wystawia — czyli
    dokładnie tam, gdzie uzgodnienie sald jest niedostępne.

    MIERZYMY `exec.position.qty`, A NIE `exec.fill.qty`, I TO JEST ISTOTNE.
    Wyjście planuje ilość z POZYCJI, więc to ona decyduje, czy sprzedaż
    przejdzie. Wiersz wypełnienia może zostać zapisany brutto i nie zaszkodzić
    nikomu, jeżeli pozycja została potem skorygowana — a czujka, która wtedy
    nadal krzyczy, uczy odbiorcę, że można ją pomijać. Zmierzone 2026-09-24 po
    ręcznej korekcie pozycji 32: `fill.qty` nadal 12.138 (brutto), `position.qty`
    już 12.125862 (netto) — sprzedaż przechodzi, więc alarmu być nie może.

    WYPEŁNIENIE MUSI POKRYWAĆ CAŁE ZLECENIE, inaczej porównanie nie ma sensu:
    przy wypełnieniu częściowym granica `zlecono − prowizja` opisuje zlecenie,
    którego giełda w całości nie wykonała, a `p.qty` — tylko jego część. Stąd
    warunek `wypelniono + prowizja >= zlecono`, który przepuszcza zarówno
    wiersz netto (12.125862 + 0.012138 = 12.138), jak i brutto (12.138 + …),
    a odrzuca połówkę. CZEGO WIĘC NIE ZŁAPIE: pozycji zbudowanej z wypełnień
    częściowych. Milczymy tam zamiast zgadywać — ale mówimy, że milczymy.
    """
    out = []
    for w in wiersze:
        prowizja = Decimal(str(w["prowizja"] or 0))
        aktywo = (w["aktywo"] or "").upper()
        if prowizja <= 0 or (w["prowizja_w"] or "").upper() != aktywo:
            # Prowizja w walucie kwotowanej nie rusza ilosci — nie ma czego
            # odejmowac i nie ma tu zadnej wady.
            continue
        wypelniono = Decimal(str(w["wypelniono"]))
        zlecono = Decimal(str(w["zlecono"]))
        if wypelniono + prowizja < zlecono:
            continue
        ilosc = Decimal(str(w["ilosc"]))
        mozliwe = zlecono - prowizja
        if ilosc <= mozliwe:
            continue
        out.append({
            "pozycja": w["pozycja"],
            "symbol": w["symbol"],
            "aktywo": aktywo,
            "ilosc": ilosc,
            "zlecono": zlecono,
            "wypelniono": wypelniono,
            "prowizja": prowizja,
            # Ile aktywa nie ma na rachunku, choc stoi w ksiegach.
            "nadmiar": ilosc - mozliwe,
            "rodzaj": "pozycja-brutto",
        })
    return out


async def zaksiegowane_brutto(fetch_all, engine: str, mode: str) -> list[dict]:
    """Wiersze do `pozycje_brutto`: otwarte pozycje i ich wejściowe wypełnienia.

    Wejście pozycji rozpoznajemy po `open_decision_id` — to ta sama decyzja,
    która zrodziła zlecenie kupna. Wiązanie po samym instrumencie zebrałoby
    także zlecenia innych rund na tej samej parze.
    """
    return await fetch_all(
        "SELECT p.id AS pozycja, i.symbol AS symbol, i.base_ccy AS aktywo,"
        "       p.qty AS ilosc, o.qty AS zlecono, f.qty AS wypelniono,"
        "       f.fee AS prowizja, f.fee_ccy AS prowizja_w"
        "  FROM exec.position p"
        "  JOIN ref.instrument i ON i.id = p.instrument_id"
        "  JOIN exec.order o ON o.decision_id = p.open_decision_id"
        "                   AND o.instrument_id = p.instrument_id"
        "                   AND o.side = 'buy'"
        "  JOIN exec.fill f ON f.order_id = o.id"
        " WHERE p.closed_at IS NULL AND p.side = 'long'"
        "   AND p.engine::text = %s AND p.mode::text = %s"
        " ORDER BY p.id", (engine, mode))


def zakres_wypelnien(pozycje) -> tuple[object | None, object | None]:
    """Od kiedy pytac gielde o wypelnienia i wzgledem czego mierzyc pokrycie.

    CZYSTA FUNKCJA — bez bazy i bez sieci. `pozycje` to wiersze `exec.position`
    z kluczami `od` (chwila ZLOZENIA zlecenia otwierajacego), `opened_at`
    i `closed_at`. Zwraca `(poczatek_okna, najstarsze_otwarcie_w_oknie)`.

    OKNO MUSI OBEJMOWAC OBIE NOGI KAZDEJ RUNDY, KTOREJ SPRZEDAZ W NIM LEZY

    Do 2026-09-23 okno zaczynalo sie od `min(opened_at)` po pozycjach WYLACZNIE
    OTWARTYCH, a odtworzenie sumowalo KAZDE wypelnienie w oknie — takze
    nalezace do pozycji dawno zamknietych. Pozycja otwarta PRZED oknem
    i zamknieta WEWNATRZ niego wnosila wiec do sumy gola sprzedaz, czyli
    prawdziwa polowe rejestru, a straznik o niej nie wiedzial, bo nie mial jej
    w swoim zapytaniu. Na produkcji taki ksztalt istnieje: ETHUSDC 0,604367
    otwarte 2026-09-15, zamkniete 2026-09-17.

    Zbior pozycji „w oknie" zalezy od poczatku okna, a poczatek okna od tego
    zbioru, wiec jest to PUNKT STALY i liczymy go wprost. Kazdy obrot moze
    okno tylko COFNAC, a zbior pozycji jest skonczony — iteracja konczy sie
    zawsze. Bledy ida przy tym w strone BEZPIECZNA: okno za szerokie wciaga
    wylacznie rundy domkniete, ktore po rozdzieleniu ilosci od prowizji
    (`pozycje_z_wypelnien`) sumuja sie do zera i nie wnosza znaku ujemnego.
    Okno za waskie urwaloby kupno i dalo falszywy `ilosc-ujemna`.

    BRAK POZYCJI OTWARTYCH TO NIE JEST STAN PUSTY, tylko najgorszy z mozliwych,
    i dlatego nie oddajemy tu None. `od_ms = None` kaze adapterowi pobrac CALY
    dostepny rejestr, a wtedy kazda historyczna runda dokladala swoja resztke,
    natomiast `pokrycie_wypelnien` milczalo w pierwszej linii — straznika nie
    bylo zadnego. Zamiast tego siegamy do ostatniej rundy, o ktorej wiemy:
    okno zostaje ograniczone, a obie jej nogi sa w srodku.
    """
    pozycje = list(pozycje)
    if not pozycje:
        return None, None
    otwarte = [p for p in pozycje if p["closed_at"] is None]
    zaczep = otwarte or [max(pozycje, key=lambda p: p["od"])]
    od = min(p["od"] for p in zaczep)
    while True:
        # Pozycja bez `closed_at` jest w oknie zawsze; zamknieta — gdy jej
        # sprzedaz wpada do okna, bo wtedy odtworzenie i tak ja zsumuje.
        w_oknie = [p for p in pozycje
                   if p["closed_at"] is None or p["closed_at"] >= od]
        nowe = min(p["od"] for p in w_oknie)
        if nowe >= od:
            return od, min(p["opened_at"] for p in w_oknie)
        od = nowe


def pokrycie_wypelnien(wypelnienia, najstarsze_otwarcie) -> str | None:
    """Czy rejestr gieldy siega tak daleko wstecz, jak nasz dziennik.

    NAJGROZNIEJSZY PRZYPADEK TEJ SCIEZKI. Gielda przechowuje wypelnienia przez
    ograniczony czas i potrafi zignorowac zbyt stary `startTime`, oddajac
    milczaco samo okno retencji. Odtworzenie z takiego okna pomija wszystko,
    co bylo wczesniej — czyli zaniza pozycje i produkuje falszywy alarm
    „pozycja-widmo" o pozycji, ktora naprawde istnieje.

    ODNIESIENIEM JEST `opened_at` NAJSTARSZEJ POZYCJI W OKNIE, a nie najstarszej
    OTWARTEJ — ten sam zbior, ktory `zakres_wypelnien` uznalo za sumowany przez
    odtworzenie. Straznik mierzy pokrycie tego, co naprawde wchodzi do sumy;
    mierzenie go wzgledem wezszego zbioru zostawialo strukturalna slepa plamke
    dokladnie na przypadku, dla ktorego straznik powstal.

    MIERZYMY WZGLEDEM `opened_at`, NIE WZGLEDEM POCZATKU OKNA. Okno zaczyna sie
    od chwili ZLOZENIA zlecenia, a wypelnienie jest z definicji pozniejsze —
    porownanie z poczatkiem okna alarmowaloby przy kazdym przebiegu. `opened_at`
    jest natomiast chwila, w ktorej zapisalismy skutek wypelnienia, wiec
    wypelnienie tworzace te pozycje musi byc od niej WCZESNIEJSZE albo rowne.

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
                f"a nasza najstarsza pozycja w oknie pochodzi z "
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
    """Mapa symbol->aktywo bazowe i wiersze pozycji dla `zakres_wypelnien`.

    `od` TO CHWILA ZLOZENIA ZLECENIA, NIE `opened_at` — I TO USUWA STALA

    Do 2026-09-23 okno liczylo sie jako `min(opened_at) - timedelta(days=1)`,
    gdzie doba byla stala wpisana wprost w ten plik. Naglowek modulu zabrania
    tego wprost („Zaden z nich nie jest stala wpisana w ten plik. Stala bylaby
    trzecim miejscem, w ktorym trzyma sie reguly gieldy"), a liczba i tak byla
    zla w obie strony: bufor mial pokryc opoznienie miedzy wypelnieniem
    a zapisem `opened_at`, ktore przy swiecy H4H wynosi do dlugosci swiecy
    (zmierzone na zleceniu 47: 13:20:51 -> 16:00:35, czyli 2 h 40 min), wiec
    doba byla o rzad wielkosci za szeroka — a przy swiecy dobowej byla by za
    waska i wycinala kupno tworzace pozycje.

    Zadnej stalej nie trzeba, bo dokladna odpowiedz jest w naszych wlasnych
    zapisach: gielda nie moze wypelnic zlecenia, zanim je do niej wyslemy,
    a `exec.order.submitted_at` trzyma chwile wyslania. `LEAST` z `opened_at`
    zabezpiecza wiersze bez zlecenia (rachunek papierowy, dane historyczne):
    `LEAST` pomija NULL-e, wiec brak zlecenia zostawia samo `opened_at`, czyli
    zachowanie sprzed tej zmiany.

    BIERZEMY `min(submitted_at)`, A NIE ZLECENIE WYBRANE PO STRONIE. Zlecenie
    odrzucone albo ponowione na tej samej decyzji ma czas WCZESNIEJSZY, czyli
    przesuwa okno wstecz — a to jest kierunek bezpieczny (patrz
    `zakres_wypelnien`: szersze okno wciaga tylko rundy domkniete).
    """
    rows = await fetch_all(
        "SELECT i.symbol, i.base_ccy FROM ref.instrument i WHERE i.active")
    baza = {r["symbol"]: r["base_ccy"] for r in rows}
    # WSZYSTKIE pozycje trybu, nie tylko otwarte — punkt staly w
    # `zakres_wypelnien` potrzebuje takze zamknietych, zeby zobaczyc rundy,
    # ktorych sprzedaz wpada w okno. Wiersz na pozycje, a pozycji jest
    # kilkadziesiat na miesiac handlu, wiec odczyt calosci jest tanszy niz
    # iterowanie punktu stalego zapytaniami.
    pozycje = await fetch_all(
        "SELECT p.opened_at, p.closed_at,"
        "       LEAST(p.opened_at, min(o.submitted_at)) AS od"
        "  FROM exec.position p"
        "  LEFT JOIN exec.order o ON o.decision_id = p.open_decision_id"
        " WHERE p.engine::text = %s AND p.mode::text = %s"
        " GROUP BY p.id, p.opened_at, p.closed_at", (engine, mode))
    return baza, pozycje


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
        baza, pozycje = await kontekst_wypelnien(fetch_all, engine, mode)
        od, najstarsze = zakres_wypelnien(pozycje)
        try:
            # MARGINES TO ROZJAZD ZEGAROW, A NIE BUFOR NA OPOZNIENIE ZAPISU.
            # Opoznienie zapisu zniklo razem ze stala - okno zaczyna sie od
            # `submitted_at` (patrz `kontekst_wypelnien`). Zostaje jedno:
            # `startTime` porownuje gielda swoim zegarem, a nasz moze byc
            # rozjechany. O ile - mowi sama gielda: `recv_window` to granica,
            # powyzej ktorej odrzuca KAZDE podpisane zapytanie, wiec zegar
            # mieszczacy sie w handlu miesci sie i tutaj. Ta sama liczba, ktora
            # mierzy `BybitAdapter.dryf_zegara`, a nie druga jej kopia.
            margines = timedelta(milliseconds=int(RECV_WINDOW))
            od_ms = int((od - margines).timestamp() * 1000) if od else None
            wyp = await adapter.wypelnienia(od_ms=od_ms)
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
    r, lot = w_locie(r, await zlecenia_w_locie(fetch_all, engine, mode), progi)
    for x in lot:
        print(f"  W LOCIE {x['aktywo']}: u nas {x['u_nas']}, u giełdy "
              f"{x['u_gieldy']} (różnica {x['roznica']:+}) — wyjaśnia ją "
              f"nierozstrzygnięte zlecenie; zaksięguje się przy rozstrzygnięciu "
              f"planu, to NIE jest alarm")

    # POZYCJA ZAKSIEGOWANA BRUTTO — SPRAWDZENIE BEZ PROGU I BEZ GIELDY.
    #
    # `rozjazdy` tego nie widzi i nie ma jak zobaczyc: roznica brutto/netto ma
    # dokladnie wysokosc jednej prowizji, czyli tyle, ile tolerancja ma z
    # zalozenia pochlaniac (patrz `pozycje_brutto`). Dokladamy wiec sprawdzenie,
    # ktore porownuje nasze wlasne zapisy tego samego zlecenia, a nie dwie sumy.
    brutto = pozycje_brutto(await zaksiegowane_brutto(fetch_all, engine, mode))
    for b in brutto:
        print(f"  POZYCJA BRUTTO {b['symbol']} (id {b['pozycja']}): w księgach "
              f"{b['ilosc']}, a giełda mogła wydać najwyżej "
              f"{b['zlecono'] - b['prowizja']} (wysłano {b['zlecono']}, prowizja "
              f"{b['prowizja']} {b['aktywo']} pobrana w kupionym aktywie) — "
              f"o {b['nadmiar']} za dużo, więc sprzedaż CAŁOŚCI padnie na brak salda")

    # ZMIERZONE ZERO MA WLASNE ZDANIE NA EKRANIE, bo inaczej trzeci stan
    # odtworzenia (`pozycje_z_wypelnien`) zamienialby sie w cisze — a zamiana
    # falszywego alarmu na cisze bylaby gorsza od samego falszywego alarmu.
    # Czytelnik musi widziec roznice miedzy „rejestr mowi, ze rundy sie
    # domknely" a „rejestru o tym aktywie w ogole nie pytalismy".
    domkniete = sorted(k for k, v in salda.items() if v == 0)
    if domkniete and zrodlo == "wypelnienia":
        print(f"uzgodnienie {engine}/{mode}: rundy domknięte w oknie rejestru "
              f"({', '.join(domkniete)}) — giełda potwierdza, że nie trzymamy "
              f"tych aktywów; to jest ZMIERZONE ZERO, a nie brak pomiaru")

    # PUSTO liczymy po wartosciach ZYWYCH. Zmierzone zero jest odpowiedzia
    # rejestru, ale nie jest pozycja — komunikat „nic nie porownano" ma
    # opisywac stan, w ktorym zadna z dwoch stron nie wniosla do porownania
    # niczego, co mozna by trzymac.
    zywe = {k: v for k, v in salda.items() if v != 0}
    if not r and not brutto and not nasze and not zywe and not lot:
        # PUSTO PO OBU STRONACH TO NIE JEST UZGODNIENIE. Zgadzaja sie dwa puste
        # zbiory, czyli nie porownano niczego. Komunikat "zgodne" powiedzialby
        # o tym stanie dokladnie tyle samo, co o stanie faktycznie sprawdzonym -
        # a to na jego podstawie decydujemy o dopuszczeniu trybu do handlu.
        print(f"uzgodnienie {engine}/{mode}: brak otwartych pozycji po obu "
              f"stronach — ścieżka działa, ale NIC NIE ZOSTAŁO PORÓWNANE")
        return 0
    if not r and not brutto:
        if zrodlo == "salda":
            print(f"uzgodnienie {engine}/{mode}: zgodne z saldami giełdy "
                  f"({len(nasze)} aktywów w dzienniku)")
        else:
            # TO NIE JEST ZGODNOSC, TYLKO LUKA POMIAROWA — I MUSI TAK BRZMIEC.
            #
            # Do 2026-09-24 stalo tu zdanie zaczynajace sie od slowa „zgodne",
            # z zastrzezeniem doklejonym w tej samej linii. Czytelnik (i czujka,
            # ktorej wydruk to trafia) zapamietywal pierwsze slowo. Tymczasem
            # odtworzenie z rejestru transakcji odpowiada na pytanie WEZSZE od
            # zadanego: odejmuje prowizje po obu stronach, wiec rozjazd polegajacy
            # WLASNIE na prowizji jest dla niego matematycznie niewidoczny —
            # i tak wlasnie pozycja SOLUSDC, ktorej nie dalo sie sprzedac,
            # przeszla przez uzgodnienie jako „zgodna".
            #
            # Zdanie zaczyna sie wiec od tego, czego NIE zmierzono, a dopiero
            # potem mowi, co zmierzono.
            print(f"uzgodnienie {engine}/{mode}: LUKA POMIAROWA — ten host nie "
                  f"wystawia sald, więc uzgodnienia sald NIE BYŁO. Zmierzono "
                  f"pytanie węższe: {len(nasze)} aktywów zgadza się z REJESTREM "
                  f"TRANSAKCJI giełdy. Pozostają niesprawdzone: ręczne zamknięcie "
                  f"w panelu, przelewy oraz każdy rozjazd wielkości prowizji "
                  f"(rejestr odejmuje ją po obu stronach)")
        return 0

    for x in r:
        print(f"  ROZJAZD {x['rodzaj']} {x['aktywo']}: u nas {x['u_nas']}, "
              f"u giełdy {x['u_gieldy']} (różnica {x['roznica']:+})")

    if not sucho:
        tresc = "\n".join(
            [f"{x['aktywo']}: u nas {x['u_nas']}, u giełdy {x['u_gieldy']} "
             f"({x['rodzaj']})" for x in r]
            + [f"{b['symbol']} (pozycja {b['pozycja']}): w księgach {b['ilosc']}, "
               f"na rachunku o {b['nadmiar']} {b['aktywo']} mniej — prowizja "
               f"pobrana w kupionym aktywie nie zeszła z ilości (pozycja-brutto)"
               for b in brutto])
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
            "pozycja-brutto": ("pozycja-brutto jest NIE DO SPRZEDANIA: wyjście "
                               "planuje całą ilość z ksiąg, a na rachunku leży "
                               "o prowizję mniej, więc giełda odrzuca CAŁOŚĆ za "
                               "brak salda — i powtórzy to przy każdej świecy. "
                               "Naprawą jest skorygowanie `exec.position.qty` "
                               "do ilości faktycznie posiadanej, nie ponawianie "
                               "sprzedaży"),
        }
        obecne = [wyjasnienia[k] for k in
                  ("pozycja-widmo", "saldo-nieznane", "ilosc-ujemna")
                  if any(x["rodzaj"] == k for x in r)]
        if brutto:
            obecne.append(wyjasnienia["pozycja-brutto"])
        # WLASNY KOD ZDARZENIA (migracja 0071). Do 2026-10-03 szlo to jako
        # `killswitch.triggered`, choc zaden kill-switch nie zadzialal - i alarm
        # uzgodnienia tlumil w dozorze przypomnienie o PRAWDZIWYM kill-switchu.
        for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
            await notify(
                u["id"], KOD_ZDARZENIA,
                f"Nasz stan nie zgadza się z giełdą ({engine})",
                f"{tresc}\n\n" + "; ".join(obecne) +
                ". Wymaga wyjaśnienia PRZED kolejnym zleceniem.",
                engine=engine, severity="critical",
                data={"rozjazdy": [{k: str(v) for k, v in x.items()}
                                   for x in list(r) + list(brutto)]})
    # POZYCJE BRUTTO LICZA SIE DO WYNIKU. Zwracana liczba jest kodem wyjscia
    # zadania cyklicznego, wiec zwrocenie samego `len(r)` zostawialoby Job na
    # zielono przy pozycji, ktorej nie da sie zamknac — czyli dokladnie ten
    # ksztalt milczenia, ktory kosztowal 19 godzin.
    return len(r) + len(brutto)


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
            #
            # I DLATEGO KOD 2, NIE 0 (audyt 2026-10-03, G3). Do tej zmiany ten
            # komentarz stal nad `return 0` - czyli kod robil dokladnie to,
            # przed czym komentarz ostrzegal: Job czujki konczyl sie na zielono,
            # a linia w logu byla jedynym sladem. Ten sam kod co
            # `Nieuzgadnialne` nizej, bo to ta sama sytuacja: porownanie
            # NIEWYKONALNE, a nie wykonane i zgodne.
            print(f"uzgodnienie {args.engine}/{args.mode}: NIEWYKONALNE — brak "
                  f"aktywnego klucza dla trybu {args.mode}, nie ma z czym porównać")
            return 2
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
            # W demo TEZ jest to awaria czujki. Wyciszenie z 15.09.2026 (return 0
            # dla demo) powstalo, gdy silnik chodzil na rachunku papierowym
            # i uzgodnienie nie mialo czego porownywac; od 22.09 demo znow
            # wysyla zlecenia na gielde, a uzgodnienie, ktore milczy, gdy nie
            # umie porownac, jest gorsze niz jego brak.
            return 2
        finally:
            await adapter.close()
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
