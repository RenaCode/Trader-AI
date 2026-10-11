"""Autonomiczne wznowienie po kill-switchu.

DLACZEGO TO JEST USLUGA, A NIE PRZYCISK

Obsluga tradingu ma byc autonomiczna. Kill-switch, ktorego zwolnienie wymaga
czlowieka przy klawiaturze, zatrzymuje system do rana - a limit dzienny
zwalniany recznie przestaje byc dzienny i staje sie dozywotni.

Jednoczesnie kill-switch, ktory wygasa sam po czasie, nie jest kill-switchem:
automatyczne wznowienie po obsunieciu to najprostsza droga do powtorzenia tej
samej straty.

Wyjscie z tej sprzecznosci nie polega na wyborze jednej ze stron, tylko na
zauwazeniu, ze TRZY LIMITY KRYTYCZNE ZNACZA TRZY ROZNE RZECZY (patrz migracja
0029). Wznowienie idzie WYLACZNIE NA DOWODZIE, ze warunek ustal - nigdy na
uplywie czasu.

TRZY ZABEZPIECZENIA, KTORE ODROZNIAJA TO OD PO PROSTU POLUZOWANIA LIMITU

1. HISTEREZA. Wznowienie przy progu ciasniejszym niz zadzialanie - polowa
   limitu. System nie moze oscylowac wokol granicy, wchodzac i wychodzac
   przy kazdym drgnieciu kapitalu.

2. KARENCJA. Minimalny czas od zadzialania. Chroni przed wznowieniem w tej
   samej minucie, gdy chwilowa wycena wrocila, a przyczyna nie.

3. LICZNIK. Po `max_wznowien` w ciagu doby limit przechodzi w tryb reczny sam
   z siebie. To jest najwazniejsze z trzech: system wraca do handlu, ale NIE
   W KOLKO. Powtarzajace sie zatrzymania znacza, ze warunek wznowienia jest
   spelniany, a problem trwa - i wtedy czlowiek jest naprawde potrzebny.

Kazde automatyczne zwolnienie jest zapisane z `released_auto = true` i zglaszane
powiadomieniem. System, ktory sam siebie zatrzymuje i sam wznawia w ciszy, jest
nieodrozninalny od systemu, ktory nie robi ani jednego, ani drugiego.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine import stan_kapitalu  # noqa: E402

# `limit_name` recznego zatrzymania z panelu - ta sama wartosc co
# `services/api/routers/risk.py:LIMIT_RECZNY` (silnik nie importuje routerow).
#
# RECZNE MA PIERWSZENSTWO (audyt 2026-10-09, W-1). Dopoki aktywne jest reczne
# zatrzymanie, ta funkcja nie zwalnia NICZEGO - takze automatu, ktorego warunek
# juz ustal. Zwolnienie samego automatu przy trzymajacym recznym nie wznowiloby
# handlu (silnik stoi przy kazdym aktywnym wierszu), ale wyslaloby krytyczne
# "Kill-switch zwolniony AUTOMATYCZNIE - silnik wraca do handlu", czyli
# nieprawde, a po recznym zwolnieniu wlasciciel nie mialby juz czego ocenic.
LIMIT_RECZNY = "reczny"

# Minimalny czas od zadzialania. Nie jest to warunek wznowienia - jest to
# warunek ROZWAZANIA wznowienia. Sam uplyw czasu niczego nie dowodzi.
KARENCJA_MIN = int(os.environ.get("WZNOWIENIE_KARENCJA_MIN", "60"))

# Ulamek limitu, ponizej ktorego miara musi wrocic, zeby wznowic. 0.5 znaczy:
# limit obsuniecia 10% -> wznowienie ponizej 5%.
HISTEREZA = float(os.environ.get("WZNOWIENIE_HISTEREZA", "0.5"))

# Co ile powtarzac w logu opis stanu, ktory sie NIE ZMIENIL.
#
# DLACZEGO TO W OGOLE ISTNIEJE - stan produkcji 2026-09-12
#
# `wznow()` wola dozorca co 60 s (`dozorca.INTERWAL_S`), a kazda galaz `continue`
# nizej wypisywala swoja linie bezwarunkowo. Kill-switch z reguly recznej trwa,
# dopoki nie dotknie go czlowiek - na produkcji 75 godzin - wiec `kubectl logs
# deploy/trader-dozorca` oddawalo 615 linii, z czego 615 identycznych:
#
#     id=2 Kapital 100063.09 spadl do progu 100563.00: min_equity ma regule
#     reczna - czeka na czlowieka
#
# To jest ta sama wada, ktora ten projekt tropi w kanale powiadomien i w kodzie
# wyjscia czujek: SYGNAL, KTORY PRZYCHODZI ZAWSZE, PRZESTAJE COKOLWIEK ZNACZYC.
# Log zalany jedna linia nie jest logiem bogatszym, tylko logiem, w ktorym nie
# widac niczego innego - w tym wyjatku z cyklu dozorcy albo chwili, w ktorej
# stan sie zmienil. Kosztuje tez retencje: 1440 linii na dobe wypycha z bufora
# poda wszystko starsze.
#
# CZEGO TA STALA NIE ROBI: nie ucisza stanu. Zmiana stanu idzie do logu
# NATYCHMIAST, a trwanie jest przypominane w tym rytmie razem z liczba
# pominietych przebiegow - patrz `Dziennik.stan`. Cisza calkowita byla by tu
# bledem gorszym niz zalew, bo stan trwale zly jest nieodrozialny od spokoju.
#
# Godzina, a nie doba: to jest log, a nie powiadomienie. Czytany bywa wtedy,
# gdy ktos juz patrzy, wiec ma odpowiadac na pytanie "co sie dzieje TERAZ"
# bez czekania. Sesja diagnostyczna rzadko trwa dluzej niz godzine.
PRZYPOMNIENIE_LOGU_S = int(os.environ.get("WZNOWIENIE_PRZYPOMNIENIE_S", "3600"))


class Dziennik:
    """Log stanu bez zalewania: ZMIANA mowi od razu, TRWANIE przypomina w rytmie.

    Pamiec jest w PROCESIE, nie w bazie, i to jest wybor, nie skrot. Pytanie
    brzmi "czy ten log juz to powiedzial", a log jest wlasnoscia procesu:
    po restarcie poda bufor zaczyna sie od nowa i pierwszy opis stanu MUSI
    sie w nim znalezc, bo inaczej `kubectl logs` po restarcie nie odpowiada na
    pytanie o stan wcale. Skutkiem ubocznym jest to, ze uruchomienie z linii
    polecen (`python wznowienie.py`) wypisuje komplet - czyli dokladnie to,
    czego sie po nim oczekuje.

    `znacznik` jest TOZSAMOSCIA stanu, a `tresc` jego opisem. Rozdzielone, bo
    opis niesie liczby, ktore drgaja co cykl (wiek zdarzenia, kapital), i
    porownywanie po samym tekscie kazaloby wypisac wszystko od nowa przy kazdej
    zmianie ostatniej cyfry - czyli nie tlumiloby niczego.
    """

    def __init__(self, przypomnienie_s: int | None = None,
                 zegar=time.monotonic) -> None:
        self._odstep = (PRZYPOMNIENIE_LOGU_S if przypomnienie_s is None
                        else przypomnienie_s)
        self._zegar = zegar
        self._stan: dict[str, dict] = {}

    def stan(self, klucz: str, znacznik: str, tresc: str) -> bool:
        """Wypisz opis stanu, jesli sie zmienil albo minal rytm. Zwraca czy poszlo."""
        teraz = self._zegar()
        poprzedni = self._stan.get(klucz)

        if poprzedni is None or poprzedni["znacznik"] != znacznik:
            self._stan[klucz] = {"znacznik": znacznik, "od": teraz,
                                 "ostatni": teraz, "pominiete": 0}
            print(tresc)
            return True

        if teraz - poprzedni["ostatni"] < self._odstep:
            poprzedni["pominiete"] += 1
            return False

        # PRZYPOMNIENIE MA SIE PRZEDSTAWIC i powiedziec, czego nie widac
        # w logu. Bez tych dwoch liczb linia sprzed godziny jest nieodrozialna
        # od linii sprzed sekundy, a to wlasnie ta roznica jest tu tresci a.
        pominiete = poprzedni["pominiete"]
        trwa_min = (teraz - poprzedni["od"]) / 60
        poprzedni.update(ostatni=teraz, pominiete=0)
        print(f"{tresc}\n    (stan TRWA — nieprzerwanie od {trwa_min:.0f} min "
              f"w tym procesie, {pominiete} identycznych przebiegów pominięto "
              f"w logu; przypomnienie co {self._odstep // 60} min)")
        return True

    def zdarzenie(self, klucz: str, tresc: str) -> None:
        """Wypisz BEZWARUNKOWO i skasuj pamiec stanu pod tym kluczem.

        Wznowienie i wstrzymanie automatu to ZDARZENIA - zachodza raz i nie maja
        czego tlumic. Skasowanie pamieci jest tu istotne: stan, ktory wrocil po
        zdarzeniu, ma sie wypisac od nowa, a nie wpasc w rytm sprzed zdarzenia.
        """
        self._stan.pop(klucz, None)
        print(tresc)


# Jeden dziennik na proces. Dozorca wola `wznow()` w petli i ma dzielic pamiec
# miedzy przebiegami; testy i kazdy inny wolajacy moga podac wlasny.
DZIENNIK = Dziennik()


async def stan_konta(fetch_one, engine: str, mode: str) -> dict | None:
    """Wynik, szczyt i wynik sprzed doby - TE SAME TRZY LICZBY CO RISK KERNEL.

    "Te same" znaczy tu: z tych samych zapytan i po tej samej regule wyboru
    miary, a nie "policzone tak, zeby wychodzilo podobnie". Poprzednia wersja
    czytala wylacznie `exec.equity_point.equity`, podczas gdy `evaluate_account`
    od migracji 0031 liczy strate dzienna i obsuniecie z `nav`. Uzasadnienie
    i scenariusz w liczbach - w naglowku `stan_kapitalu`.

    KLUCZE NAZYWAJA WIELKOSC, NIE KOLUMNE. Poprzednie `equity`/`peak`/`day_ago`
    obiecywaly pieniadze; po wejsciu `nav` te same klucze niosly wartosc
    jednostki krazaca wokol 1,0. Nazwa, ktora klamie o jednostce, jest tym
    samym rodzajem bledu co druga kopia zapytania.
    """
    wiersze = {}
    for klucz, sql in stan_kapitalu.PLAN:
        wiersze[klucz] = await fetch_one(sql, (engine, mode))
    return stan_kapitalu.zloz_stan(wiersze)


def warunek_ustal(limit_name: str, wartosc_limitu: float, stan: dict) -> tuple[bool, str]:
    """Czy przyczyna zatrzymania przestala obowiazywac - z histereza.

    Zwraca (czy_ustal, opis liczbowy). Opis idzie do notatki zwolnienia, zeby
    w dzienniku zostalo, NA JAKIEJ PODSTAWIE system wrocil do handlu, a nie samo
    "wznowiono automatycznie".
    """
    prog = wartosc_limitu * HISTEREZA
    # Jednostka idzie do opisu, bo dwa procenty policzone z dwoch roznych
    # krzywych czytaja sie w dzienniku identycznie - a to wlasnie ta
    # nieodroznialnosc pozwalala zdejmowac hamulec dzien za wczesnie.
    skad = "jednostka" if stan.get("jednostka") else "kapitał"
    if limit_name == "max_daily_loss":
        if stan["doba"] <= 0:
            return False, "brak odniesienia sprzed doby"
        strata = (stan["doba"] - stan["wynik"]) / stan["doba"] * 100
        return strata < prog, f"strata dzienna {strata:.2f}% < {prog:.2f}% ({skad})"
    if limit_name == "max_drawdown":
        if stan["szczyt"] <= 0:
            return False, "brak szczytu"
        dd = (stan["szczyt"] - stan["wynik"]) / stan["szczyt"] * 100
        return dd < prog, f"obsunięcie {dd:.2f}% < {prog:.2f}% ({skad})"
    # Limit nieznany tej regule NIE jest wznawiany. Domysl jest tu celowo
    # zachowawczy: nowy limit krytyczny, o ktorym ta funkcja nie wie, zatrzyma
    # system do czasu, az ktos swiadomie dopisze jego regule.
    return False, f"brak reguły ustania dla {limit_name}"


async def wznow(fetch_all, fetch_one, execute, notify, engine: str = "crypto",
                mode: str = "demo", sucho: bool = False,
                dziennik: Dziennik | None = None) -> int:
    """Przejrzyj aktywne zatrzymania i zwolnij te, ktorych warunek ustal.

    WYPISYWANIE IDZIE PRZEZ `Dziennik`, A NIE PRZEZ GOLE `print`. Ta funkcja
    chodzi w petli co 60 s i opisuje STANY, ktore z natury trwaja godzinami -
    uzasadnienie i zmierzony koszt przy `PRZYPOMNIENIE_LOGU_S`. Zdarzenia
    (wznowienie, wstrzymanie automatu) ida bezwarunkowo przez `zdarzenie`.
    """
    dz = DZIENNIK if dziennik is None else dziennik
    zdarzenia = await fetch_all(
        "SELECT k.id, k.reason, k.limit_name, k.triggered_at,"
        "       round(extract(epoch from (now() - k.triggered_at))/60) AS wiek_min"
        " FROM risk.killswitch_event k"
        " WHERE k.engine::text = %s AND k.mode::text = %s AND k.released_at IS NULL"
        " ORDER BY k.triggered_at", (engine, mode))
    if not zdarzenia:
        dz.stan("wznowienie", "brak", "wznowienie: brak aktywnych kill-switchy")
        return 0

    reczne = [z for z in zdarzenia if z["limit_name"] == LIMIT_RECZNY]
    if reczne:
        ids = ",".join(str(z["id"]) for z in reczne)
        dz.stan("wznowienie", f"reczne:{ids}",
                f"wznowienie: aktywne RĘCZNE zatrzymanie (id={ids}) — automat nie "
                f"zwalnia żadnego z {len(zdarzenia)} zdarzeń do jego zwolnienia")
        return 0

    stan = await stan_konta(fetch_one, engine, mode)
    if stan is None:
        dz.stan("wznowienie", "bez-kapitalu",
                "wznowienie: brak danych o kapitale — nie ma na czym oprzeć decyzji")
        return 0
    dz.stan("wznowienie", "aktywne",
            f"wznowienie: {len(zdarzenia)} aktywnych zatrzymań do przejrzenia")

    zwolnione = 0
    for z in zdarzenia:
        # WIEK ZDARZENIA W KAZDEJ LINII. Stan opisany bez niego czyta sie tak
        # samo po minucie i po trzech dobach, a to jest jedyna liczba, ktora
        # odroznia "wlasnie sie stalo" od "stoi i nikt tego nie tknal".
        opis = f"id={z['id']} ({z['wiek_min']:.0f} min) {z['reason']}"
        klucz = f"zdarzenie:{z['id']}"
        nazwa = z["limit_name"]
        if not nazwa:
            dz.stan(klucz, "bez-nazwy",
                    f"  {opis}: bez nazwy limitu (sprzed 0029) — zostaje ręczne")
            continue

        lim = await fetch_one(
            "SELECT value, wznowienie, max_wznowien FROM risk.limit_def"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s AND enabled",
            (engine, mode, nazwa))
        if not lim:
            dz.stan(klucz, f"brak-limitu:{nazwa}",
                    f"  {opis}: limit {nazwa} już nie istnieje — zostaje ręczne")
            continue
        if lim["wznowienie"] == "reczne":
            dz.stan(klucz, f"reczne:{nazwa}",
                    f"  {opis}: {nazwa} ma regułę ręczną — czeka na człowieka")
            continue
        if z["wiek_min"] < KARENCJA_MIN:
            dz.stan(klucz, "karencja",
                    f"  {opis}: karencja ({z['wiek_min']:.0f}/{KARENCJA_MIN} min)")
            continue

        # LICZNIK PRZED WARUNKIEM. Gdyby szedl po nim, system przy trwajacym
        # problemie wznawialby i zatrzymywal sie w kolko, a licznik tylko
        # opisywalby te petle zamiast ja przerwac.
        ile = await fetch_one(
            "SELECT count(*) AS n FROM risk.killswitch_event"
            " WHERE engine::text=%s AND mode::text=%s AND limit_name=%s"
            "   AND released_auto AND released_at > now() - interval '24 hours'",
            (engine, mode, nazwa))
        if (ile or {}).get("n", 0) >= lim["max_wznowien"]:
            # STAN, nie zdarzenie: licznik dobowy opada sam i dopoki nie opadnie,
            # ta linia powtarzalaby sie co cykl.
            #
            # POWIADOMIENIE RAZ NA ZDARZENIE (przeglad 2026-10-04, T-3). Do tej
            # poprawki komentarz mowil, ze `notify()` to wytlumi - ale
            # `killswitch.triggered` jest `always_on`, a `always_on` omija
            # cooldown. Wynik: alarm krytyczny co 60 s (rytm dozorcy) przez
            # cala dobe, do ~1440 wierszy w dzwonku. Znacznik to samo
            # powiadomienie: `data.event_id` + `data.rodzaj = 'licznik'`.
            dz.stan(klucz, f"licznik:{ile['n']}",
                    f"  {opis}: {ile['n']} automatycznych wznowień w dobie przy "
                    f"limicie {lim['max_wznowien']} — dalej wyłącznie ręcznie")
            juz = None if sucho else await fetch_one(
                "SELECT 1 AS jest FROM notify.notification"
                " WHERE event_code = 'killswitch.triggered'"
                "   AND data->>'event_id' = %s AND data->>'rodzaj' = 'licznik'"
                " LIMIT 1", (str(z["id"]),))
            if not sucho and not juz:
                for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
                    await notify(
                        u["id"], "killswitch.triggered",
                        f"Kill-switch wraca w kółko ({engine})",
                        f"Limit {nazwa} zatrzymał silnik już {ile['n']} raz(y) w ciągu "
                        f"doby i za każdym razem warunek wznowienia był spełniony. "
                        f"To znaczy, że przyczyna trwa, a miara ją gubi. Automatyczne "
                        f"wznowienie zostało wstrzymane — zdarzenie czeka na decyzję.",
                        engine=engine, severity="critical",
                        data={"limit": nazwa, "wznowien_24h": ile["n"],
                              "event_id": z["id"], "rodzaj": "licznik"})
            continue

        ustal, dowod = warunek_ustal(nazwa, float(lim["value"]), stan)
        if not ustal:
            # ZNACZNIK BEZ LICZB Z `dowod`. Miara drga przy kazdym odczycie
            # krzywej, wiec znacznik z niej zbudowany zmienialby sie co cykl
            # i nie stlumilby ani jednej linii. Stanem jest "warunek trwa",
            # a nie "warunek trwa przy 4,31%"; liczba idzie do tresci.
            dz.stan(klucz, f"trwa:{nazwa}", f"  {opis}: warunek trwa ({dowod})")
            continue

        if sucho:
            dz.zdarzenie(klucz, f"  {opis}: WZNAWIAM — {dowod}")
            zwolnione += 1
            continue
        # RECZNE ZATRZYMANIE SPRAWDZANE PONOWNIE W TYM SAMYM UPDATE (audyt
        # 2026-10-10, N2-1). Lista zdarzen jest czytana raz, na poczatku
        # przebiegu; klikniecie "Zatrzymaj handel" miedzy tym odczytem a zapisem
        # zwalnialo automat i wysylalo krytyczne "Silnik wraca do handlu", choc
        # silnik stal na recznym. `fetch_one` z RETURNING, a nie `execute`:
        # brak wiersza znaczy, ze nie zwolniono NIC i alarmu byc nie moze.
        zapis = await fetch_one(
            "UPDATE risk.killswitch_event k SET released_at = now(),"
            " released_auto = true, release_note = %s"
            " WHERE k.id = %s AND k.released_at IS NULL"
            "   AND NOT EXISTS (SELECT 1 FROM risk.killswitch_event r"
            "                    WHERE r.engine = k.engine AND r.mode = k.mode"
            "                      AND r.limit_name = %s AND r.released_at IS NULL)"
            " RETURNING k.id",
            (f"Wznowienie automatyczne ({lim['wznowienie']}): {dowod}", z["id"],
             LIMIT_RECZNY))
        if not zapis:
            dz.zdarzenie(klucz, f"  {opis}: NIE wznawiam — w trakcie przebiegu "
                                f"pojawiło się ręczne zatrzymanie albo zdarzenie "
                                f"zostało już zwolnione")
            continue
        dz.zdarzenie(klucz, f"  {opis}: WZNAWIAM — {dowod}")
        zwolnione += 1
        # `killswitch.released` / `critical` (T-4, T-6): handel wraca bez
        # czlowieka, wiec wiadomosc idzie przez bramke na telefon. Jako
        # `warning` lezala tylko w dzwonku, a wlasciciel po alarmie
        # o zatrzymaniu uwazal, ze handel stoi.
        wznowien = int((ile or {}).get("n", 0)) + 1
        for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
            await notify(
                u["id"], "killswitch.released",
                f"Kill-switch zwolniony AUTOMATYCZNIE ({engine}) — {nazwa}",
                f"Zatrzymanie: {z['reason']}\n\nPodstawa wznowienia: {dowod}\n\n"
                f"To {wznowien}. automatyczne wznowienie tego limitu w ciągu doby "
                f"(najwyżej {lim['max_wznowien']}).\n\n"
                f"Silnik wraca do handlu przy najbliższym cyklu. Zatrzymanie "
                f"i wznowienie są w zakładce Ryzyko.",
                engine=engine, severity="critical",
                data={"limit": nazwa, "dowod": dowod, "event_id": z["id"],
                      "reczny": False, "wznowien_24h": wznowien})

    if sucho and zwolnione:
        print("Tryb suchy — nic nie zwolniono.")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--sucho", action="store_true",
                    help="wypisz decyzje, nie zwalniaj i nie powiadamiaj")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, execute, fetch_all, fetch_one, open_pool
    from services.api.notifications import notify

    await open_pool()
    try:
        return await wznow(fetch_all, fetch_one, execute, notify,
                           engine=args.engine, mode=args.mode, sucho=args.sucho)
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
