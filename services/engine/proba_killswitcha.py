"""Próba pożarowa kill-switcha — jako usługa, nie skrypt.

DLACZEGO TO NIE JEST PLIK .sh NA VPS-IE

Skrypt leżący na serwerze rozjeżdża się z kodem, który testuje: obraz idzie
przez CI i wdrożenie, plik na dysku nie idzie nigdzie. Po miesiącu nie wiadomo,
czy sprawdza wersję, która działa. Ten moduł jedzie w tym samym obrazie co
silnik, więc testuje dokładnie ten kod, który handluje.

Druga rzecz: próba uruchamiana ręcznie zostanie uruchomiona raz. Jako zadanie
cykliczne wykonuje się sama i odzywa się dopiero wtedy, gdy coś nie zadziała.

CO SPRAWDZA

Testy jednostkowe sprawdzają regułę: czy przekroczony limit żąda zatrzymania.
Nie sprawdzają, czy silnik ją woła, czy wiersz dociera do bazy, czy dozór to
zauważa i czy powiadomienie wychodzi. Kill-switch jest jedynym mechanizmem
w tym systemie, którego awaria nie objawia się niczym — silnik po jego
zadziałaniu wygląda tak samo jak silnik bez sygnału do wejścia.

    ogniwo 1  limit przekroczony -> żądanie zatrzymania
    ogniwo 2  żądanie -> wiersz w risk.killswitch_event
    ogniwo 3  aktywny -> silnik wstrzymał wejścia
    ogniwo 4  dozór -> powiadomienie

OGNIWO 3 BYŁO DO 2026-09-22 WYPISANE W TEJ LIŚCIE I NIESPRAWDZANE W KODZIE.
Między etykietami „OGNIWO 1-2" i „OGNIWO 4" nie stało ani jedno zapytanie o to,
czy silnik cokolwiek wstrzymał, a na końcu bezwarunkowo drukowało się zdanie
„wszystkie ogniwa zadziałały". Pominięte było jedyne, które chroni PIENIĄDZE —
pozostałe trzy opisują zapis i powiadomienie. Próba meldowała więc sukces,
którego nie zmierzyła, i to przez jedyny kanał, jaki ten bezpiecznik ma.

CO ZOSTAJE ZMIENIONE

Jedna wartość: próg kapitału podniesiony powyżej bieżącego stanu, żeby silnik
SAM wykrył przekroczenie. Przywracana w `finally`, więc wraca także po błędzie
i po przerwaniu — zostawiony podniesiony próg zatrzymałby silnik na stałe,
czyli spowodowałby dokładnie tę awarię, przed którą kill-switch ma chronić.

Próba używa progu kapitału, bo to jedyny z trzech limitów krytycznych z regułą
`reczne` — usługa wznowienia go nie ruszy i nie zwolni zdarzenia w połowie
pomiaru.

TRZY WYNIKI, NIE DWA

    ZALICZONA (0)          łańcuch zadziałał od limitu po powiadomienie
    NIEZALICZONA (1)       próba się WYKONAŁA i któreś ogniwo padło —
                           to jest awaria zabezpieczenia i ma krzyczeć
    NIEROZSTRZYGNIĘTA (0)  któregoś ogniwa NIE DA SIĘ zmierzyć. Dwie przyczyny:
                           kill-switch już stoi (nie ma czego wywołać ze stanu
                           spoczynku) albo silnik nie miał w oknie próby ani
                           jednego zamiaru wejścia, więc ogniwo 3 nie zostawiło
                           śladu. Wynik nazywa ogniwo, którego nie zmierzył
    NIESPRAWDZONE (2)      nierozstrzygnięta zbyt długo — patrz niżej

Do 2026-09-10 istniały dwa pierwsze i trzeci wpadał do drugiego, czyli aktywny
kill-switch był zgłaszany jako awaria samego kill-switcha. To błąd rozpoznania:
„nie dało się zmierzyć" to nie to samo co „zmierzono i wyszło źle". Skutek był
gorszy niż fałszywy alarm — zaufanie do próby spadało dokładnie wtedy, gdy
człowiek i tak wiedział o kill-switchu, a prawdziwa awaria łańcucha wyglądałaby
identycznie i utonęłaby w tych powtórkach.

NIEROZSTRZYGNIĘTA NIE JEST JEDNAK ZALICZONA. Kod zerowy znaczy tu tylko tyle, że
nie ma o czym budzić człowieka w nocy; komunikat mówi wprost, że w tym cyklu
NIKT nie sprawdził, czy bezpiecznik działa.

ILE Z OGNIWA 3 DA SIĘ ZMIERZYĆ W OKNIE PRÓBY — LICZBY, NIE NADZIEJE

Ślad wstrzymania wejść powstaje w `journal.decision` TYLKO wtedy, gdy silnik
w tym oknie w ogóle chciał wejść: `loop.py` podmienia wtedy akcję na `hold`
i zapisuje `powod_holdu='wstrzymanie_ryzykiem'`. Samo niewejście nie zostawia
śladu, bo niewejście jest tu stanem normalnym.

A decyzje zapadają RAZ NA ŚWIECĘ na instrument, przy `DEFAULT_BAR = '4h'`
(`horizons.py`), a nie co cykl. W oknie próby (kilkanaście minut) mieści się
więc około jednej dwunastej świecy, a zamiarów wejścia jest w tym systemie
rzędu dwóch na dobę (818 decyzji przez czternaście dni, z tego 36 nie-holdów —
migracja 0040). Ogniwo 3 jest zatem mierzalne WYŁĄCZNIE OKAZJONALNIE i żadne
czekanie mieszczące się w `activeDeadlineSeconds` tego nie zmieni.

Stąd trzy decyzje, na których stoi kod niżej:

  * NA OGNIWO 3 NIE CZEKAMY. Pytamy o nie dwa razy — zaraz po wykryciu
    zdarzenia i jeszcze raz po ogniwie 4 — więc obejmujemy całe okno próby,
    nie wydłużając jej ani o sekundę. Czekanie kupiłoby ułamek procenta szansy
    za cały pozostały budżet czasu.
  * „NIE ZMIERZONO" JEST TU WYNIKIEM CZĘSTYM i nie wolno go mylić z awarią.
    Próba świecąca na czerwono codziennie zostanie wyłączona w tydzień — i
    wtedy nie sprawdza się już NIC.
  * ALE NIE WOLNO GO TEŻ MYLIĆ Z SUKCESEM. Niezmierzone ogniwo 3 znaczy, że
    łańcuch NIE został potwierdzony w całości, więc taki przebieg kończy się
    jako NIEROZSTRZYGNIĘTY, a nie jako ZALICZONY, i mówi wprost, czego nie
    zmierzył.

CZEGO TU ŚWIADOMIE NIE MA: ESKALACJI PO N DOBACH BEZ OGNIWA 3

Cisza wokół ogniwa 3 jest stanem NORMALNYM (powody wyżej), więc licznik
„ile dób bez potwierdzenia ogniwa 3" zapalałby zadanie na czerwono w kółko,
przy działającym bezpieczniku. `MAX_DNI_BEZ_POTWIERDZENIA` stosuje się nadal
do próby ZABLOKOWANEJ przez zastany kill-switch — tam cisza ma inną przyczynę
(ktoś nie zwolnił zatrzymania) i sama w sobie jest do naprawienia.

Ceną jest jedna rzecz i trzeba ją nazwać: przebieg, który zmierzył ogniwa 1-2
i 4, a ogniwa 3 nie, kończy się kodem ZERO. Widać go wyłącznie w treści —
tak samo jak wynik NIEROZSTRZYGNIĘTY. Odróżnienie go kodem wyjścia wymagałoby
zapamiętania, kiedy łańcuch był potwierdzony OSTATNI RAZ W CAŁOŚCI, a tego nie
da się dziś odczytać: `_dni_od_potwierdzenia` rozpoznaje próbę po trójce kolumn,
którą zwolnienie z `finally` zapisuje tak samo jak zwolnienie po sukcesie.
Właściwym miejscem na to rozróżnienie jest KOLUMNA w `risk.killswitch_event`
(migracja plus nadanie), a nie sprytniejsze zapytanie ani dopasowanie notatki —
tego ostatniego zabrania wprost test `test_slad_poprzednich_prob_czytany_
z_TROJKI_KOLUMN_a_nie_z_notatki`.

Jedno zdarzenie jest w ogniwie 3 rozstrzygające w drugą stronę i nie wymaga
żadnej okazji: DECYZJA WEJŚCIOWA zapisana po zadziałaniu bezpiecznika. Silnik,
który mimo aktywnego kill-switcha zapisał `enter_long`/`enter_short`, nie
wstrzymał wejść — i to jest awaria najcięższa z możliwych, bo dotyczy pieniędzy.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.loop import OPIS_POWODU_HOLDU  # noqa: E402

LIMIT_PROBY = "min_equity"

# NAZWA GALEZI Z JEDNEGO ZRODLA, PRZEZ INDEKSOWANIE MAPY.
#
# `loop.OPIS_POWODU_HOLDU` jest zbiorem zamknietym, tym samym, ktorego pilnuje
# CHECK z migracji 0040. Wpisany tu na sztywno napis rozjechalby sie z nim po
# cichu: zapytanie nizej przestaloby znajdowac wiersze, a proba meldowalaby, ze
# silnik NIE wstrzymal wejsc - czyli falszywa awarie bezpiecznika, ktory dziala.
# Indeksowanie mapy zamienia ten rozjazd w KeyError przy imporcie, czyli w blad
# widoczny natychmiast i dla kazdego przebiegu.
POWOD_WSTRZYMANIA = "wstrzymanie_ryzykiem"
OPIS_WSTRZYMANIA = OPIS_POWODU_HOLDU[POWOD_WSTRZYMANIA]

# Stany pojedynczego ogniwa. Trzy, nie dwa - „nie zmierzylem" jest tu wynikiem
# czestym (patrz naglowek) i myleniem go z ktorymkolwiek z pozostalych dwoch
# stoi cala wada, ktora ten modul naprawia.
ZADZIALALO = "zadziałało"
NIEZMIERZONE = "NIE ZMIERZONO"
PADLO = "NIE ZADZIAŁAŁO"

# Ile czekać na cykl silnika (biegnie co 300 s) i na dozór (co 600 s).
CZEKAJ_SILNIK_S = int(os.environ.get("PROBA_CZEKAJ_SILNIK_S", "420"))
CZEKAJ_DOZOR_S = int(os.environ.get("PROBA_CZEKAJ_DOZOR_S", "660"))

# Okno, w ktorym zdarzenie kill-switcha na `LIMIT_PROBY` jest skutkiem proby
# (`risk.limit_def.proba_do`, migracja 0045). Dluzsze niz najdluzsza mozliwa
# proba (oba czekania) plus margines na cykl czujki (10 min) - bo czujka,
# ktora ruszyla tuz przed koncem proby, czyta znacznik chwile po nim.
OKNO_PROBY_S = CZEKAJ_SILNIK_S + CZEKAJ_DOZOR_S + 600
KROK_S = 15

# ILE DÓB BEZ POTWIERDZENIA, ZANIM SAMA CISZA JEST AWARIĄ.
#
# Bezpiecznik niesprawdzony przez wiele dni to osobne ryzyko od bezpiecznika
# zepsutego — i jedyne, które rośnie samo. Trzeci wynik bez tego progu byłby
# workiem bez dna: „nie dało się zmierzyć" powtarzane w nieskończoność
# z zerowym kodem wyjścia niczym się nie różni od wyłączenia próby.
#
# Siedem, bo próg musi mieścić się między dwiema granicami:
#
#   OD DOŁU — zwykłą ludzką obsługę prawdziwego zatrzymania. Kill-switch, który
#   zadziałał w piątek wieczorem i został zwolniony w poniedziałek po analizie,
#   daje trzy doby nierozstrzygniętych prób. Próg 3 albo 5 zamieniałby normalny
#   tryb pracy w drugi alarm — o alarmie. Wyłączony byłby w tydzień.
#
#   OD GÓRY — opóźnienie wykrycia, które ten projekt już raz uznał za zbyt duże.
#   Próba jest dobowa właśnie dlatego, że przy tygodniowej o awarii bezpiecznika
#   dowiadujemy się średnio po trzech i pół dnia (patrz komentarz w
#   charts/trader/templates/proba-cron.yaml). Tydzień ciszy jest więc dokładnie
#   tą granicą, powyżej której „chwilowo nie sprawdzamy" staje się stanem.
#
# Świadomie BEZ odczytu ze środowiska: próg, który da się podnieść zmienną,
# zostanie podniesiony przy pierwszym niewygodnym przebiegu i nikt tego nie
# zauważy — tak samo jak nie widać wyłączonego limitu.
MAX_DNI_BEZ_POTWIERDZENIA = 7


class Niezaliczona(RuntimeError):
    """Ogniwo łańcucha nie zadziałało. To awaria zabezpieczenia, nie próby."""


class Nierozstrzygnieta(RuntimeError):
    """Próby NIE DAŁO SIĘ przeprowadzić. To nie jest wynik pomiaru.

    Niesie `dni` — ile dób minęło od ostatniego POTWIERDZENIA, że łańcuch
    działa. Bez tej liczby trzeci wynik byłby nieodróżnialny od sukcesu.

    `dni is None` znaczy coś jeszcze innego i nie wolno tego zastąpić zerem:
    w dzienniku NIE MA ANI JEDNEGO pełnego potwierdzenia, więc nie ma od czego
    liczyć ciszy. Zero czytałoby się jako „potwierdzono przed chwilą", czyli
    dokładnie odwrotnie niż jest.
    """

    def __init__(self, powod: str, dni: float | None):
        super().__init__(powod)
        self.dni = dni


async def _czekaj_na(warunek, limit_s: int, opis: str):
    """Odpytuje warunek do skutku albo do limitu czasu. Zwraca wynik albo None."""
    minelo = 0
    while minelo < limit_s:
        wynik = await warunek()
        if wynik:
            return wynik
        await asyncio.sleep(KROK_S)
        minelo += KROK_S
    return None


async def _dni_od_potwierdzenia(fetch_one, engine: str, mode: str) -> float | None:
    """Ile dób od ostatniej ZALICZONEJ próby. None, gdy takiej nigdy nie było.

    ŚLAD JUŻ JEST W BAZIE — osobna tabela byłaby drugą kopią tej samej rzeczy.
    Każda zaliczona próba zwalnia zdarzenie, które sama wywołała, i zostawia
    parę kolumn, która nie powstaje inaczej:

        released_auto = true                       -> usługa wznowienia
        released_auto = false, released_by = ID    -> człowiek przez API
        released_auto = false, released_by = NULL  -> PRÓBA POŻAROWA

    Ta trójka jest już opisana niżej, przy samym zwolnieniu, i pilnuje jej test
    `test_proba_pozarowa_NIE_udaje_wznowienia_automatycznego`. Szukanie śladu po
    treści `release_note` działałoby do pierwszej zmiany sformułowania — ten sam
    błąd, przed którym ostrzega migracja 0029 przy `limit_name`.

    CZEGO TA TRÓJKA NIE ROZRÓŻNIA — i trzeba to wiedzieć, czytając tę liczbę.
    Zwolnienie z bloku `finally` (próba przerwana albo taka, która ogniwa 3 nie
    zmierzyła) pisze DOKŁADNIE te same trzy wartości co zwolnienie po próbie
    zaliczonej; różni je tylko treść notatki, a notatki ta funkcja świadomie nie
    czyta (patrz wyżej — i test `test_slad_poprzednich_prob_czytany_z_TROJKI_
    KOLUMN_a_nie_z_notatki`, który tego pilnuje). „Ostatnie potwierdzenie" znaczy
    więc dziś „ostatni przebieg, który doszedł do wywołania zdarzenia", a nie
    „ostatni przebieg, który potwierdził cały łańcuch". Rozróżnienie wymagałoby
    KOLUMNY w `risk.killswitch_event`, nie sprytniejszego zapytania.

    Liczymy od `released_at`, nie od `triggered_at`: potwierdzeniem jest moment,
    w którym łańcuch przeszedł do końca, a nie ten, w którym się zaczął.

    Doby liczy BAZA. Odejmowanie dat po stronie Pythona wymagałoby zgadywania
    strefy — a strefa pomylona o dwie godziny przy progu liczonym w dobach jest
    niewidoczna dokładnie do dnia, w którym ma zadziałać.
    """
    r = await fetch_one(
        "SELECT extract(epoch FROM now() - max(released_at)) / 86400.0 AS dni"
        " FROM risk.killswitch_event"
        " WHERE engine::text=%s AND mode::text=%s AND released_at IS NOT NULL"
        "   AND released_auto = false AND released_by IS NULL", (engine, mode))
    if r and r["dni"] is not None:
        return float(r["dni"])
    return None


async def _ogniwo_3(fetch_one, engine: str, mode: str, id_zdarzenia: int
                    ) -> tuple[int, int]:
    """Ile śladów wstrzymania i ile WEJŚĆ zapisał silnik PO zadziałaniu.

    Zwraca `(wstrzymane, wejscia)` — obie liczby z okna zaczynającego się
    w `triggered_at` TEGO zdarzenia i dla TEJ pary engine/mode.

    OBA LICZNIKI SĄ POTRZEBNE I MÓWIĄ O CZYM INNYM:

      * `wstrzymane` > 0 — silnik chciał wejść i został zatrzymany. Jedyny
        POZYTYWNY dowód, że ogniwo 3 działa. Bywa rzadko, bo wymaga zamiaru
        wejścia w oknie próby (patrz nagłówek modułu).
      * `wejscia` > 0 — silnik zapisał wejście MIMO aktywnego bezpiecznika.
        Dowód NEGATYWNY i rozstrzygający: nie wymaga żadnej okazji, bo sam
        jest tą okazją. Pod wstrzymaniem taki wiersz nie ma prawa powstać —
        `loop.py` podmienia wtedy akcję na `hold` PRZED zapisem decyzji.

    Zero i zero nie znaczy ani jednego, ani drugiego: znaczy, że w tym oknie
    silnik nie miał czego wstrzymywać. To jest brak pomiaru i tak ma być
    zgłoszone.

    Granicą okna jest `triggered_at` czytane W BAZIE, podzapytaniem — tak samo
    jak w ogniwie 4. Przeliczanie chwili w Pythonie wymagałoby zgadywania
    strefy, a strefa pomylona o dwie godziny przy oknie kilkunastominutowym
    daje okno całkowicie rozminięte z próbą.
    """
    r = await fetch_one(
        "SELECT count(*) FILTER (WHERE d.powod_holdu = %s) AS wstrzymane,"
        "       count(*) FILTER (WHERE d.action::text LIKE 'enter%%') AS wejscia"
        "  FROM journal.decision d"
        " WHERE d.engine::text = %s AND d.mode::text = %s"
        "   AND d.decided_at > (SELECT triggered_at FROM risk.killswitch_event"
        "                       WHERE id = %s)",
        (POWOD_WSTRZYMANIA, engine, mode, id_zdarzenia))
    if not r:
        return 0, 0
    return int(r["wstrzymane"] or 0), int(r["wejscia"] or 0)


async def przeprowadz(fetch_all, fetch_one, execute, engine: str = "crypto",
                      mode: str = "demo", sucho: bool = False) -> int:
    stan = await fetch_one(
        "SELECT round(equity::numeric, 2) AS equity FROM exec.equity_point"
        " WHERE engine=%s AND mode=%s ORDER BY event_time DESC LIMIT 1",
        (engine, mode))
    if not stan:
        raise Niezaliczona("brak zapisu kapitału — próba nie ma czym wyzwolić")

    lim = await fetch_one(
        "SELECT value FROM risk.limit_def WHERE engine::text=%s AND mode::text=%s"
        "   AND name=%s AND enabled", (engine, mode, LIMIT_PROBY))
    if not lim:
        raise Niezaliczona(f"limit {LIMIT_PROBY} nie istnieje")

    aktywne = await fetch_one(
        "SELECT count(*) AS n,"
        "       extract(epoch FROM now() - min(triggered_at)) / 86400.0"
        "           AS dni_blokady"
        " FROM risk.killswitch_event"
        " WHERE engine::text=%s AND mode::text=%s AND released_at IS NULL",
        (engine, mode))
    if aktywne["n"]:
        # NIE JEST TO NIEPOWODZENIE ŁAŃCUCHA, tylko brak możliwości pomiaru:
        # próba wywołuje kill-switch ze stanu spoczynku, a tego stanu nie ma.
        # (Nie odróżniłaby też swojego zdarzenia od zastanego i zwolniłaby
        # cudze — dlatego nie próbuje mimo wszystko.)
        dni = await _dni_od_potwierdzenia(fetch_one, engine, mode)
        if dni is None:
            # Ani jednej zaliczonej próby w dzienniku: świeże wdrożenie albo
            # taki system, w którym próba nigdy nie doszła do końca. Dolną
            # granicą ciszy jest wtedy wiek najstarszego czynnego zdarzenia —
            # przez ten czas próba na pewno nie mogła się wykonać. Zero
            # zerowałoby licznik na zawsze i próg byłby martwy.
            dni = float(aktywne["dni_blokady"] or 0.0)
        raise Nierozstrzygnieta(
            f"kill-switch jest już aktywny ({aktywne['n']}) — zwolnij go najpierw",
            dni)

    kapital = float(stan["equity"])
    prog_przed = float(lim["value"])
    prog_proby = round(kapital + 500)
    print(f"=== PRÓBA POŻAROWA KILL-SWITCHA: {engine}/{mode} ===")
    print(f"kapitał {kapital:.2f}   próg przed próbą {prog_przed:.2f}   aktywnych 0")

    if sucho:
        print(f"Tryb suchy: podniósłbym {LIMIT_PROBY} na {prog_proby} i czekał "
              f"na cykl silnika. Nic nie zmieniam.")
        return 0

    # IDENTYFIKATOR ZDARZENIA, KTORE PROBA SAMA WYWOLA. Poza blokiem `try`,
    # bo czyta go `finally` - a tam nie wolno polegac na tym, ze przypisanie
    # w srodku zdazylo sie wykonac.
    moje_zdarzenie: int | None = None

    # STAN KAZDEGO OGNIWA Z OSOBNA, a nie jedna flaga „poszlo dobrze".
    # Komunikat koncowy powstaje WYLACZNIE z tego slownika, wiec nie da sie
    # napisac w nim niczego, czego ten przebieg nie zmierzyl - a to wlasnie
    # rozjazd miedzy zdaniem koncowym a pomiarem byl tu cala wada.
    ogniwa: dict[str, str] = {
        "1-2 limit -> zdarzenie": NIEZMIERZONE,
        "3 aktywny -> silnik wstrzymał wejścia": NIEZMIERZONE,
        "4 dozór -> powiadomienie": NIEZMIERZONE,
    }

    def wypisz_ogniwa() -> None:
        print("\n--- ogniwa łańcucha ---")
        for nazwa, stan in ogniwa.items():
            print(f"  ogniwo {nazwa}: {stan}")

    try:
        print(f"\n--- podnoszę {LIMIT_PROBY} na {prog_proby} (powyżej kapitału) ---")
        # ZAPOWIEDZ OKNA PROBY W TYM SAMYM ZAPISIE, CO PODNIESIENIE PROGU.
        #
        # Czujka zywotnosci (`dozor.sprawdz_ryzyko`) widzi zdarzenie proby
        # jako zwykly kill-switch - i do 2026-09-22 co rano wysylala o nim
        # drugie powiadomienie i konczyla sie kodem 1. Znacznik `proba_do`
        # (migracja 0045) mowi jej, ze zdarzenie na tym limicie przed tym
        # terminem jest SKUTKIEM PROBY. Idzie w jednym UPDATE z wartoscia,
        # zeby nie bylo chwili, w ktorej prog jest juz podniesiony, a znacznika
        # jeszcze nie ma. Termin, a nie flaga - proba, ktora zginie przed
        # `finally`, nie moze zostawic znacznika na zawsze.
        await execute(
            "UPDATE risk.limit_def SET value = %s, updated_at = now(),"
            "       proba_do = now() + %s * interval '1 second'"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s",
            (prog_proby, OKNO_PROBY_S, engine, mode, LIMIT_PROBY))

        print(f"\n--- OGNIWO 1-2: czekam na cykl silnika (do {CZEKAJ_SILNIK_S}s) ---")

        async def zdarzenie():
            return await fetch_one(
                "SELECT id, reason, limit_name FROM risk.killswitch_event"
                " WHERE engine::text=%s AND mode::text=%s AND released_at IS NULL"
                " ORDER BY triggered_at DESC LIMIT 1", (engine, mode))

        z = await _czekaj_na(zdarzenie, CZEKAJ_SILNIK_S, "zdarzenie")
        if not z:
            wypisz_ogniwa()
            raise Niezaliczona(
                "silnik nie uruchomił kill-switcha mimo kapitału poniżej progu")
        moje_zdarzenie = z["id"]
        print(f"  zadziałał: id={z['id']} limit={z['limit_name']} — {z['reason']}")
        if z["limit_name"] != LIMIT_PROBY:
            raise Niezaliczona(
                f"zadziałał inny limit ({z['limit_name']}) — próba mierzy nie to, "
                f"co zakłada")
        ogniwa["1-2 limit -> zdarzenie"] = ZADZIALALO

        # OGNIWO 3, PYTANIE PIERWSZE. Bez czekania: slad zapisuje cykl, ktory
        # zdarzenie wywolal, wiec jesli w ogole powstal, to juz tu jest.
        print("\n--- OGNIWO 3, pytanie 1 z 2: czy silnik wstrzymał wejścia ---")
        wstrzymane, wejscia = await _ogniwo_3(fetch_one, engine, mode, z["id"])
        print(f"  wstrzymanych zamiarów wejścia: {wstrzymane}, "
              f"wejść mimo bezpiecznika: {wejscia} — pytam jeszcze raz po "
              f"ogniwie 4, żeby objąć całe okno próby")

        print(f"\n--- OGNIWO 4: czy dozór zgłosił (do {CZEKAJ_DOZOR_S}s) ---")

        async def powiadomienie():
            # FILTR PO SILNIKU I PO IDENTYFIKATORZE ZDARZENIA, nie po samym
            # kodzie zdarzenia. `killswitch.triggered` wysylaja takze
            # `uzgodnienie.py` i `wznowienie.py`, a dozor powtarza je dla
            # zdarzenia zastanego co dziesiec minut (komentarz w uzgodnienie.py)
            # - czyli w oknie `CZEKAJ_DOZOR_S` zaliczenie CUDZEGO powiadomienia
            # jako swojego nie bylo teoretyczne, tylko prawdopodobne. Ogniwo 4
            # meldowaloby wtedy dzialajacy kanal alarmowy na podstawie maila
            # o czyms innym.
            #
            # `event_id` w `data` wstawia `dozor.py` przy kazdym zgloszeniu
            # kill-switcha; wzor filtra po silniku stoi tamze, przy liczeniu
            # odstepu miedzy przypomnieniami.
            r = await fetch_one(
                "SELECT count(*) AS n FROM notify.notification"
                " WHERE event_code = 'killswitch.triggered'"
                "   AND engine::text = %s"
                "   AND data->>'event_id' = %s"
                "   AND created_at > (SELECT triggered_at FROM risk.killswitch_event"
                "                     WHERE id = %s)",
                (engine, str(z["id"]), z["id"]))
            return r["n"] or None

        n = await _czekaj_na(powiadomienie, CZEKAJ_DOZOR_S, "powiadomienie")
        if not n:
            # Cichy kill-switch jest gorszy niz jego brak, bo daje zludzenie
            # kontroli. To jest osobne niepowodzenie od braku zadzialania.
            ogniwa["4 dozór -> powiadomienie"] = PADLO
            wypisz_ogniwa()
            raise Niezaliczona(
                "kill-switch zadziałał, a nikt się o tym nie dowiedział")
        print(f"  powiadomień: {n}")
        ogniwa["4 dozór -> powiadomienie"] = ZADZIALALO

        # OGNIWO 3, PYTANIE DRUGIE. Czekanie na ogniwo 4 trwalo do jedenastu
        # minut i przez ten czas silnik zrobil kilka cykli - pytamy wiec jeszcze
        # raz, za darmo, o CALE okno proby zamiast o sam jej poczatek.
        print("\n--- OGNIWO 3, pytanie 2 z 2: całe okno próby ---")
        wstrzymane_2, wejscia_2 = await _ogniwo_3(fetch_one, engine, mode, z["id"])
        wstrzymane, wejscia = max(wstrzymane, wstrzymane_2), max(wejscia, wejscia_2)

        if wejscia:
            ogniwa["3 aktywny -> silnik wstrzymał wejścia"] = PADLO
            wypisz_ogniwa()
            raise Niezaliczona(
                f"silnik zapisał {wejscia} decyzji wejściowych PO zadziałaniu "
                f"kill-switcha — bezpiecznik nie wstrzymał wejść, czyli nie "
                f"zrobił jedynej rzeczy, która chroni pieniądze")
        if wstrzymane:
            ogniwa["3 aktywny -> silnik wstrzymał wejścia"] = ZADZIALALO
            print(f"  wstrzymanych zamiarów wejścia: {wstrzymane} "
                  f"({OPIS_WSTRZYMANIA})")
        else:
            print("  ani jednego zamiaru wejścia w oknie próby — ogniwa 3 NIE "
                  "DA SIĘ z tego przebiegu rozstrzygnąć")

        # ZWOLNIENIE PRZEZ PROBE NIE JEST WZNOWIENIEM AUTOMATYCZNYM.
        #
        # `released_auto` istnieje po to, zeby dalo sie odpowiedziec na jedno
        # pytanie: CZY SYSTEM WROCIL DO HANDLU SAM. Ustawianie jej przez probe
        # zlewa dwa rozne zdarzenia w jedno - a wtedy odpowiedz na to pytanie
        # wymaga czytania tresci notatki, czyli flaga przestaje cokolwiek
        # rozstrzygac. Przy dobowej probie takich wierszy przybywa jeden dziennie
        # i po miesiacu to one zdominowalyby statystyke wznowien automatycznych.
        #
        # Proba sprzata wiec po sobie jako zwolnienie NIEAUTOMATYCZNE.
        #
        # ALE ZWALNIAJACYM NIE JEST CZLOWIEK, wiec `released_by` zostaje puste.
        # Ta kolumna jest typu bigint z kluczem obcym do app."user" (migracja
        # 0007) - miesci sie w niej wylacznie IDENTYFIKATOR KONTA, nie nazwa
        # procesu. Wpisanie tam napisu wywraca zapytanie na wiazaniu parametru,
        # a wpisanie ID wlasciciela byloby falszem w jedynym miejscu, ktore
        # odpowiada na pytanie "kto pozwolil systemowi znowu ryzykowac
        # pieniadze": interfejs pokazuje login wziety wlasnie stad
        # (services/api/routers/risk.py). Proba nie ma zreszta nadania na te
        # kolumne - 0034 daje jej UPDATE tylko na (released_at, released_auto,
        # release_note) - i to jest decyzja, a nie przeoczenie: proba nie jest
        # osoba i nie ma jej czym byc.
        #
        # Zrodlo zwolnienia rozstrzyga wiec TROJKA KOLUMN, bez czytania notatki:
        #
        #   released_auto = true                       -> wznowila sie uslugą
        #                                                 wznowienia (system SAM)
        #   released_auto = false, released_by = ID    -> zwolnil czlowiek
        #   released_auto = false, released_by = NULL  -> zwolnila proba pozarowa
        #
        # Notatka niesie reszte: co to bylo i skad przyszlo.
        wypisz_ogniwa()

        # ZDANIE KONCOWE JEST WNIOSKIEM Z POMIARU, A NIE STALA W KODZIE.
        #
        # Do 2026-09-22 stal tu bezwarunkowy `print` o wszystkich ogniwach -
        # wypisywany takze wtedy, gdy jedno z nich nie zostalo nawet odpytane.
        # Zdanie o sukcesie, ktorego nikt nie zmierzyl, jest w tym miejscu
        # gorsze od braku proby: brak proby nikogo nie uspokaja.
        niezmierzone = [n for n, s in ogniwa.items() if s != ZADZIALALO]
        if niezmierzone:
            # Zwolnienie idzie przez `finally` - z notatka BEZ prefiksu
            # potwierdzenia, wiec ten przebieg nie odnowi licznika ciszy.
            raise Nierozstrzygnieta(
                "łańcuch NIE został potwierdzony w całości — nie zmierzono: "
                + ", ".join(f"ogniwo {n}" for n in niezmierzone),
                await _dni_od_potwierdzenia(fetch_one, engine, mode))

        await execute(
            "UPDATE risk.killswitch_event SET released_at = now(),"
            " released_auto = false, release_note = %s"
            " WHERE id = %s",
            ("PRÓBA POŻAROWA ZALICZONA — zdarzenie wywołane celowo przez "
             "services/engine/proba_killswitcha.py", z["id"]))
        print("\n=== PRÓBA ZALICZONA: wszystkie cztery ogniwa ZMIERZONE "
              "i zadziałały ===")
        return 0
    finally:
        # PRZYWRACANIE W `finally`, nie na końcu bloku: próg musi wrócić także
        # wtedy, gdy próba padnie w połowie albo zostanie przerwana.
        print(f"\n--- przywracanie {LIMIT_PROBY} na {prog_przed:.2f} ---")
        # Znacznik okna schodzi razem z progiem. Gdyby ten zapis nie doszedl,
        # termin i tak wygasnie sam - patrz `OKNO_PROBY_S`.
        await execute(
            "UPDATE risk.limit_def SET value = %s, updated_at = now(), proba_do = NULL"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s",
            (prog_przed, engine, mode, LIMIT_PROBY))
        po = await fetch_one(
            "SELECT value FROM risk.limit_def WHERE engine::text=%s AND mode::text=%s"
            "   AND name=%s", (engine, mode, LIMIT_PROBY))
        print(f"    próg po przywróceniu: {float(po['value']):.2f}")

        # ZDARZENIE PROBY TEZ WRACA, I Z TEGO SAMEGO POWODU CO PROG.
        #
        # Kill-switch wywolany przez probe nie opisuje zadnego stanu rachunku -
        # powstal dlatego, ze `min_equity` stalo przez chwile powyzej kapitalu.
        # Przywrocenie progu kasuje przyczyne, ale nie zdarzenie, a zdarzenie
        # z `released_at IS NULL` ZATRZYMUJE HANDEL (`evaluate_account`, pkt 1).
        # Proba padajaca na ogniwie 3-4 zostawiala wiec silnik wstrzymany na
        # czas nieokreslony - wlasnym artefaktem, nie decyzja ryzyka.
        #
        # Od 2026-09-10 kosztuje to podwojnie: nastepne przebiegi widza aktywny
        # kill-switch i koncza sie ZEREM jako NIEROZSTRZYGNIETE przez
        # MAX_DNI_BEZ_POTWIERDZENIA dob. CronJob swieci na zielono przy silniku,
        # ktory nie handluje - a kanal, ktory mial o tym powiedziec (dozor),
        # jest dokladnie tym ogniwem, ktorego awaria to spowodowala.
        #
        # `AND released_at IS NULL` sprawia, ze na sciezce ZALICZONEJ jest to
        # zapis pusty: zwolnienie poszlo juz wyzej, razem z wlasciwa notatka.
        # Zwalniamy WYLACZNIE wlasne zdarzenie - cudzego proba nie dotyka,
        # i to jest ten sam powod, dla ktorego w ogole nie startuje przy
        # aktywnym kill-switchu.
        if moje_zdarzenie is not None:
            await execute(
                "UPDATE risk.killswitch_event SET released_at = now(),"
                " released_auto = false, release_note = %s"
                " WHERE id = %s AND released_at IS NULL",
                ("PRÓBA POŻAROWA NIEDOKOŃCZONA — zdarzenie wywołane celowo przez "
                 "services/engine/proba_killswitcha.py, przyczyna (podniesiony "
                 "próg) już nie istnieje. Łańcuch NIE został potwierdzony "
                 "w całości: próba padła albo któregoś ogniwa nie zmierzyła.",
                 moje_zdarzenie))


async def uruchom(fetch_all, fetch_one, execute, **kw) -> int:
    """Przeprowadza próbę i zamienia jej wynik na KOD WYJŚCIA.

    Rozdział wyników od kodów siedzi tutaj, a nie w `main`, bo `main` zajmuje
    się pulą połączeń i argumentami — a to jest jedyne miejsce, w którym zapada
    decyzja „czy CronJob ma zaświecić na czerwono". Osobna funkcja znaczy też,
    że tę decyzję da się sprawdzić testem bez bazy.

        0  zaliczona albo nierozstrzygnięta w granicach ciszy
        1  NIEZALICZONA — łańcuch padł, zabezpieczenie nie działa
        2  NIESPRAWDZONE zbyt długo — cisza sama stała się awarią

    Dwójka zamiast jedynki nie jest ozdobnikiem: oba kody kończą pod na
    czerwono, ale mówią o dwóch różnych rzeczach i wymagają dwóch różnych
    reakcji (napraw łańcuch / zwolnij kill-switch albo zdecyduj świadomie, że
    handel stoi dalej). W logu zadania widać je bez czytania treści.
    """
    try:
        return await przeprowadz(fetch_all, fetch_one, execute, **kw)
    except Niezaliczona as e:
        print(f"\nNIEZALICZONA: {e}", file=sys.stderr)
        print("  To jest awaria samego zabezpieczenia, nie próby.", file=sys.stderr)
        return 1
    except Nierozstrzygnieta as e:
        print(f"\nNIEROZSTRZYGNIĘTA: {e}", file=sys.stderr)
        print("  ZABEZPIECZENIE NIE ZOSTAŁO W TYM CYKLU SPRAWDZONE — to nie "
              "jest potwierdzenie,", file=sys.stderr)
        print("  że kill-switch zadziała, tylko brak możliwości sprawdzenia.",
              file=sys.stderr)
        if e.dni is None:
            # CISZY NIE DA SIE ZMIERZYC, BO NIE MA OD CZEGO. Zadna proba nie
            # potwierdzila dotad calego lancucha, wiec licznik nie ma poczatku -
            # a wpisanie tu zera albo nieskonczonosci byloby wymyslona data.
            # Roznica jest widoczna w komunikacie i nie zapala zadania.
            print("  Pełnego potwierdzenia łańcucha NIE MA w dzienniku ani "
                  "razu — licznik ciszy nie ma od czego liczyć.",
                  file=sys.stderr)
            return 0
        print(f"  Od ostatniego potwierdzenia: {e.dni:.1f} doby "
              f"(próg: {MAX_DNI_BEZ_POTWIERDZENIA}).", file=sys.stderr)
        if e.dni > MAX_DNI_BEZ_POTWIERDZENIA:
            print(f"\nNIESPRAWDZONE od {e.dni:.1f} doby — sama ta cisza jest "
                  f"już awarią.", file=sys.stderr)
            print("  Handel stoi, więc nic nie ryzykuje TERAZ. Ryzyko powstaje "
                  "w chwili zwolnienia", file=sys.stderr)
            print("  kill-switcha: system wróci do handlu z bezpiecznikiem, "
                  "którego od tygodnia", file=sys.stderr)
            print("  nikt nie sprawdził.", file=sys.stderr)
            return 2
        return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--sucho", action="store_true",
                    help="wypisz plan, nie dotykaj limitu")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")
    if args.mode != "demo" and not os.environ.get("PROBA_NA_LIVE"):
        # Na koncie live proba wstrzymuje wejscia na kilkanascie minut. To nie
        # jest katastrofa, ale nie jest tez niczym - wymaga swiadomej zgody.
        sys.exit("Próba na koncie innym niż demo wymaga PROBA_NA_LIVE=1")

    from services.api.db import close_pool, execute, fetch_all, fetch_one, open_pool

    await open_pool()
    try:
        return await uruchom(fetch_all, fetch_one, execute,
                             engine=args.engine, mode=args.mode,
                             sucho=args.sucho)
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
