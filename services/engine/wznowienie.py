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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine import stan_kapitalu  # noqa: E402

# Minimalny czas od zadzialania. Nie jest to warunek wznowienia - jest to
# warunek ROZWAZANIA wznowienia. Sam uplyw czasu niczego nie dowodzi.
KARENCJA_MIN = int(os.environ.get("WZNOWIENIE_KARENCJA_MIN", "60"))

# Ulamek limitu, ponizej ktorego miara musi wrocic, zeby wznowic. 0.5 znaczy:
# limit obsuniecia 10% -> wznowienie ponizej 5%.
HISTEREZA = float(os.environ.get("WZNOWIENIE_HISTEREZA", "0.5"))


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
                mode: str = "demo", sucho: bool = False) -> int:
    zdarzenia = await fetch_all(
        "SELECT k.id, k.reason, k.limit_name, k.triggered_at,"
        "       round(extract(epoch from (now() - k.triggered_at))/60) AS wiek_min"
        " FROM risk.killswitch_event k"
        " WHERE k.engine::text = %s AND k.mode::text = %s AND k.released_at IS NULL"
        " ORDER BY k.triggered_at", (engine, mode))
    if not zdarzenia:
        print("wznowienie: brak aktywnych kill-switchy")
        return 0

    stan = await stan_konta(fetch_one, engine, mode)
    if stan is None:
        print("wznowienie: brak danych o kapitale — nie ma na czym oprzeć decyzji")
        return 0

    zwolnione = 0
    for z in zdarzenia:
        opis = f"id={z['id']} {z['reason']}"
        nazwa = z["limit_name"]
        if not nazwa:
            print(f"  {opis}: bez nazwy limitu (sprzed 0029) — zostaje ręczne")
            continue

        lim = await fetch_one(
            "SELECT value, wznowienie, max_wznowien FROM risk.limit_def"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s AND enabled",
            (engine, mode, nazwa))
        if not lim:
            print(f"  {opis}: limit {nazwa} już nie istnieje — zostaje ręczne")
            continue
        if lim["wznowienie"] == "reczne":
            print(f"  {opis}: {nazwa} ma regułę ręczną — czeka na człowieka")
            continue
        if z["wiek_min"] < KARENCJA_MIN:
            print(f"  {opis}: karencja ({z['wiek_min']:.0f}/{KARENCJA_MIN} min)")
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
            print(f"  {opis}: {ile['n']} automatycznych wznowień w dobie przy "
                  f"limicie {lim['max_wznowien']} — dalej wyłącznie ręcznie")
            if not sucho:
                for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
                    await notify(
                        u["id"], "killswitch.triggered",
                        f"Kill-switch wraca w kółko ({engine})",
                        f"Limit {nazwa} zatrzymał silnik już {ile['n']} raz(y) w ciągu "
                        f"doby i za każdym razem warunek wznowienia był spełniony. "
                        f"To znaczy, że przyczyna trwa, a miara ją gubi. Automatyczne "
                        f"wznowienie zostało wstrzymane — zdarzenie czeka na decyzję.",
                        engine=engine, severity="critical",
                        data={"limit": nazwa, "wznowien_24h": ile["n"]})
            continue

        ustal, dowod = warunek_ustal(nazwa, float(lim["value"]), stan)
        if not ustal:
            print(f"  {opis}: warunek trwa ({dowod})")
            continue

        print(f"  {opis}: WZNAWIAM — {dowod}")
        zwolnione += 1
        if sucho:
            continue
        await execute(
            "UPDATE risk.killswitch_event SET released_at = now(),"
            " released_auto = true, release_note = %s"
            " WHERE id = %s AND released_at IS NULL",
            (f"Wznowienie automatyczne ({lim['wznowienie']}): {dowod}", z["id"]))
        for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
            await notify(
                u["id"], "killswitch.triggered",
                f"Kill-switch zwolniony automatycznie ({engine})",
                f"Zatrzymanie: {z['reason']}\n\nPodstawa wznowienia: {dowod}\n\n"
                f"Silnik wraca do handlu przy najbliższym cyklu. Zatrzymanie "
                f"i wznowienie są w zakładce Ryzyko.",
                engine=engine, severity="warning",
                data={"limit": nazwa, "dowod": dowod, "event_id": z["id"]})

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
