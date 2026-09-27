"""LIKWIDACJA otwartych pozycji po kill-switchu — zleceniem RYNKOWYM.

CO KILL-SWITCH ROBIL DO 2026-09-27 I CZEGO NIE ROBIL

Dwie rzeczy: wstrzymywal WEJSCIA (`loop.run_once`, lista `powody_wstrzymania`)
i zdejmowal niewypelnione zlecenia wejscia z arkusza
(`gielda_exec.anuluj_wejscia`). OTWARTA EKSPOZYCJA ZOSTAWALA. Wyjscia szly
dalej, ale jako `PostOnly`, czyli mogly sie nie wypelnic — a na spadajacym
rynku nie wypelniaja sie dokladnie wtedy, gdy trzeba wyjsc: cena ucieka od
pasywnej strony ksiegi, plan wygasa po dwoch swiecach, nastepna swieca planuje
wyjscie od nowa. Zmierzone 2026-09-27: 3 z 6 zlecen z limitem odrzucone przez
`EC_PostOnlyWillTakeLiquidity`. Bezpiecznik zatrzymywal wiec DOKLADANIE
ryzyka, a nie ryzyko.

CENA TEJ ZMIANY, ZAPISANA WPROST

Zlecenie rynkowe kosztuje stawke TAKER i poslizg, a poslizg takera jest w tym
systemie NIEZMIERZONY: `koszty.POSLIZG_TAKER = 0.0004` to zalozenie z n=0
(`poslizg.py`). Sciezki `execution='gielda' AND order_type='market'` nie uzyto
tu ANI RAZU — wszystkie 19 zlecen `market` w historii to `internal_paper`.
Poslizg zlecenia rynkowego jest z definicji NIEOGRANICZONY od gory: gielda
wypelnia po najlepszej dostepnej cenie, jakakolwiek by nie byla. Wlasciciel
zatwierdzil oba te wybory wprost 2026-09-27, po przedstawieniu kosztu.

DLACZEGO W OGOLE RYNKOWE, A NIE LIMIT PRZY RYNKU

Bo pytanie brzmi „ile kosztuje NIEWYJSCIE", a nie „ile kosztuje wyjscie".
Limit po agresywnej stronie spreadu jest odrzucany przez `PostOnly`, a bez
`PostOnly` jest i tak takerem — tylko bez gwarancji wypelnienia calosci. Jedyna
rzecz, ktora zlecenie rynkowe kupuje, jest ta jedna, ktorej kill-switch
potrzebuje: WYKONANIE TERAZ.

--------------------------------------------------------------------------
PROBA POZAROWA: DLACZEGO DRILL NIE ZAMYKA POZYCJI CO DOBE
--------------------------------------------------------------------------

`trader-proba-killswitcha` chodzi CODZIENNIE (03:21 UTC) i podnosi `min_equity`
powyzej kapitalu, zeby uruchomic PRAWDZIWY kill-switch — po to, by zmierzyc
caly lancuch. Podlaczenie likwidacji wprost do „kill-switch aktywny"
znaczyloby zamykanie wszystkich pozycji co dobe, zleceniem rynkowym, za pelny
poslizg i za nic. Dzis ekspozycja to 2 401,84 (2,4% kapitalu), wiec zalozenie
„drill i tak nie ma czego zamykac" jest FALSZYWE i nie wolno na nim niczego
oprzec.

ROZSTRZYGA ZNACZNIK, KTORY JUZ ISTNIEJE: `risk.limit_def.proba_do`

Migracja 0045 dolozyla go po to, zeby czujka zywotnosci nie brala skutku proby
za awarie. Uzasadnienie jest tam wypisane i obowiazuje bez zmian takze tutaj:
zdarzenie proby jest NIEODROZNIALNE od prawdziwego (ten sam limit, ten sam
powod, ten sam `RiskKernel`) i to jest celowe — proba mierzy prawdziwa sciezke.
Jedyne, co proba wie, a czego nie wie nikt inny, to ZE TRWA, i mowi to bazie,
ZANIM podniesie prog. Zbudowanie tu drugiego, wlasnego znacznika daloby dwa
zrodla tej samej prawdy, ktore rozjada sie przy pierwszej zmianie jednego.

WARUNEK JEST OSTRZEJSZY NIZ W `dozor.py` I ROZNICA JEST ISTOTNA.

Czujka pyta `proba_do > now() AND triggered_at >= updated_at`, czyli „na tym
limicie proba TRWA". Dla powiadomienia to wystarcza. Dla likwidacji NIE:
`proba_do` wygasa po `OKNO_PROBY_S` (28 min), a proba zabita SIGKILL-em (po
okresie karencji, `activeDeadlineSeconds`) nie wykonuje `finally` i zostawia
podniesiony prog razem z czynnym zdarzeniem. Po 28 minutach `proba_do > now()`
przestaje byc prawda, zdarzenie zaczyna wygladac na prawdziwe — i likwidacja
sprzedalaby wszystko po artefakcie proby. Dlatego pytamy BEZ `now()`:

    proba_do IS NOT NULL AND triggered_at >= updated_at

`proba_do` przeszle i wciaz nie-NULL to PODPIS PROBY, KTORA ZGINELA (tak samo
czyta go `tests/test_proba_a_prawdziwy_killswitch.py`), a nie dowod, ze proby
nie ma. Kierunek bledu jest przy tym wybrany swiadomie: w tym stanie likwidacja
NIE RUSZA i mowi to GLOSNO (licznik w pulsie silnika plus linia na ekranie),
zamiast placic pelny poslizg za cudzy niedokonczony proces. Prawdziwy
kill-switch przy zasmieconym `proba_do` tez nie zlikwiduje pozycji — i to jest
koszt, ktory trzeba znac: dopoki ktos nie wyczysci znacznika, likwidacja jest
wylaczona. Czujka zglasza ten stan osobno (`dozor.py`, limity z przeszlym
`proba_do`), wiec nie jest to cisza.

CO ZMIENILO SIE W SAMEJ PROBIE: KOLEJNOSC DWOCH ZAPISOW W `finally`.
Zwolnienie wlasnego zdarzenia idzie teraz PRZED przywroceniem progu
i skasowaniem `proba_do`. Wczesniej bylo odwrotnie, wiec miedzy tymi dwoma
zapytaniami istnialo okno, w ktorym zdarzenie jest czynne, a znacznika juz nie
ma — i cykl silnika trafiony w to okno zlikwidowalby pozycje. Okno bylo
milisekundowe, ale „milisekundy sie nie trafia" to premisa tego samego rodzaju,
co „drill nie ma czego zamykac".

--------------------------------------------------------------------------
IDEMPOTENCJA: TRZY WARSTWY, BO DWA RAZY SPRZEDANA POZYCJA TO POZYCJA KROTKA
--------------------------------------------------------------------------

Kill-switch jest wyzwalany w kazdym cyklu, a cykl chodzi co 300 s. Zdarzenie
zostaje czynne do RECZNEGO zwolnienia, wiec „aktywny kill-switch" widzi ta
funkcja dziesiatki razy pod rzad.

  1. BAZA, ATOMOWO. `INSERT INTO exec.likwidacja ... ON CONFLICT
     (killswitch_event_id, position_id) DO NOTHING RETURNING id`. Brak
     zwroconego wiersza znaczy „ta para juz byla" i jest to rozstrzygniecie
     JEDNEGO zapytania, a nie wynik odczytu i porownania — dwa procesy nie
     przepchna sie tedy oba.
  2. GIELDA. `client_order_id` jest wyprowadzony z pary (zdarzenie, pozycja),
     nie z czasu i nie losowo, a `zloz_zlecenie` w obu adapterach PYTA o ten
     identyfikator przed wyslaniem. To ta sama ochrona, ktora maja plany
     (`gielda_exec.id_zlecenia`).
  3. `exec.order` ma `UNIQUE (mode, client_order_id)`, wiec drugi wiersz dla
     tego samego zlecenia odrzucilaby baza.

JEDNA PROBA NA POZYCJE NA ZDARZENIE — I TO JEST DECYZJA, NIE OGRANICZENIE
TECHNICZNE. Wypelnienie CZESCIOWE zostawia reszte pozycji otwarta; tej reszty
NIE GONIMY. Gonienie wymagaloby odpowiedzi na pytanie „ile juz sprzedalismy"
w chwili, w ktorej odpowiedz gieldy wlasnie nie dotarla — a kazda pomylka w te
strone jest sprzedaza czegos, czego nie mamy. Reszta jest ZAPISANA
(`exec.likwidacja.stan = 'czesciowa'` z obiema iloscami), zwykla sciezka
wyjscia dziala dalej (kill-switch nigdy nie blokuje wyjsc), a kolejne zdarzenie
kill-switcha dostanie nowe `killswitch_event_id`, czyli prawo do nowej proby.

--------------------------------------------------------------------------
POMIAR POSLIZGU TAKERA: PO CO TA TABELA MA TRZY CENY
--------------------------------------------------------------------------

`poslizg.py` odrzuca wiersze rachunku papierowego, bo tam cena wypelnienia to
`cena * (1 +- POSLIZG)` — pomiar bylby echem zalozenia. Dla zlecen z limitem
z gieldy jest gorzej niz „za malo danych": zmierzone na produkcji 2026-09-27
WSZYSTKIE 35 wypelnionych zlecen z limitem ma `exec.order.limit_price` rowne
`exec.fill.price` CO DO OSTATNIEJ CYFRY, bo limit wypelnia sie po cenie limitu
z definicji. Poslizg cenowy makera nie jest mala liczba — on nie istnieje.

Dla zlecenia rynkowego ta rownosc NIE zachodzi i dopiero to jest pomiarem.
Warunkiem jest cena odniesienia SPRZED wyslania, a zlecenie rynkowe nie ma
`limit_price`, w ktorym daloby sie ja zapisac. Dlatego `exec.likwidacja` trzyma
trzy liczby z JEDNEGO odczytu arkusza (zero dodatkowych zapytan):

    cena_odniesienia  strona ksiegi, ktora zlecenie UDERZY (bid przy sprzedazy,
                      ask przy kupnie) — mianownik poslizgu
    bid, ask          obie strony, zeby dalo sie rozdzielic poslizg na SPREAD
                      i na RUCH RYNKU. `poslizg.py` mowi wprost, ze tej
                      mozliwosci dotad nie bylo

a poslizg liczy sie z nich i z `exec.fill`:

    poslizg = znak * (fill.price - cena_odniesienia) / cena_odniesienia
    znak = +1 przy kupnie, -1 przy sprzedazy (dodatni = na nasza niekorzysc)

`poslizg.cena_odniesienia` czyta te kolumne jako zrodlo `arkusz_likwidacji`
i stawia je PRZED `exec.order.limit_price` — dla wiersza rynkowego tamta
kolumna jest NULL-em, a `journal.decision.target_price` opisuje chwile sprzed
godzin, nie chwile wysylki.

CZEGO TA CENA ODNIESIENIA NIE OBEJMUJE: GLEBOKOSCI. Czytamy arkusz o glebokosci
1, wiec zlecenie wieksze niz najlepsza oferta zje kolejne poziomy i poslizg
bedzie o to wiekszy — i tak MA byc zmierzony, bo tyle naprawde placimy.
Rozdzielenie „spread" od „zjedzona glebokosc" wymagaloby arkusza glebszego
i jest osobna decyzja.

--------------------------------------------------------------------------
POZYCJA NIEDOMYKALNA
--------------------------------------------------------------------------

`zlecenia.sprawdz_zlecenie` jest JEDYNYM sedzia ksztaltu zlecenia i ta funkcja
pyta go o CALA pozycje przed wyslaniem czegokolwiek. Cztery jego odmowy sa
zarazem czterema sposobami, w jakie zlecenie rynkowe odbiloby sie od gieldy:
niekompletne reguly, ilosc znikajaca po zaokragleniu do kroku, ilosc ponizej
minimum, wartosc ponizej `min_notional`. W kazdym z nich NIE WYSYLAMY NIC
i zapisujemy `stan='pominieta'` z powodem doslownie od `sprawdz_zlecenie`.

DLACZEGO LIKWIDACJA NIE SPISUJE PYLU NA STRATE, choc `gielda_exec.zloz_plan`
to robi. Bo to jest ta sama regula i ma zostac w jednym miejscu: domykanie
pozycji trwale niesprzedawalnej nalezy do sciezki wyjscia
(`loop.trwale_niesprzedawalna`, wolane z `zapisz_wyjscie` i z `zloz_plan`),
a druga kopia rozjechalaby sie z pierwsza — dokladnie tak, jak rozjechaly sie
`czy_zamykalna` i `trwale_niesprzedawalna`, gdy ta regula byla napisana dwa
razy. Likwidacja ODNOTOWUJE, ze tej pozycji nie ruszyla, i mowi CZY przeszkoda
jest trwala (krok, minimum ilosci) czy cenowa (`min_notional` — resztka warta
dzis za malo jutro po wzroscie ceny sprzeda sie normalnie).

--------------------------------------------------------------------------
CO Z ODPOWIEDZIA, KTORA NIE DOTARLA
--------------------------------------------------------------------------

Zlecenie rynkowe wypelnia sie natychmiast, ale odpowiedz moze sie zgubic. Stan
`'nieznana'`/`'zlecona'` w dzienniku znaczy „pozycja MOZE byc juz zamknieta"
i jest to TRZECI wynik, nie odmiana zera. Dlatego kazdy przebieg ZACZYNA od
domkniecia poprzednich: `dokoncz_rozpoczete` pyta gielde o wiersze
nierozstrzygniete i ksieguje ROZNICE miedzy tym, co gielda wypelnila, a tym, co
juz zaksiegowalismy. Ta sama arytmetyka co w `gielda_exec.posprzataj_zlecenia`
i z tego samego powodu: pytanie brzmi „czy jest wypelnienie, ktorego nie ma
w dzienniku", a nie „jaki byl status wiersza".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from services.engine.gielda_exec import arkusz_wysylki, filtry_z_bazy
from services.engine.loop import zapisz_wyjscie
from services.engine.zlecenia import Filtry, sprawdz_zlecenie
from services.exchanges.base import ExchangeError, OdmowaGieldy

# Stany dziennika, w ktorych sprawa NIE JEST ZAMKNIETA i trzeba dopytac gielde.
STANY_OTWARTE = ("zlecona", "nieznana")


def id_likwidacji(id_zdarzenia: int, position_id: int) -> str:
    """Nasz identyfikator zlecenia likwidacyjnego. Z PARY, nie z czasu.

    Zdarzenie kill-switcha i pozycja - czyli dokladnie te dwie rzeczy, ktore
    rozstrzygaja, czy to juz robilismy. Identyfikator z czasu albo losowy
    znosilby cala ochrone po stronie gieldy: kazda proba tworzylaby nowe
    zlecenie, a dwa zlecenia sprzedazy na jedna pozycje to pozycja krotka.
    """
    return f"tai-likw-{id_zdarzenia}-{position_id}"


# Zdarzenie kill-switcha RAZEM ZE ZNACZNIKIEM PROBY TEGO SAMEGO LIMITU.
#
# `LEFT JOIN`, a nie `JOIN`: zdarzenie bez `limit_name` (albo na limicie, ktory
# w miedzyczasie wylaczono) ma zostac widziane jako PRAWDZIWE. Wyciecie go
# JOIN-em znaczyloby "nie ma wiersza limitu, wiec nie likwidujemy" - czyli
# bezpiecznik wylaczany brakiem danej.
#
# SUROWE KOLUMNY, BEZ `coalesce(...) AS z_proby` W SQL-U. Rozstrzyga o tym
# `ocen_zdarzenie`, czyli CZYSTA FUNKCJA, ktora da sie sprawdzic na WARTOSCIACH
# zamiast na tresci zapytania. Test czytajacy SQL po napisie sprawdza, ze napis
# tam jest, a nie ze warunek dziala - w tym repozytorium taki test juz raz
# przeszedl, bo wzorzec dopasowal sie do literalu wylaczajacego cala oslone.
SQL_ZDARZENIE = (
    "SELECT k.id, k.reason, k.limit_name, k.triggered_at,"
    "       d.updated_at AS limit_updated_at, d.proba_do"
    "  FROM risk.killswitch_event k"
    "  LEFT JOIN risk.limit_def d ON d.engine = k.engine AND d.mode = k.mode"
    "                            AND d.name = k.limit_name AND d.enabled"
    " WHERE k.engine::text = %s AND k.mode::text = %s"
    "   AND k.released_at IS NULL"
    " ORDER BY k.triggered_at DESC LIMIT 1")


def ocen_zdarzenie(z: dict | None) -> tuple[int | None, str]:
    """Czy TO zatrzymanie uzasadnia likwidacje. CZYSTA FUNKCJA.

    Zwraca `(id zdarzenia, powod)`. `id is None` znaczy: NIE likwidujemy,
    a `powod` mowi, dlaczego — i te dwa powody sa zupelnie rozne:

      * nie ma czynnego zatrzymania — stan normalny, cisza jest na miejscu;
      * zatrzymanie jest ARTEFAKTEM PROBY POZAROWEJ — stan, o ktorym trzeba
        powiedziec glosno, bo przy nim likwidacja NIE DZIALA takze dla
        prawdziwego kill-switcha.

    Uzasadnienie warunku (i dlaczego jest ostrzejszy niz w `dozor.py`) stoi
    w naglowku modulu, w sekcji o probie pozarowej.
    """
    if not z:
        return None, "brak czynnego kill-switcha"
    if z.get("proba_do") is not None and z.get("limit_updated_at") is not None \
            and z["triggered_at"] >= z["limit_updated_at"]:
        return None, (
            f"zdarzenie {z['id']} na limicie {z['limit_name']} powstalo po "
            f"podniesieniu progu przez PROBE POZAROWA (proba_do "
            f"{z['proba_do']}) — to artefakt proby, nie strata. LIKWIDACJI NIE "
            f"URUCHAMIAM. Jesli `proba_do` jest w przeszlosci, proba zginela "
            f"przed przywroceniem progu: wtedy likwidacja jest WYLACZONA takze "
            f"dla prawdziwego zatrzymania, dopoki ktos nie wyczysci znacznika")
    return z["id"], f"kill-switch {z['id']}: {z['reason']}"


def stan_z_gieldy(stan, ilosc_zlecona: Decimal) -> str:
    """Stan dziennika likwidacji z odpowiedzi gieldy. CZYSTA FUNKCJA.

    O WYNIKU ROZSTRZYGA ILOSC, NIE STATUS — i to jest istota. Zlecenie rynkowe
    jest IOC: wypelnia, ile moze, i reszte anuluje, wiec gielda oddaje
    `cancelled` zarowno dla zlecenia, z ktorego nie weszlo NIC, jak i dla tego,
    z ktorego weszlo 90%. Rozstrzyganie po statusie wpisaloby drugi przypadek
    jako „odrzucona", czyli zaraportowaloby pozycje zamknieta prawie w calosci
    jako nietknieta — i odwrotnie przy `filled` z zerem.
    """
    if stan is None:
        return "nieznana"
    if stan.wypelniono <= 0:
        return "odrzucona" if stan.status in ("cancelled", "rejected") else "zlecona"
    if stan.wypelniono >= ilosc_zlecona:
        return "wypelniona"
    return "czesciowa"


async def _odniesienie(adapter, poz: dict, kupno: bool, kategoria: str,
                       teraz: datetime) -> tuple[dict, dict]:
    """(dane odniesienia do dziennika, slad arkusza do `exec.order.raw`).

    TRZY ZRODLA, ALE TYLKO JEDNO JEST POMIAREM POSLIZGU.

    `cena_odniesienia` w dzienniku dostaje WYLACZNIE cene z arkusza ksiegi
    egzekucji, bo tylko ona jest cena Z CHWILI PRZED WYSLANIEM. `lastPrice`
    (drugie zrodlo) opisuje ostatnia TRANSAKCJE i lezalo juz 11 tikow nad askiem
    (zlecenie 54), a `avg_price` pozycji to cena WEJSCIA, czyli liczba sprzed
    godzin albo dni. Obie nadaja sie do sprawdzenia KSZTALTU zlecenia (czy
    wartosc przekracza `min_notional`) i nie nadaja sie do niczego wiecej.

    Podstawienie ktorejkolwiek z nich pod `cena_odniesienia` daloby liczbe
    wygladajaca na pomiar poslizgu i bedaca pomiarem czegos innego — dokladnie
    ten blad, ktory `poslizg.py` opisuje przy `rozjazd_ksiag`: wyjscie
    wypelnione co do grosza raportowalo +29 pb, czyli 1,45 calej rundy
    kosztowej, bo mierzono je wzgledem ceny, ktorej gielda nigdy nie dostala.
    """
    oferty, skad, slad = await arkusz_wysylki(adapter, poz["symbol"], kategoria,
                                              teraz)
    if oferty is not None:
        cena = oferty.agresywna(kupno=kupno)
        return dict(cena_odniesienia=cena, zrodlo_odniesienia="arkusz",
                    bid=oferty.bid, ask=oferty.ask,
                    cena_ksztaltu=cena, skad=skad), slad
    try:
        ostatnia = await adapter.cena_odniesienia(poz["symbol"], kategoria)
    except ExchangeError as e:
        ostatnia = None
        skad = f"{skad}; ostatnia cena tez nie przyszla [{e}]"
    if ostatnia is not None and Decimal(str(ostatnia)) > 0:
        return dict(
            cena_odniesienia=None, zrodlo_odniesienia="lastPrice",
            bid=None, ask=None, cena_ksztaltu=Decimal(str(ostatnia)),
            skad=f"{skad}; ksztalt sprawdzony na ostatniej cenie {ostatnia} "
                 f"— POSLIZGU Z TEGO ZLECENIA NIE DA SIE ZMIERZYC, bo nie ma "
                 f"ceny z chwili wysylki"), slad
    return dict(
        cena_odniesienia=None, zrodlo_odniesienia="avg_price",
        bid=None, ask=None, cena_ksztaltu=Decimal(str(poz["avg_price"])),
        skad=f"{skad}; ksztalt sprawdzony na CENIE WEJSCIA "
             f"{poz['avg_price']} — POSLIZGU Z TEGO ZLECENIA NIE DA SIE "
             f"ZMIERZYC"), slad


def _pozycja(cur, position_id: int) -> dict | None:
    cur.execute(
        "SELECT p.id, p.side, p.qty, p.avg_price, p.closed_at, p.instrument_id,"
        "       i.symbol, i.quote_ccy, i.base_ccy"
        "  FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.id = %s", (position_id,))
    return cur.fetchone()


def _zaksieguj(cur, poz: dict, wiersz: dict, stan, teraz: datetime) -> Decimal:
    """Ksieguje ROZNICE miedzy wypelnieniem gieldy a dziennikiem. Zwraca ilosc.

    ROZNICE, NIE CALOSC — ta sama arytmetyka co w `posprzataj_zlecenia`. Wiersz
    dziennika trzyma, ile juz zaksiegowalismy z tego zlecenia, wiec powtorne
    wywolanie (odpowiedz spozniona, drugi cykl) dokłada tylko nowe wypelnienie
    i NIE sprzedaje pozycji drugi raz.

    `decision_id=None`: LIKWIDACJA NIE MA DECYZJI ZA SOBA i nie wolno podstawic
    tu cudzej. `exec.position.close_decision_id` odpowiada na pytanie "ktora
    decyzja zamknela te pozycje"; wpisanie tam decyzji wejsciowej albo ostatniej
    z dziennika byloby falszem w jedynym miejscu, ktore laczy pozycje z jej
    uzasadnieniem. Kolumna jest NULLABLE (0005), a `exec.likwidacja` mowi, co ja
    zamknelo.

    `filtry=None` ZOSTAWIA DOMYKANIE PYLU SCIEZCE WYJSCIA. `zapisz_wyjscie`
    z regulami gieldy domyka pozycje, z ktorej zostala resztka nie do
    sprzedania, i SPISUJE JA NA STRATE. Tutaj tego nie chcemy: likwidacja ma
    zapisac, co sprzedala, a nie spisywac na strate reszty przy pierwszej
    proznej odpowiedzi gieldy. Resztka zostaje otwarta, widzi ja risk kernel,
    a `exec.likwidacja.stan='czesciowa'` mowi, ze jest.
    """
    juz = Decimal(str(wiersz.get("ilosc_wypelniona") or 0))
    nowe = Decimal(str(stan.wypelniono)) - juz
    if nowe <= 0:
        return Decimal(0)
    prowizja_juz = Decimal(str(wiersz.get("prowizja") or 0))
    prowizja = Decimal(str(stan.prowizja or 0)) - prowizja_juz
    if prowizja < 0:
        prowizja = Decimal(0)
    zapisz_wyjscie(
        cur, poz, Decimal(str(stan.srednia_cena)), prowizja, teraz, None,
        ilosc=nowe, order_id=wiersz["order_id"],
        venue_fill_id=f"likw-{wiersz['client_order_id']}-{stan.wypelniono}",
        prowizja_ccy=stan.prowizja_w, filtry=None)
    return nowe


def _domknij_wiersz(cur, wiersz: dict, stan, nowy: str, powod: str,
                    teraz: datetime) -> None:
    cur.execute(
        "UPDATE exec.likwidacja SET stan=%s, powod=%s,"
        " ilosc_wypelniona=%s, cena_wypelnienia=%s, prowizja=%s,"
        " prowizja_ccy=%s, rozstrzygnieta_at=%s WHERE id=%s",
        (nowy, powod,
         None if stan is None else stan.wypelniono,
         None if stan is None else stan.srednia_cena,
         None if stan is None else stan.prowizja,
         None if stan is None else stan.prowizja_w,
         None if nowy in STANY_OTWARTE else teraz, wiersz["id"]))


def _zapisz_zlecenie(cur, poz: dict, engine: str, mode: str, strona: str,
                     ilosc: Decimal, id_wlasne: str, id_gieldy: str | None,
                     status: str, powod: str | None, raw: dict,
                     teraz: datetime) -> int:
    """Wiersz `exec.order` dla zlecenia RYNKOWEGO. Zwraca jego id.

    `limit_price` ZOSTAJE NULL-em i to jest cala roznica wobec sciezki planowej.
    Zlecenie rynkowe nie ma ceny limitu; wpisanie tam czegokolwiek — ceny
    odniesienia, ceny wejscia, `lastPrice` — trafiloby prosto do pomiaru
    poslizgu jako zrodlo "zlecenie" (`poslizg.cena_odniesienia`, punkt 1)
    i nazwaloby poslizgiem odleglosc od liczby, ktorej gielda nigdy nie
    dostala. Cena odniesienia mieszka w `exec.likwidacja`.

    `decision_id` i `planned_order_id` tez sa NULL-ami: za tym zleceniem nie
    stoi decyzja modelu ani plan, tylko zadzialanie bezpiecznika. Podstawienie
    tu czegokolwiek robiloby z likwidacji wykonanie decyzji, ktorej nikt nie
    podjal — a `poslizg.cena_odniesienia` wzieloby wtedy `target_price` tamtej
    decyzji jako cene odniesienia dla zlecenia rynkowego zlozonego wiele godzin
    pozniej.
    """
    cur.execute(
        "INSERT INTO exec.order (engine, mode, instrument_id, venue_order_id,"
        " client_order_id, side, order_type, qty, status, reject_reason,"
        " submitted_at, raw)"
        " VALUES (%s,%s,%s,%s,%s,%s,'market',%s,%s,%s,%s,%s) RETURNING id",
        (engine, mode, poz["instrument_id"], id_gieldy, id_wlasne, strona,
         ilosc, status, powod, teraz, json.dumps(raw)))
    return cur.fetchone()["id"]


async def dokoncz_rozpoczete(cur, adapter, *, engine: str, mode: str,
                             kategoria: str, teraz: datetime,
                             verbose: bool = True) -> dict:
    """Dopytuje gielde o likwidacje, ktore nie doszly do rozstrzygniecia.

    DLACZEGO TO MUSI ISTNIEC, A NIE WYSTARCZY `posprzataj_zlecenia`. Tamta
    funkcja domyka WIERSZ ZLECENIA i zapisuje, ze jest wypelnienie poza
    dziennikiem — ale go NIE KSIEGUJE (ksiegowanie nalezy do rozstrzygania
    planow, a likwidacja planu nie ma). Bez tej funkcji zlecenie rynkowe,
    ktorego odpowiedz sie zgubila, zostawialoby aktywo SPRZEDANE na gieldzie
    i pozycje OTWARTA w naszych ksiegach: risk kernel liczylby ekspozycje,
    ktorej nie ma, a krzywa kapitalu wycenialaby aktywo, ktorego nie trzymamy.
    """
    st = dict(domkniete=0, zaksiegowane=0, nadal_nieznane=0)
    cur.execute(
        "SELECT id, position_id, client_order_id, order_id, ilosc_zlecona,"
        "       ilosc_wypelniona, prowizja, stan"
        "  FROM exec.likwidacja"
        " WHERE engine::text=%s AND mode::text=%s AND stan = ANY(%s)"
        " ORDER BY id", (engine, mode, list(STANY_OTWARTE)))
    for wiersz in cur.fetchall():
        try:
            stan = await adapter.stan_zlecenia(wiersz["client_order_id"],
                                               kategoria=kategoria)
        except ExchangeError as e:
            st["nadal_nieznane"] += 1
            if verbose:
                print(f"  [likwidacja] {wiersz['client_order_id']}: stanu nadal "
                      f"nie da sie odczytac [{e}] — pozycja MOZE byc zamknieta")
            continue
        if stan is None:
            # GIELDA GO NIE ZNA. Zlecenie nie powstalo - a to znaczy, ze pozycja
            # NIE zostala sprzedana. Zamykamy wiersz dziennika, pozycji nie
            # ruszamy i mowimy WPROST, ze ekspozycja zostaje.
            _domknij_wiersz(
                cur, wiersz, None, "odrzucona",
                f"gielda nie zna zlecenia {wiersz['client_order_id']} "
                f"(dziennik mowil {wiersz['stan']}) — nic nie weszlo, POZYCJA "
                f"ZOSTAJE OTWARTA", teraz)
            st["domkniete"] += 1
            continue
        nowy = stan_z_gieldy(stan, Decimal(str(wiersz["ilosc_zlecona"] or 0)))
        poz = _pozycja(cur, wiersz["position_id"])
        uwaga = ""
        if stan.wypelniono > 0 and stan.srednia_cena:
            if poz is None or poz["closed_at"] is not None:
                uwaga = (f"; WYPELNIENIE {stan.wypelniono} NIE ZAKSIEGOWANE — "
                         f"pozycja {wiersz['position_id']} jest juz zamknieta "
                         f"inna droga, wiec ksiegowanie dopisaloby DRUGIE "
                         f"wyjscie. Wylapie to uzgodnienie z gielda")
            else:
                st["zaksiegowane"] += int(
                    _zaksieguj(cur, poz, wiersz, stan, teraz) > 0)
        _domknij_wiersz(cur, wiersz, stan, nowy,
                        f"domkniete po fakcie: gielda {stan.wypelniono}/"
                        f"{stan.ilosc}, status {stan.status}{uwaga}", teraz)
        if nowy not in STANY_OTWARTE:
            st["domkniete"] += 1
        if verbose:
            print(f"  [likwidacja] {wiersz['client_order_id']}: {nowy} — "
                  f"gielda {stan.wypelniono}/{stan.ilosc}{uwaga}")
    return st


async def likwiduj_po_killswitchu(cur, adapter, *, engine: str, mode: str,
                                  kategoria: str, teraz: datetime | None = None,
                                  verbose: bool = True) -> dict:
    """Zamyka WSZYSTKIE otwarte pozycje zleceniem rynkowym. Zwraca statystyke.

    Uzasadnienie kazdego wyboru — proba pozarowa, idempotencja, pomiar poslizgu,
    pozycja niedomykalna — stoi w naglowku modulu.

    ZWRACANY SLOWNIK IDZIE DO PULSU SILNIKA, wiec ma pelny ksztalt ZAWSZE, takze
    gdy nic sie nie stalo. Klucz pojawiajacy sie dopiero przy zdarzeniu nie da
    sie odroznic od klucza, ktorego ta wersja nie liczy - ta sama zasada co
    w `loop.nowe_stats`.
    """
    teraz = teraz or datetime.now(timezone.utc)
    st = dict(czynna=False, powod="", rozwazone=0, zlecone=0, wypelnione=0,
              czesciowe=0, pominiete=0, odrzucone=0, nieznane=0,
              juz_zrobione=0, wstrzymana_artefaktem_proby=False,
              domkniete_wczesniejsze=0, nadal_nieznane=0)

    cur.execute(SQL_ZDARZENIE, (engine, mode))
    zdarzenie = cur.fetchone()
    id_zdarzenia, powod = ocen_zdarzenie(zdarzenie)
    st["powod"] = powod
    if id_zdarzenia is None:
        # ARTEFAKT PROBY JEST STANEM DO ZGLOSZENIA, a brak zatrzymania nie jest.
        # Rozdzielenie ich TUTAJ to jedyne miejsce, w ktorym da sie to zrobic:
        # z zewnatrz oba wygladaja identycznie, jako zero zlikwidowanych pozycji.
        if zdarzenie:
            st["wstrzymana_artefaktem_proby"] = True
            if verbose:
                print(f"  LIKWIDACJA WSTRZYMANA: {powod}")
        return st
    st["czynna"] = True
    if verbose:
        print(f"  LIKWIDACJA POZYCJI ZLECENIEM RYNKOWYM — {powod}")

    # DOMKNIECIE POPRZEDNICH IDZIE PIERWSZE. Wiersz `zlecona`/`nieznana` znaczy
    # "pozycja MOZE byc juz zamknieta"; policzenie jej nizej jako otwartej nie
    # grozi drugim zleceniem (klucz dziennika), ale zostawialoby
    # niezaksiegowane wypelnienie na kolejna dobe.
    wczesniejsze = await dokoncz_rozpoczete(
        cur, adapter, engine=engine, mode=mode, kategoria=kategoria,
        teraz=teraz, verbose=verbose)
    st["domkniete_wczesniejsze"] = wczesniejsze["domkniete"]
    st["nadal_nieznane"] = wczesniejsze["nadal_nieznane"]

    filtry = filtry_z_bazy(cur, engine)
    cur.execute(
        "SELECT p.id, p.side, p.qty, p.avg_price, p.instrument_id,"
        "       i.symbol, i.quote_ccy, i.base_ccy"
        "  FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.engine::text=%s AND p.mode::text=%s AND p.closed_at IS NULL"
        " ORDER BY p.id", (engine, mode))
    for poz in cur.fetchall():
        st["rozwazone"] += 1
        # STRONA ZLECENIA JEST ODWROTNA DO STRONY POZYCJI. Wpisanie tu strony
        # pozycji zrobiloby z likwidacji DRUGIE wejscie - i to na calej
        # ekspozycji naraz, w chwili, w ktorej system wlasnie stwierdzil, ze
        # ryzyko jest za duze.
        kupno = poz["side"] != "long"
        strona = "buy" if kupno else "sell"
        f = filtry.get(poz["instrument_id"], Filtry())
        odn, slad = await _odniesienie(adapter, poz, kupno, kategoria, teraz)
        ilosc, _, powod_ksztaltu = sprawdz_zlecenie(
            Decimal(str(poz["qty"])), odn["cena_ksztaltu"], f,
            kupno=kupno, wyjscie=True)
        id_wlasne = id_likwidacji(id_zdarzenia, poz["id"])

        # ZAJECIE PARY (ZDARZENIE, POZYCJA) IDZIE PRZED CZYMKOLWIEK INNYM.
        # Wiersz powstaje TAKZE dla pozycji, ktorej NIE RUSZAMY - inaczej
        # "pominieta" nie zostawialaby zadnego sladu, a cisza znaczylaby
        # jednoczesnie "nie bylo czego zamykac" i "likwidacja tego nie tknela".
        cur.execute(
            "INSERT INTO exec.likwidacja (killswitch_event_id, position_id,"
            " engine, mode, instrument_id, client_order_id, strona,"
            " ilosc_pozycji, ilosc_zlecona, cena_odniesienia,"
            " zrodlo_odniesienia, bid, ask, stan, powod, zlecona_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (killswitch_event_id, position_id) DO NOTHING"
            " RETURNING id",
            (id_zdarzenia, poz["id"], engine, mode, poz["instrument_id"],
             id_wlasne, strona, Decimal(str(poz["qty"])),
             None if powod_ksztaltu else ilosc,
             odn["cena_odniesienia"], odn["zrodlo_odniesienia"],
             odn["bid"], odn["ask"],
             "pominieta" if powod_ksztaltu else "zlecona",
             powod_ksztaltu or odn["skad"], teraz))
        zajete = cur.fetchone()
        if not zajete:
            # JUZ BYLO. Zadnego zlecenia, zadnego zapisu - i to jest cala
            # idempotencja: rozstrzyga OGRANICZENIE BAZY, nie odczyt i porownanie.
            st["juz_zrobione"] += 1
            continue
        id_wiersza = zajete["id"]

        if powod_ksztaltu:
            st["pominiete"] += 1
            if verbose:
                print(f"  [likwidacja] {poz['symbol']} POMINIETA — "
                      f"{powod_ksztaltu}")
            continue

        arkusz = dict(slad)
        arkusz["cena_odniesienia"] = (
            None if odn["cena_odniesienia"] is None
            else str(odn["cena_odniesienia"]))
        arkusz["zrodlo_odniesienia"] = odn["zrodlo_odniesienia"]
        raw = dict(execution="gielda", etap="likwidacja_po_killswitchu",
                   killswitch_event_id=id_zdarzenia, likwidacja_id=id_wiersza,
                   arkusz_likwidacji=arkusz)
        try:
            z = await adapter.zloz_zlecenie(
                symbol=poz["symbol"], strona=strona, ilosc=ilosc,
                cena_limitu=None, id_wlasne=id_wlasne, kategoria=kategoria,
                rynkowe=True)
        except OdmowaGieldy as e:
            # GIELDA ODPOWIEDZIALA "NIE" - zlecenia NIE MA i nie bedzie.
            oid = _zapisz_zlecenie(cur, poz, engine, mode, strona, ilosc,
                                   id_wlasne, None, "rejected", str(e),
                                   dict(raw, etap="odmowa_gieldy"), teraz)
            cur.execute(
                "UPDATE exec.likwidacja SET stan='odrzucona', powod=%s,"
                " order_id=%s, rozstrzygnieta_at=%s WHERE id=%s",
                (f"gielda odmowila przyjecia zlecenia rynkowego [{e}] — "
                 f"POZYCJA ZOSTAJE OTWARTA", oid, teraz, id_wiersza))
            st["odrzucone"] += 1
            if verbose:
                print(f"  [likwidacja] {poz['symbol']} ODMOWA GIELDY — {e}")
            continue
        except ExchangeError as e:
            # ODPOWIEDZ NIE DOTARLA. Zlecenie MOGLO wejsc, wiec nie zapisujemy
            # ani sukcesu, ani porazki - `dokoncz_rozpoczete` zapyta o to
            # w nastepnym cyklu po NASZYM identyfikatorze.
            oid = _zapisz_zlecenie(cur, poz, engine, mode, strona, ilosc,
                                   id_wlasne, None, "unknown", str(e),
                                   dict(raw, etap="odpowiedz_nie_dotarla"),
                                   teraz)
            cur.execute(
                "UPDATE exec.likwidacja SET stan='nieznana', powod=%s,"
                " order_id=%s WHERE id=%s",
                (f"odpowiedz na zlecenie rynkowe nie dotarla [{e}] — pozycja "
                 f"MOZE byc zamknieta; pytamy gielde po naszym id",
                 oid, id_wiersza))
            st["nieznane"] += 1
            if verbose:
                print(f"  [likwidacja] {poz['symbol']} ODPOWIEDZ NIE DOTARLA — {e}")
            continue

        oid = _zapisz_zlecenie(cur, poz, engine, mode, strona, ilosc, id_wlasne,
                               z.id_gieldy, z.status, None,
                               dict(raw, juz_istnialo=z.juz_istnialo), teraz)
        cur.execute("UPDATE exec.likwidacja SET order_id=%s WHERE id=%s",
                    (oid, id_wiersza))
        st["zlecone"] += 1

        # STAN PYTAMY OD RAZU. Zlecenie rynkowe wypelnia sie natychmiast, wiec
        # w zdecydowanej wiekszosci przypadkow odpowiedz jest tu kompletna -
        # a wtedy wypelnienie ksieguje sie w TYM cyklu, nie w nastepnym.
        try:
            stan = await adapter.stan_zlecenia(id_wlasne, kategoria=kategoria)
        except ExchangeError as e:
            st["nieznane"] += 1
            cur.execute(
                "UPDATE exec.likwidacja SET stan='nieznana', powod=%s WHERE id=%s",
                (f"zlecenie zlozone, ale stanu nie udalo sie odczytac [{e}] — "
                 f"pozycja MOZE byc zamknieta", id_wiersza))
            if verbose:
                print(f"  [likwidacja] {poz['symbol']} zlozone, stan nieznany — {e}")
            continue

        wiersz = dict(id=id_wiersza, position_id=poz["id"],
                      client_order_id=id_wlasne, order_id=oid,
                      ilosc_zlecona=ilosc, ilosc_wypelniona=None,
                      prowizja=None, stan="zlecona")
        nowy = stan_z_gieldy(stan, ilosc)
        uwaga = ""
        if stan is not None and stan.wypelniono > 0 and stan.srednia_cena:
            _zaksieguj(cur, poz, wiersz, stan, teraz)
            if (stan.prowizja_w or "").upper() == (poz["base_ccy"] or "").upper():
                # PROWIZJA W AKTYWIE BAZOWYM przy sprzedazy nie powinna sie
                # zdarzyc (spot pobiera ja wtedy w walucie kwotowanej), a gdyby
                # sie zdarzyla, `zapisz_wyjscie` zsumowaloby ja z prowizjami
                # w walucie kwotowanej - czyli dodaloby jednostki do dolarow.
                # Przeliczenia NIE ZGADUJEMY; zostawiamy slad.
                uwaga = (f"; UWAGA: prowizja {stan.prowizja} w {stan.prowizja_w} "
                         f"(aktywo bazowe) zsumowana z prowizjami w "
                         f"{poz['quote_ccy']} — `fees_paid` ma wtedy DWIE "
                         f"jednostki naraz")
        if stan is not None:
            cur.execute("UPDATE exec.order SET status=%s, updated_at=%s WHERE id=%s",
                        (stan.status, teraz, oid))
        opis_stanu = (
            "zlecenie zlozone, a gielda go nie zna — stan nieznany"
            if stan is None else
            f"zlecenie rynkowe: gielda {stan.wypelniono}/{stan.ilosc}, "
            f"status {stan.status}{uwaga}")
        _domknij_wiersz(cur, wiersz, stan, nowy, opis_stanu, teraz)
        if nowy == "wypelniona":
            st["wypelnione"] += 1
        elif nowy == "czesciowa":
            st["czesciowe"] += 1
        elif nowy == "odrzucona":
            st["odrzucone"] += 1
        elif nowy == "nieznana":
            st["nieznane"] += 1
        if verbose:
            opis = ("brak odpowiedzi" if stan is None
                    else f"{stan.wypelniono}/{stan.ilosc} @ {stan.srednia_cena}")
            print(f"  [likwidacja] {poz['symbol']} {strona} {ilosc} — {nowy}, "
                  f"{opis}, odniesienie {odn['cena_odniesienia']} "
                  f"({odn['zrodlo_odniesienia']})")
    return st
