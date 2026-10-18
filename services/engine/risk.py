"""Risk kernel: ostatnia instancja przed wyslaniem zlecenia.

Model moze CHCIEC wejsc w pozycje. Ten modul decyduje, czy wolno - i jest
celowo napisany tak, zeby nie dalo sie go obejsc przez prompt, konfiguracje
modelu ani zmiane wag. Limity pochodza z risk.limit_def w bazie i sa sprawdzane
przy kazdej decyzji.

Kolejnosc kontroli jest istotna: najpierw to, co zatrzymuje CALY silnik
(kill-switch, dzienna strata, obsuniecie), potem to, co dotyczy pojedynczego
zlecenia (wielkosc, ekspozycja, czestotliwosc). Inaczej system moglby zmniejszyc
zlecenie i wyslac je mimo aktywnego kill-switcha.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from services.engine import stan_kapitalu
from services.engine.risk_rules import (
    RiskState, RiskVerdict, evaluate_account, evaluate_limits,
)


class BrakLimitow(RuntimeError):
    """Silnik nie ma limitow, ktore musi miec. To nie jest stan do obejscia."""


# Limity, bez ktorych handel nie ma prawa ruszyc. Wszystkie trzy koncza sie
# kill-switchem, czyli to one stanowia PODLOGE KAPITALU - a wlasnie o nia
# wlascicielowi chodzi. Limity o dzialaniu `block` i `downsize` ograniczaja
# wielkosc pojedynczej decyzji i ich brak jest kosztowny, ale nie grozny tak
# samo: bez podlogi nie ma czego bronic.
LIMITY_KONIECZNE = ("max_daily_loss", "max_drawdown", "min_equity")


# Co ile sekund pomiar limitow konta ma zostawiac slad w risk.limit_usage.
#
# DLACZEGO POMIAR W OGOLE MUSI ZOSTAWIAC SLAD
#
# `check_account()` liczy zuzycie kazdego limitu konta w kazdym cyklu i do tej
# pory NIE ZAPISYWALO NICZEGO. Jedynym pisarzem do risk.limit_usage byla sciezka
# wejscia w pozycje, wiec w bazie nie ma ani jednego pomiaru z dni, w ktorych
# silnik nie wchodzil - a to sa dokladnie te dni, w ktorych bezpiecznik jest
# jedyna rzecza, jaka system robi. Zdanie "ochrona kapitalu czuwa" bylo wiec
# ZALOZENIEM, nie faktem: gdyby `check_account` przestalo byc wolane albo
# zaczelo sie wywracac po cichu, dane wygladalyby identycznie.
#
# DLACZEGO STALY RYTM, A NIE "ZAPIS PRZY ZMIANIE"
#
# Zapis przy zmianie wartosci albo przy przekroczeniu progu wykorzystania jest
# tanszy, ale niszczy jedyna wlasciwosc, dla ktorej to robimy: CISZA PRZESTAJE
# BYC JEDNOZNACZNA. Brak wiersza znaczylby wtedy albo "nic sie nie zmienilo",
# albo "nikt nie mierzy" - czyli powtarzalby dokladnie te wade, ktora ta zmiana
# usuwa. Puls, ktory bije tylko wtedy, gdy cos sie dzieje, nie jest pulsem.
#
# DLACZEGO 300 s, SKORO DOZORCA CHODZI CO 60 s
#
# Konto ocenia dzis DWA procesy: petla decyzyjna co 300 s i dozorca ryzyka co
# 60 s (services/engine/dozorca.py). Rytm jest wspolny dla obu, bo trzyma go
# BAZA, a nie proces - warunek "czy jest juz wiersz mlodszy niz 300 s" widzi
# zapisy obu. Dzieki temu koszt wyznacza najwolniejszy z nich, a nie najszybszy,
# a wiersze nie sa kopiami tej samej sekundy z dwoch procesow.
#
# KOSZT, POLICZONY: 3 limity konta x 288 pomiarow na dobe = 864 wiersze dziennie
# na silnik, ~1730 przy dwoch silnikach, ~630 tys. rocznie. W hypertable po
# ~120 B z indeksem to ~75 MB rocznie - mniej niz dobowy przyrost swiec. Za te
# cene dostajemy szereg czasowy wykorzystania kazdego limitu, na ktorym widac
# NARASTANIE, a nie tylko moment przekroczenia.
#
# CZEGO TA STALA NIE DOTYCZY: sciezki decyzyjnej. Tam `record_usage` wolane jest
# bez rytmu, bo pomiar towarzyszy KONKRETNEJ decyzji i jest jej czescia.
POMIAR_CO_S = 300


def sprawdz_komplet(limity: dict) -> list[str]:
    """Ktorych koniecznych limitow brakuje. CZYSTA FUNKCJA.

    Sprawdzamy KONKRETNE NAZWY, nie samo "czy cokolwiek jest". Zasianie dla
    nowego trybu jednego limitu na liczbe zlecen wygladaloby jak komplet,
    a zostawialoby rachunek bez podlogi.
    """
    return [n for n in LIMITY_KONIECZNE if n not in limity]


# Ile moze miec pomiar kapitalu, zanim przestaniemy nazywac go POMIAREM.
#
# NIKT NIE PYTAL, ILE LAT MA TA LICZBA. Wszyscy trzej czytelnicy `exec.equity_point`
# (ten kernel, dozorca ryzyka i `wznowienie.stan_konta`) brali ostatni wiersz bez
# patrzenia na `event_time`. Zapytanie "ostatni punkt krzywej" odpowiada tak samo chetnie
# dla punktu sprzed pieciu minut, jak dla punktu sprzed trzech dni - i bez wieku
# tych dwoch przypadkow NIE DA SIE odroznic. To ten sam blad, ktory po stronie
# ceny naprawia `cena_odniesienia` w loop.py, tyle ze o poziom wyzej: tam
# zamrozona byla cena jednego instrumentu, tu zamrozony jest CALY rachunek.
#
# Skutek jest cichy w najgorszy sposob: zawieszona petla (pod zyje, cykl nie
# chodzi) zamraza krzywa, a zamrozona krzywa nie ma obsuniecia, nie ma straty
# dziennej i nie schodzi do progu kapitalu. Wszystkie trzy limity krytyczne
# licza sie wtedy z liczby, ktorej nikt nie zmierzyl, i kazdy z nich melduje
# `allow` - z bijacym pulsem i zielonym punktem w API.
#
# DLACZEGO 15 MINUT. Petla decyzyjna budzi sie co 300 s (values.yaml, komenda
# poda `silnik`), wiec 15 minut to TRZY POMINIETE CYKLE - dokladnie ta sama
# zasada, z ktorej wziete sa CENA_STARA_PO_MIN w loop.py i MARTWY_PO_MIN
# w dozorze: jedno spoznienie to wahanie, trzy to sygnal. Zmierzone na
# produkcji 2026-09-09 (3713 odstepow miedzy punktami od 27.08): mediana 300 s,
# p95 300 s, p99 360 s. Prog lezy wiec 2,5x nad najgorszym normalnym odstepem,
# a obie prawdziwe przerwy (88 i 83 minuty) miesci z ogromnym zapasem.
#
# Wiek liczymy w momencie sprawdzenia, czyli NA POCZATKU cyklu, a punkt
# zapisuje sie na jego KONCU - zdrowy odczyt ma tu naturalnie 5-6 minut, nie
# zero. Prog musi to uwzgledniac i uwzglednia: 15 > 6.
#
# Prog, ktory miga, konczy tak samo jak czujka, ktora miga - ktos ja podniesie,
# zeby przestala przeszkadzac.
#
# KTO PISZE KRZYWA - I DLACZEGO TEN PROG NIE OBOWIAZUJE OBU SILNIKOW JEDNAKOWO.
#
# Do 2026-09-22 stalo tu zdanie "`exec.equity_point` pisze wylacznie
# `loop.update_equity`". Przestalo byc prawda tego dnia i to zdanie wlasnie
# usypialo wade z ustalenia 12: pisarzy jest TRZECH, kazdy z inna definicja
# kapitalu (patrz `stan_kapitalu`, sekcja ZRODLA), a od 2026-09-26 obowiazuje
# zasada JEDEN PISARZ NA SERIE (`stan_kapitalu.KRZYWA_Z_KSIAG_GIELDY`).
#
# Dla `crypto` pisze petla co 300 s, wiec 15 minut to nadal trzy pominiete
# cykle i rachunek wyzej obowiazuje bez zmian.
#
# Dla `equity` pisze `kapital-equity` co 900 s (values.yaml), czyli JEDEN
# pominiety cykl wystarcza, zeby pomiar byl "stary". Prog nie jest wiec dla
# tego silnika trzykrotnym zapasem, tylko granica. Stan ten ISTNIEJE OD
# 2026-09-22, a nie od decyzji o jednym pisarzu - ale decyzja czyni go trwalym
# i dlatego jest tu nazwany, zamiast czekac, az objawi sie jako `block`
# w przypadkowej minucie. Prog wieku pomiaru per silnik to osobna decyzja
# (razem z brakiem dozorcy dla `engine='equity'`), nie poprawka przy okazji.
POMIAR_STARY_PO_MIN = 15

# PROG DLA KRZYWEJ Z KSIAG GIELDY - DECYZJA Z 2026-10-18.
#
# Objawilo sie dokladnie to, co zdanie wyzej zapowiadalo: przy zapisie co
# 900 s i progu 15 min kazdy cykl `kapital-equity` zostawial okno, w ktorym
# pomiar byl "sprzed 16 min" - a `kapital_gieldy` przycinal jeszcze
# `event_time` do pelnej minuty, dokladajac do wieku 0-60 s. Dozorca
# i `check_account` blokowaly wtedy wejscia, choc proces pisal co do sekundy
# na czas.
#
# Prog = DWA interwaly zapisu: jeden pominiety cykl to wahanie (restart poda,
# chwilowy blad API Alpaki), dwa to krzywa, ktora stoi. To slabsza ochrona niz
# "trzy cykle" krypto, ale krypto liczy cykle po 5 min (15 min), a tu jeden
# cykl to 15 min - trzy dalyby 45 min handlu na liczbie, ktorej nikt nie
# odswiezyl. Krypto zostaje przy 15 min: pisze petla co 300 s na KONCU cyklu,
# a wiek mierzy sie na POCZATKU, wiec zdrowy odczyt ma 5-6 min i do progu
# brakuje 9 - wyscigu, ktory tu zachodzil, tam nie ma.
#
# Interwal musi zgadzac sie z `--interval` workera `kapital-equity`
# w charts/trader/values.yaml - pilnuje tego test (`test_wiek_pomiaru_kapitalu`).
INTERWAL_KSIAG_GIELDY_S = 900
POMIAR_STARY_PO_MIN_KSIEGI = 2 * INTERWAL_KSIAG_GIELDY_S / 60


def prog_wieku_pomiaru(engine: str) -> float:
    """Prog wieku krzywej kapitalu w minutach - zalezy od tego, KTO ja pisze."""
    return (POMIAR_STARY_PO_MIN_KSIEGI
            if stan_kapitalu.zrodlo_obowiazujace(engine) == stan_kapitalu.ZRODLO_KSIEGI_GIELDY
            else POMIAR_STARY_PO_MIN)


def ocen_wiek_pomiaru(wiek_min: float | None,
                      prog_min: float = POMIAR_STARY_PO_MIN) -> str | None:
    """Czy krzywa kapitalu jest jeszcze POMIAREM. CZYSTA FUNKCJA.

    Zwraca powod wstrzymania albo None, gdy pomiar jest swiezy.

    `wiek_min is None` znaczy "nie ma ani jednego punktu krzywej" i tez jest
    powodem: brak pomiaru nie jest stanem "wszystko w porzadku", tylko stanem
    "nie wiadomo" - ta sama zasada co przy braku limitow wyzej.
    """
    if wiek_min is None:
        return ("brak jakiegokolwiek pomiaru kapitału (exec.equity_point pusty) "
                "— limity nie mają czego oceniać")
    if wiek_min > prog_min:
        return (f"pomiar kapitału sprzed {wiek_min:.0f} min "
                f"(próg {prog_min:.0f} min): krzywa stoi, więc obsunięcie, "
                f"strata dzienna i próg kapitału liczą się z liczby, "
                f"której nikt nie odświeżył")
    return None


class RiskKernel:
    def __init__(self, conn, engine: str, mode: str = "demo"):
        self.conn = conn
        self.engine = engine
        self.mode = mode
        self._limits = self._load_limits()

    def _load_limits(self) -> dict[str, dict]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, name, value, unit, action, instrument_id FROM risk.limit_def"
            " WHERE engine = %s AND mode = %s AND enabled",
            (self.engine, self.mode),
        )
        limity = {r["name"]: dict(r) for r in cur.fetchall()}
        brakujace = sprawdz_komplet(limity)
        if brakujace:
            # BRAK LIMITU NIE ZNACZY "BEZ OGRANICZEN", TYLKO "NIE WIADOMO".
            #
            # Zapytanie filtruje po `mode`, a limity byly zasiane WYLACZNIE dla
            # trybu demo. Przelaczenie na `live` zwracalo wiec pusty slownik,
            # a `evaluate_account` przechodzilo przez wszystkie `if lim:` i
            # oddawalo werdykt `allow`. Znaczylo to, ze pierwszy dzien
            # z prawdziwymi pieniedzmi jest dokladnie tym dniem, w ktorym cala
            # architektura bezpiecznika jest wylaczona - bez zadnego bledu,
            # z bijacym heartbeatem i zielonym statusem.
            #
            # Dozorca buduje ten sam obiekt, wiec niezalezny nadzorca milczalby
            # tak samo. Dlatego to wyjatek, a nie ostrzezenie.
            raise BrakLimitow(
                f"silnik {self.engine}/{self.mode} nie ma limitow: "
                f"{', '.join(brakujace)}. Handel bez nich nie jest handlem "
                f"z wiekszym ryzykiem - jest handlem BEZ OCENY ryzyka")
        return limity

    # ------------------------------------------------------------- stan konta
    def kapital(self) -> Decimal | None:
        """Kapital, wzgledem ktorego licza sie limity procentowe. None = NIE WIADOMO.

        JEDNO ZRODLO DLA LIMITU I DLA WIELKOSCI ZLECENIA - po to ta metoda jest
        publiczna. Petla decyzyjna wyprowadza z tej samej liczby budzet na
        pozycje (`risk_rules.position_budget`), a `evaluate_limits` sprawdza nia
        `max_total_exposure` i `max_position_notional`. Gdyby budzet bral kapital
        skadinad, obie strony liczylyby procenty od dwoch roznych liczb -
        dokladnie ten uklad, ktory do 2026-09-09 dawal budzet 1500 na rachunku,
        na ktorym limit wynosil 1500 lacznie.

        None ZNACZY "NIE ZMIERZONO" i nie wolno go zamieniac na zero ani na
        kapital startowy. Zero przeszloby przez `wartosc_limitu` jako limit
        zerowy (system stanalby, ale z powodem opisujacym nie ten stan, ktory
        zaszedl), a stala 100 000 dawalaby profil ryzyka dziesieciokrotnie
        wyzszy od zmierzonego na rachunku 10 000. Wolajacy ma na None jedna
        odpowiedz: wstrzymac wejscia.

        O WIEK tej liczby pyta osobno `wiek_pomiaru_min()`: punkt krzywej moze
        istniec i byc sprzed trzech dni, a to jest inny brak niz brak punktu.
        """
        cur = self.conn.cursor()
        cur.execute(stan_kapitalu.SQL_EQUITY_BIEZ, (self.engine, self.mode))
        row = cur.fetchone()
        if not row or row.get("equity") is None:
            return None
        return Decimal(row["equity"])

    def _equity(self) -> tuple[Decimal, Decimal, Decimal]:
        """Zwraca (kapital biezacy, szczyt, kapital sprzed doby) - W PIENIADZACH.

        Do oceny WYNIKU sluzy `_wynik()`: kapital rosnie od wplat, wiec liczony
        z niego szczyt podnosi sie po kazdym przelewie, a obsuniecie od takiego
        szczytu mierzy przelew, nie strategie.
        """
        cur = self.conn.cursor()
        # Zero przy braku punktu ZOSTAJE tutaj i jest celowe: reguly konta
        # (`min_equity`, obsuniecie) maja wtedy zadzialac, a nie przepuscic.
        # Rozroznienie "zero" od "nie wiadomo" nalezy do `kapital()`, bo tylko
        # tam ma konsekwencje - wielkosci zlecenia nie wolno liczyc ze zgadywanej
        # liczby, a limit wolno oceniac konserwatywnie.
        current = self.kapital() or Decimal(0)

        cur.execute(stan_kapitalu.SQL_EQUITY_SZCZYT, (self.engine, self.mode))
        peak = Decimal((cur.fetchone() or {}).get("peak") or current)

        cur.execute(stan_kapitalu.SQL_EQUITY_DOBA, (self.engine, self.mode))
        row = cur.fetchone()
        if not row:
            # RACHUNEK MLODSZY NIZ DOBA - patrz komentarz przy `_wynik()`.
            # Odniesieniem jest POCZATEK historii, a nie punkt biezacy: dla
            # rachunku, ktory istnieje od trzech godzin, "od poczatku" i "od
            # doby" to ta sama rzecz. Spadek na punkt biezacy dawal zawsze 0%.
            cur.execute(stan_kapitalu.SQL_EQUITY_PIERWSZY, (self.engine, self.mode))
            row = cur.fetchone()
        day_ago = Decimal(row["equity"]) if row else current
        return current, peak, day_ago

    def _wynik(self) -> tuple[Decimal | None, Decimal | None, Decimal | None,
                              Decimal | None, bool]:
        """Wartosc jednostki: biezaca, szczytowa, sprzed doby, wplaty netto, PELNOSC.

        None w kazdym miejscu, gdzie danych nie ma - reguly spadaja wtedy na
        kapital, czyli na zachowanie sprzed migracji 0031. Dzieki temu proces
        nie wywraca sie na bazie, w ktorej krzywej jeszcze nie przeliczono.

        PIATA WARTOSC MOWI, CZY WOLNO UFAC SZCZYTOWI, i jest odpowiedzia na to,
        ze `max(nav)` liczy sie po kolumnie, ktorej migracja 0031 nie uzupelnila
        wstecz. Punkt krzywej bez `nav` nie jest dla tej miary punktem NIZSZYM -
        jest punktem NIEISTNIEJACYM, wiec szczyt sprzed migracji przepada bez
        sladu, a obsuniecie wychodzi mniejsze, niz jest naprawde. Na produkcji
        2026-09-09: 3190 z 3714 punktow bez `nav`, obsuniecie 0,0101% zamiast
        0,0429%.

        Warunkiem jest BRAK LUK W CALEJ KRZYWEJ, a nie samo "nav siega
        poczatku": dziura w srodku moze przykryc szczyt tak samo skutecznie jak
        dziura na poczatku. Pytamy wiec o liczbe punktow bez `nav` - zero znaczy
        "szczyt jest szczytem", cokolwiek innego cofa pomiar na kapital.
        """
        cur = self.conn.cursor()
        cur.execute(stan_kapitalu.SQL_NAV_BIEZ, (self.engine, self.mode))
        biez = cur.fetchone()
        if not biez:
            return None, None, None, None, False

        cur.execute(stan_kapitalu.SQL_NAV_AGREGAT, (self.engine, self.mode))
        agr = cur.fetchone() or {}
        szczyt = agr.get("szczyt")
        # None traktujemy jak luke: brak odpowiedzi na pytanie o pelnosc nie
        # jest odpowiedzia "pelna".
        od_poczatku = agr.get("bez_nav") == 0

        cur.execute(stan_kapitalu.SQL_NAV_DOBA, (self.engine, self.mode))
        doba = cur.fetchone()
        if not doba:
            # PIERWSZA DOBA RACHUNKU NIE MOZE BYC DOBA BEZ LIMITU STRATY DZIENNEJ.
            #
            # Brak punktu sprzed 24 h znaczy dokladnie jedno: CALA historia jest
            # mlodsza niz doba (gdyby istnial punkt starszy, zapytanie wyzej by
            # go zwrocilo). Odniesieniem jest wiec POCZATEK tej historii - dla
            # rachunku istniejacego od trzech godzin "ile stracilem dzisiaj"
            # i "ile stracilem od poczatku" to to samo pytanie.
            #
            # Wczesniej brak punktu oznaczal spadek na nav BIEZACE, czyli
            # dzielenie liczby przez samą siebie: strata dzienna wychodzila
            # 0,00% niezaleznie od tego, ile rachunek stracil. Limit nie mial
            # jak zadzialac przez pierwsza dobe - bez bledu i z zielonym
            # statusem, czyli w jedynej postaci, ktorej nie widac.
            #
            # Dwa inne miejsca liczyly to poprawnie od poczatku: `overview.py`
            # spada na pierwszy punkt krzywej, a symulacja portfelowa na
            # START_EQUITY. Rozjechala sie wylacznie sciezka bezpiecznika -
            # czyli ta jedna, w ktorej cisza kosztuje pieniadze.
            #
            # DOTYCZY TO KAZDEGO SWIEZEGO RACHUNKU, nie tylko pierwszego:
            # migracja 0031 zaczela liczyc nav 2026-09-07, wiec przez pierwsza
            # dobe po niej produkcja tez nie miala limitu straty dziennej
            # (przed migracja dzialal, bo `_equity()` mialo historie od 27.08).
            cur.execute(stan_kapitalu.SQL_NAV_PIERWSZY, (self.engine, self.mode))
            doba = cur.fetchone()

        nav = Decimal(biez["nav"])
        return (nav,
                Decimal(szczyt) if szczyt is not None else nav,
                Decimal(doba["nav"]) if doba else nav,
                Decimal(biez["wplaty_netto"]) if biez["wplaty_netto"] is not None else None,
                od_poczatku)

    def _open_exposure(self) -> tuple[Decimal, dict[int, Decimal]]:
        """Kapital ZAANGAZOWANY: pozycje ORAZ zlecenia, ktore jeszcze nimi nie sa.

        DLACZEGO NIE SAME POZYCJE (naprawa 2026-09-09)

        Do tego dnia liczylo sie tu wylacznie `exec.position`. Produkcja chodzi
        z `--limit`, wiec pozycja powstaje dopiero przy wypelnieniu w NASTEPNEJ
        swiecy. Kazde kolejne zlecenie tego samego cyklu widzialo wiec te sama
        ekspozycje sprzed cyklu - `max_total_exposure` nie kumulowal sie NIGDY.
        Dziesiec instrumentow oznaczalo dziesiec zlecen, z ktorych kazde z osobna
        miescilo sie w limicie, a razem przekraczaly go wielokrotnie. Jedynym
        dzialajacym bezpiecznikiem zostawal `max_position_notional`, ktory tnie
        POJEDYNCZE zlecenie i o sumie nie ma nic do powiedzenia.

        Zamiar zaangazowania pieniedzy jest zaangazowaniem. Zlecenie lezace
        w arkuszu wypelni sie bez pytania kogokolwiek o zdanie, wiec limit,
        ktory go nie widzi, mierzy przeszlosc, a nie ryzyko.

        SKLADAMY TRZY ZRODLA, PILNUJAC, ZEBY NIC NIE POLICZYC DWA RAZY:

          1. otwarte pozycje - z `wycen()`, czyli PO CENIE RYNKOWEJ;
          2. zlecenia zlozone i jeszcze nierozstrzygniete (`exec.order`
             w stanie new/partial/unknown) - po cenie limitu;
          3. plany, ktore na gielde jeszcze nie poszly (`journal.planned_order`
             w stanie 'pending' BEZ wiersza w `exec.order`).

        Granica miedzy (2) a (3) jest dokladnie ta sama, ktorej uzywa
        `gielda_exec.zloz_oczekujace` do wyboru planow do wyslania:
        `NOT EXISTS (SELECT 1 FROM exec.order WHERE planned_order_id = p.id)`.
        Plan zostaje 'pending' takze po zlozeniu, wiec bez tego warunku jedno
        zlecenie liczyloby sie i jako plan, i jako zlecenie.

        LICZYMY TYLKO WEJSCIA. Plan wyjscia to zamiar ZMNIEJSZENIA ekspozycji;
        doliczenie go blokowaloby wejscia tym mocniej, im wiecej pozycji system
        wlasnie zamyka - czyli dokladnie odwrotnie, niz limit ma dzialac.

        DLACZEGO CENA RYNKOWA, A NIE KOSZT (`qty * avg_price`)

        Pozycja, ktora urosla, angazuje dzis wiecej pieniedzy, niz kosztowala:
        gdy rynek zawroci, tracimy jej wartosc BIEZACA, nie cene wejscia. Liczac
        kosztem, zwyzka cicho zwalniala miejsce pod kolejne zakupy - limit
        luzowal sie dokladnie wtedy, gdy ekspozycja rosla. Drugi powod jest
        rownie wazny: `update_equity` zapisuje do `exec.equity_point.exposure`
        wielkosc rynkowa, wiec liczba pokazywana wlascicielowi i liczba
        egzekwowana przez bezpiecznik byly dwiema roznymi wielkosciami o tej
        samej nazwie.

        `wycen()` zna juz odpowiedz na "co, gdy nie ma swiezej ceny" (bierze
        najlepsza, jaka jest, i melduje brak) - powtarzanie tej reguly tutaj
        wlasnym SQL-em byloby jej druga kopia. Import jest lokalny, bo
        `loop` importuje ten modul; na poziomie pliku dalby cykl.
        """
        from services.engine.loop import pozycje_do_wyceny, wycen

        cur = self.conn.cursor()
        per: dict[int, Decimal] = {}

        def dodaj(instrument_id: int, kwota: Decimal) -> None:
            per[instrument_id] = per.get(instrument_id, Decimal(0)) + kwota

        # 1. POZYCJE, wycenione tak samo jak krzywa kapitalu.
        for iid, kwota in wycen(pozycje_do_wyceny(
                cur, self.engine, self.mode,
                datetime.now(timezone.utc))).per_instrument.items():
            dodaj(iid, kwota)

        # 2. ZLECENIA ZLOZONE, jeszcze nierozstrzygniete. 'unknown' znaczy
        # "odpowiedz gieldy nie dotarla" - najgrozniejszy z tych stanow, bo
        # zlecenie moglo wejsc. Pominiecie go byloby uznaniem niewiedzy za zero.
        cur.execute(
            "SELECT o.instrument_id,"
            "       sum(o.qty * COALESCE(o.limit_price, p.limit_price, 0)) AS notional"
            "  FROM exec.order o"
            "  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id"
            " WHERE o.engine=%s AND o.mode=%s"
            "   AND o.status IN ('new','partial','unknown')"
            # Zlecenie bez planu pochodzi ze sciezki rynkowej (rachunek
            # papierowy) i wejsciem jest domyslnie - stad COALESCE, a nie
            # cichy JOIN, ktory takie wiersze by zgubil.
            "   AND COALESCE(p.trigger->>'rodzaj', 'wejscie') = 'wejscie'"
            " GROUP BY o.instrument_id",
            (self.engine, self.mode))
        for r in cur.fetchall():
            dodaj(r["instrument_id"], Decimal(r["notional"] or 0))

        # 3. PLANY JESZCZE NIEZLOZONE - w tym te z BIEZACEGO cyklu. To one
        # sprawiaja, ze limit kumuluje sie w obrebie jednego przebiegu.
        # `planned_order` nie ma kolumn engine/mode, wiec silnik i tryb bierzemy
        # z decyzji, ktora plan zrodzila.
        cur.execute(
            "SELECT p.instrument_id, sum(p.qty * p.limit_price) AS notional"
            "  FROM journal.planned_order p"
            "  JOIN journal.decision d ON d.id = p.decision_id"
            " WHERE d.engine=%s AND d.mode=%s AND p.status='pending'"
            "   AND p.trigger->>'rodzaj' = 'wejscie'"
            "   AND NOT EXISTS (SELECT 1 FROM exec.order o"
            "                    WHERE o.planned_order_id = p.id)"
            " GROUP BY p.instrument_id",
            (self.engine, self.mode))
        for r in cur.fetchall():
            dodaj(r["instrument_id"], Decimal(r["notional"] or 0))

        return sum(per.values(), Decimal(0)), per

    def ekspozycja(self) -> Decimal:
        """Laczna ekspozycja z `_open_exposure` - ta sama, ktora ocenia
        `max_total_exposure`. Dla wlasnego sufitu strategii momentum
        (`strategia_momentum.miesci_sie`), zeby nie liczyc jej druga kopia."""
        return self._open_exposure()[0]

    def _orders_last_hour(self) -> int:
        """Ile zlecen poszlo w ostatniej godzinie - RAZEM Z BIEZACYM CYKLEM.

        DLACZEGO NIE SAM `exec.order` (naprawa 2026-09-23)

        Dokladnie ten sam ksztalt wady, ktory 2026-09-09 naprawiono dla
        `max_total_exposure`, tyle ze w liczniku zlecen. Produkcja chodzi
        z `--limit`, wiec wiersz w `exec.order` powstaje dopiero w
        `zloz_oczekujace`, czyli na KONCU przebiegu. Wszystkie wejscia jednego
        cyklu czytaly wiec te sama liczbe sprzed cyklu:

            19 zlecen w poprzedniej godzinie, limit 20, 10 instrumentow
            w cyklu -> kazde widzi `19 < 20` i przechodzi -> po cyklu jest ich
            29 przy limicie 20

        Limit czestotliwosci istnieje jako OCHRONA PRZED PETLA BLEDU
        (`risk_rules.evaluate_limits`, punkt 4) - czyli przed dokladnie taka
        seria. Nie moze byc egzekwowalny wylacznie miedzy cyklami.

        Rozjazd byl przy tym jednostronny i dotyczyl kalibracji:
        `portfolio.simulate_portfolio` inkrementuje `orders_h` w obrebie swiecy
        (`portfolio.py`), wiec backtest mierzyl limit, ktorego produkcja nie
        miala.

        PLANY LICZYMY PRZEZ TEN SAM JOIN CO `_open_exposure`.
        `journal.planned_order` nie ma kolumn engine/mode (migracja 0005), wiec
        silnik i tryb bierzemy z decyzji, ktora plan zrodzila. `NOT EXISTS`
        wyklucza podwojne liczenie planu, ktory ma juz swoj wiersz zlecenia.

        OKNO Z `created_at` PLANU, a nie z `submitted_at` zlecenia, bo zlecenia
        jeszcze nie ma - to jest caly sens tego doliczenia.

        RODZAJU PLANU NIE FILTRUJEMY, w odroznieniu od `_open_exposure`.
        Tamten liczy KAPITAL, wiec bierze same wejscia; ten liczy ZLECENIA,
        a wyjscie jest zleceniem tak samo jak wejscie - `exec.order` po stronie
        zrealizowanej tez ich nie rozdziela.

        DWA ZAPYTANIA, A NIE JEDNO Z DWOMA PODZAPYTANIAMI. Licza dwie rozne
        rzeczy w dwoch roznych tabelach i po dwoch roznych znacznikach czasu
        (`submitted_at` zlecenia, `created_at` planu). Zlaczone w jeden
        `SELECT` czytaly sie jak jedna wielkosc, a przy pierwszej zmianie okna
        latwo bylo poprawic tylko polowe.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT count(*) AS n FROM exec.order WHERE engine=%s AND mode=%s"
            " AND submitted_at > now() - interval '1 hour'", (self.engine, self.mode))
        zlozone = (cur.fetchone() or {}).get("n", 0)

        cur.execute(
            "SELECT count(*) AS n FROM journal.planned_order p"
            "  JOIN journal.decision d ON d.id = p.decision_id"
            " WHERE d.engine=%s AND d.mode=%s AND p.status='pending'"
            "   AND p.created_at > now() - interval '1 hour'"
            "   AND NOT EXISTS (SELECT 1 FROM exec.order o"
            "                    WHERE o.planned_order_id = p.id)",
            (self.engine, self.mode))
        zaplanowane = (cur.fetchone() or {}).get("n", 0)
        return zlozone + zaplanowane

    def killswitch_active(self) -> dict | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, reason, triggered_at FROM risk.killswitch_event"
            " WHERE engine=%s AND mode=%s AND released_at IS NULL"
            " ORDER BY triggered_at DESC LIMIT 1", (self.engine, self.mode))
        return cur.fetchone()

    def wiek_pomiaru_min(self) -> float | None:
        """Ile minut ma najswiezszy punkt krzywej kapitalu. None = nie ma zadnego.

        Jedna liczba, o ktora do 2026-09-09 nie pytal NIKT - ani ten kernel,
        ani dozorca, ani wznowienie. Patrz komentarz przy POMIAR_STARY_PO_MIN.

        Czas bierzemy z BAZY, a nie z zegara procesu: punkt zapisuje inny
        proces, na innym podzie, i porownywanie go z lokalnym zegarem
        wprowadzaloby roznice zegarow do progu bezpieczenstwa.

        clock_timestamp(), a nie now(): `now()` to czas POCZATKU TRANSAKCJI,
        wiec w dlugiej transakcji pomiar wygladalby na SWIEZSZY, niz jest -
        czyli blad w jedyna strone, ktorej ten prog nie moze popelniac.
        """
        cur = self.conn.cursor()
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (clock_timestamp() - max(event_time)))"
            "     / 60 AS wiek_min"
            " FROM exec.equity_point WHERE engine=%s AND mode=%s",
            (self.engine, self.mode))
        wiek = (cur.fetchone() or {}).get("wiek_min")
        return float(wiek) if wiek is not None else None

    @property
    def limits(self) -> dict[str, dict]:
        """Wczytane limity - do wyliczenia budzetu na pozycje po stronie petli."""
        return self._limits

    # ------------------------------------------------------------- ocena
    def check_account(self) -> RiskVerdict:
        """Limity CALEGO KONTA, sprawdzane w kazdym cyklu.

        Wolane niezaleznie od tego, czy silnik chce w cokolwiek wejsc. Bez tego
        obsuniecie kapitalu podczas trzymania pozycji nie bylo przez nikogo
        oceniane i kill-switch nie mial szans zadzialac - patrz komentarz przy
        evaluate_account().

        EKSPOZYCJA POZA TA SCIEZKA (naprawa 2026-09-12)

        Do tego dnia stalo tu `self._open_exposure()`. Ta liczba NIE BYLA PRZEZ
        NIKOGO CZYTANA: `evaluate_account` ma cztery reguly - kill-switch,
        strate dzienna, prog kapitalu i obsuniecie - i zadna z nich nie siega
        ani do `state.exposure`, ani do `state.per_instrument`. Oba pola czyta
        wylacznie `evaluate_limits`, czyli ocena POJEDYNCZEGO ZLECENIA, ktorej
        ta metoda nie wola. Do `record_usage` tez nic z tego nie szlo, bo
        `evaluate_account` nie dopisuje pozycji `max_total_exposure` do `usage`.

        Kosztowalo to CALY BEZPIECZNIK. `_open_exposure` czyta
        `journal.planned_order` i `journal.decision`, a rola `trader_dozorca`
        nie ma USAGE na schemat `journal` - i to jest granica celowa
        (migracja 0034: nadzorca, ktory czyta uzasadnienia decyzji, zaczyna je
        oceniac). Dozorca wywracal sie wiec na KAZDYM cyklu, 900 razy pod rzad,
        zanim doszedl do `wznow()` i do wlasnego pulsu - a poniewaz petla lapie
        wyjatek i spi 60 s, na zewnatrz wygladalo to na `Running` 1/1 i ArgoCD
        `Healthy`. Zaobserwowane na produkcji 2026-09-11/12:
        `permission denied for schema journal`, zero domknietych cykli.

        DRUGIE PRZEJSCIE PRZEZ TE SAMA GRANICE, jeszcze nieujawnione: krok (1)
        `_open_exposure` wycenia pozycje po `market.candle`, a dozorca nie ma
        USAGE takze na `market`. Na produkcji nie bylo tego widac wylacznie
        dlatego, ze przy zatrzymanym handlu nie ma ani jednej otwartej pozycji,
        wiec `cena_odniesienia` nie jest w ogole wolana. Samo nadanie praw do
        `journal` przesunelo by awarie na `market` przy pierwszej pozycji.

        DLACZEGO NIE WASKI `GRANT`, TYLKO USUNIECIE ZAPYTANIA. Nadanie praw po
        to, zeby policzyc liczbe, ktorej nikt nie czyta, jest rozszczelnieniem
        granicy bez zadnej wartosci po drugiej stronie. Pytanie „czy pominiecie
        `journal` zanizy ekspozycje" nie powstaje, bo ekspozycja w tej sciezce
        nie jest do niczego uzywana. W `check()` - jedynej sciezce, ktora ja
        czyta - zapytania zostaja bez zmian; tamta chodzi na roli
        `trader_silnik`, ktora ma pelny dostep do `journal`.
        """
        equity, peak, day_ago = self._equity()
        nav, nav_szczyt, nav_doba, wplaty, nav_od_poczatku = self._wynik()
        state = RiskState(
            equity=equity, peak=peak, day_ago=day_ago,
            # EKSPOZYCJI TU NIE MIERZYMY - patrz `EKSPOZYCJA POZA TA SCIEZKA`
            # w docstringu wyzej. `None`, a nie `Decimal(0)`: stan bez pomiaru
            # ma sie nie nadawac do oceny zlecenia.
            exposure=None, per_instrument=None, orders_last_hour=0,
            killswitch=self.killswitch_active(),
            nav=nav, nav_peak=nav_szczyt, nav_day_ago=nav_doba,
            wplaty_netto=wplaty, nav_od_poczatku=nav_od_poczatku,
        )
        verdict = evaluate_account(state, self._limits)

        # SLAD POMIARU, ZANIM COKOLWIEK SIE WYDARZY.
        #
        # Zapis idzie PRZED wyzwoleniem kill-switcha celowo: wiersz opisujacy
        # pomiar, ktory doprowadzil do zatrzymania, ma istniec niezaleznie od
        # tego, czy sam zapis zatrzymania sie powiedzie. Odwrotna kolejnosc
        # zostawialaby zdarzenie bez liczby, ktora je uzasadnia.
        #
        # `usage` JEST PELNE PRZY KAZDYM WERDYKCIE, takze przy aktywnym
        # kill-switchu i przy naruszonym limicie (naprawa 2026-09-12). Do tego
        # dnia bylo odwrotnie: `evaluate_account` wracalo w punkcie 1 przed
        # pierwszym pomiarem, wiec zatrzymane konto nie zostawialo ANI JEDNEGO
        # wiersza - i przez szesc dob nie istnial zaden dowod, ze bezpiecznik
        # jeszcze liczy. Uzasadnienie i liczby z produkcji: `risk_rules.pomiar_konta`.
        #
        # Pusta lista znaczy wiec od teraz jedno: `risk.limit_def` nie ma ani
        # jednego limitu konta dla tej pary engine/mode. To stan do zgloszenia,
        # a nie spodziewana cisza - patrz `dozor.ocen_cisze_pomiaru`.
        self.record_usage(verdict.usage, co_najmniej_s=POMIAR_CO_S)

        if verdict.trigger_killswitch:
            self._trigger_killswitch(verdict.trigger_killswitch, verdict.trigger_limit)

        # WIEK POMIARU JEST WARUNKIEM HANDLU, nie ozdoba dziennika.
        #
        # Sprawdzamy DOPIERO po ocenie limitow i tylko wtedy, gdy ta wypadla
        # `allow`. Kolejnosc jest istotna z dwoch powodow. Po pierwsze, werdykt
        # niekorzystny jest zawsze wazniejszy: kill-switch wymaga recznego
        # zwolnienia, a wstrzymanie na starym pomiarze mija samo - podmiana
        # jednego na drugie zamienialaby zatrzymanie trwale w chwilowe. Po
        # drugie, stara krzywa nie moze WYWOLAC zatrzymania, ktorego nie bylo:
        # jest to ostatni prawdziwy pomiar, wiec limit, ktory z niej strzela,
        # strzela z powodu, ktory naprawde sie wydarzyl.
        #
        # DLACZEGO WSTRZYMANIE, A NIE KILL-SWITCH. Zamrozona krzywa wraca sama
        # razem z petla; kill-switch wymaga czlowieka, wiec zatrzymanie na stale
        # znaczyloby budzenie wlasciciela o trzeciej w nocy do awarii, ktora
        # minela. Ten sam wybor i z tego samego powodu zrobiono dla pozycji bez
        # swiezej ceny (patrz `powody_wstrzymania` w loop.py) - werdykt `block`
        # bez `trigger_killswitch` wpada tam w gotowa sciezke wstrzymania wejsc,
        # ktora przepuszcza WYJSCIA.
        if verdict.verdict == "allow":
            stary = ocen_wiek_pomiaru(self.wiek_pomiaru_min(),
                                      prog_wieku_pomiaru(self.engine))
            if stary:
                # `usage` zostaje z oceny limitow: pomiar jest stary, ale to
                # nadal jedyne wykorzystanie limitow, jakie znamy, a jego
                # skasowanie zrobiloby dziure w historii `risk.limit_usage`.
                return RiskVerdict("block", stary, usage=verdict.usage)
        return verdict

    def check(self, instrument_id: int, notional: Decimal,
              qty: Decimal) -> RiskVerdict:
        """Zbiera stan konta z bazy i oddaje ocene czystym regulom.

        Cala logika limitow siedzi w services.engine.risk_rules i jest wspolna
        z symulacja walk-forward. Ten modul odpowiada wylacznie za to, SKAD
        pochodzi stan - nie za to, jak sie go ocenia. Dzieki temu backtest nie
        moze mierzyc innego systemu niz ten, ktory naprawde handluje.
        """
        equity, peak, day_ago = self._equity()
        nav, nav_szczyt, nav_doba, wplaty, nav_od_poczatku = self._wynik()
        exposure, per_instrument = self._open_exposure()

        state = RiskState(
            equity=equity, peak=peak, day_ago=day_ago,
            exposure=exposure, per_instrument=per_instrument,
            orders_last_hour=self._orders_last_hour(),
            killswitch=self.killswitch_active(),
            nav=nav, nav_peak=nav_szczyt, nav_day_ago=nav_doba,
            wplaty_netto=wplaty, nav_od_poczatku=nav_od_poczatku,
        )
        rodzaj = self._rodzaj(instrument_id)
        if rodzaj is None:
            # NIE WIEMY, CZYM HANDLUJEMY - a to nie jest to samo co "handlujemy
            # spotem". Od rodzaju zalezy, czy limit dzwigni jest konieczny
            # (`evaluate_limits`, punkt 7), wiec zejscie na domysl bylo by
            # wylaczeniem tego zabezpieczenia przez BRAK DANEJ. Ta sama zasada
            # co przy `prog_min_equity`: limitu, ktorego nie da sie policzyc,
            # nie wolno zamienic po cichu w limit o innym znaczeniu.
            #
            # `block`, a nie wyjatek: to jeden instrument, nie caly silnik.
            return RiskVerdict(
                "block",
                f"Instrument {instrument_id} nie ma wiersza w ref.instrument — "
                "nie wiadomo, czy pozwala na pozycję krótką, więc nie wiadomo, "
                "czy limit dźwigni jest konieczny")
        verdict = evaluate_limits(state, self._limits, instrument_id, notional, qty,
                                  kind=rodzaj)
        if verdict.trigger_killswitch:
            self._trigger_killswitch(verdict.trigger_killswitch, verdict.trigger_limit)
        return verdict

    def _rodzaj(self, instrument_id: int) -> str | None:
        """`ref.instrument.kind` instrumentu. Z BAZY, nie od wolajacego.

        DLACZEGO KERNEL PYTA SAM, ZAMIAST PRZYJAC PARAMETR

        Od tej wartosci zalezy, czy limit dzwigni jest KONIECZNY
        (`risk_rules.evaluate_limits`, punkt 7). Gdyby przychodzila z zewnatrz,
        pominiecie jej w jednym wywolaniu - albo podanie `--kind` z wiersza
        polecen zamiast rodzaju instrumentu - wylaczaloby to zabezpieczenie
        CICHO, bo werdykt wygladalby wtedy dokladnie tak samo jak na spocie.
        W `loop.py` te dwie rzeczy juz zyja obok siebie pod mylaco podobnymi
        nazwami: `--kind` jest cennikiem rachunku papierowego, a rodzajem
        instrumentu jest `ocena["kind"]` z `ref.instrument`.

        `None` ZNACZY "WIERSZA NIE MA" i nie jest tym samym co "spot". Co z tym
        zrobic, rozstrzyga `check()` - bo odpowiedzia na niewiedze jest WERDYKT,
        a nie wyjatek. `instrument_id` pochodzi z zapytania o samo
        `ref.instrument`, wiec pusta odpowiedz znaczy, ze baza zmienila sie pod
        nami w trakcie cyklu: ma z tego wyjsc jeden instrument mniej, a nie
        przerwany cykl calego silnika.

        Bez pamieci podrecznej: cykl pyta raz na instrument (dziesiec zapytan
        po kluczu glownym na 300 s), a slownik trzymany przez zycie procesu
        przezylby zmiane `kind` w bazie i podawal ja nieaktualna dowolnie dlugo.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT kind FROM ref.instrument WHERE id = %s", (instrument_id,))
        r = cur.fetchone()
        return (r or {}).get("kind")

    def _trigger_killswitch(self, reason: str, limit_name: str | None = None) -> None:
        """Zapis zatrzymania. Tu nie ma zadnego wygasania ani licznika.

        Kill-switch nie zwalnia sie w tym miejscu i nie zwalnia sie sam z uplywu
        czasu. Robi to osobna usluga (services/engine/wznowienie.py) i WYLACZNIE
        na dowodzie, ze warunek ustal - z histereza, karencja i licznikiem
        wznowien. Limity z regula `reczne` nie sa przez nia ruszane wcale.

        `limit_name` jest zapisywana, bo bez niej regula wznowienia musialaby
        zgadywac z tekstu powodu, ktory jest zdaniem po polsku i zmienia sie
        razem z liczbami.
        """
        self.conn.cursor().execute(
            "INSERT INTO risk.killswitch_event (engine, mode, reason, limit_name)"
            " VALUES (%s,%s,%s,%s)",
            (self.engine, self.mode, reason, limit_name))

    def record_usage(self, usage: list[dict], *,
                     co_najmniej_s: int | None = None) -> int:
        """Zapisuje zmierzone zuzycie limitow. Zwraca liczbe zapisanych wierszy.

        `co_najmniej_s` wlacza RYTM: wiersz powstaje tylko wtedy, gdy dla tego
        limitu nie ma jeszcze pomiaru mlodszego niz tyle sekund. Uzywa go sciezka
        konta (`check_account`), bo ocenia to samo konto co cykl i z dwoch
        procesow naraz - patrz POMIAR_CO_S. Sciezka decyzyjna nie podaje rytmu:
        tam pomiar towarzyszy KONKRETNEJ decyzji i pominiecie go zerwaloby
        zwiazek miedzy zapisana decyzja a liczbami, na ktorych ja podjeto.

        WARUNEK TRZYMA BAZA, NIE PROCES. Licznik w pamieci procesu liczylby czas
        od jego wlasnego startu, wiec restart poda co minute (albo drugi proces
        oceniajacy to samo konto) omijalby rytm w calosci. `NOT EXISTS` widzi
        zapisy wszystkich procesow i przezywa restart.
        """
        cur = self.conn.cursor()
        zapisane = 0
        for u in usage:
            lim = self._limits.get(u["name"])
            if not lim:
                # CICHE POMINIECIE BYLO TU DZIURA W DOWODZIE.
                #
                # `usage` powstaje w risk_rules wylacznie dla limitow, ktore ten
                # sam slownik wlasnie zwrocil, wiec nietrafiona nazwa nie jest
                # stanem swiata - jest literowka albo zmiana nazwy limitu po
                # jednej stronie. `continue` zamienialo taka pomylke w brak
                # wierszy dla jednego limitu: tabela wygladalaby zdrowo, a mierzony
                # bylby o jeden limit mniej, niz sadzimy. Dokladnie tak wygladal
                # `min_equity`, zanim znalezlismy prawdziwa przyczyne.
                raise BrakLimitow(
                    f"pomiar limitu '{u['name']}' nie ma odpowiednika w risk.limit_def "
                    f"dla {self.engine}/{self.mode} - zuzycie tego limitu nie "
                    f"zostaloby nigdzie zapisane, a jego brak wygladalby jak spokoj")

            util = (u["used"] / u["threshold"]) if u["threshold"] else 0.0
            if co_najmniej_s is None:
                cur.execute(
                    "INSERT INTO risk.limit_usage (limit_def_id, event_time, used, threshold, utilization)"
                    " VALUES (%s, now(), %s, %s, %s) ON CONFLICT DO NOTHING",
                    (lim["id"], u["used"], u["threshold"], util))
            else:
                # clock_timestamp(), a nie now(): now() to czas POCZATKU
                # TRANSAKCJI, wiec dwa pomiary tego samego limitu w jednej
                # transakcji dostawalyby ten sam klucz glowny
                # (limit_def_id, event_time) i drugi z nich znikalby przez
                # ON CONFLICT DO NOTHING - po cichu, bo to nie jest blad.
                cur.execute(
                    "INSERT INTO risk.limit_usage (limit_def_id, event_time, used, threshold, utilization)"
                    " SELECT %s, clock_timestamp(), %s, %s, %s"
                    "  WHERE NOT EXISTS ("
                    "        SELECT 1 FROM risk.limit_usage"
                    "         WHERE limit_def_id = %s"
                    "           AND event_time > clock_timestamp() - make_interval(secs => %s))"
                    " ON CONFLICT DO NOTHING",
                    (lim["id"], u["used"], u["threshold"], util,
                     lim["id"], co_najmniej_s))

            # `rowcount` przez getattr, bo atrapy kursorow w testach go nie maja,
            # a liczba zapisow jest tu wygoda dla wolajacego, nie warunkiem
            # poprawnosci.
            n = getattr(cur, "rowcount", 0)
            zapisane += n if isinstance(n, int) and n > 0 else 0
        return zapisane

    def record_violations(self, violations: list[dict], decision_id: int | None) -> None:
        cur = self.conn.cursor()
        for v in violations:
            lim = self._limits.get(v["limit"])
            if not lim:
                continue
            cur.execute(
                "INSERT INTO risk.violation (limit_def_id, engine, decision_id, observed,"
                " threshold, action_taken, detail) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (lim["id"], self.engine, decision_id, v["observed"], v["threshold"],
                 v["action"], __import__("json").dumps(v)))
