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

CO ZOSTAJE ZMIENIONE

Jedna wartość: próg kapitału podniesiony powyżej bieżącego stanu, żeby silnik
SAM wykrył przekroczenie. Przywracana w `finally`, więc wraca także po błędzie
i po przerwaniu — zostawiony podniesiony próg zatrzymałby silnik na stałe,
czyli spowodowałby dokładnie tę awarię, przed którą kill-switch ma chronić.

Próba używa progu kapitału, bo to jedyny z trzech limitów krytycznych z regułą
`reczne` — usługa wznowienia go nie ruszy i nie zwolni zdarzenia w połowie
pomiaru.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

LIMIT_PROBY = "min_equity"
# Ile czekać na cykl silnika (biegnie co 300 s) i na dozór (co 600 s).
CZEKAJ_SILNIK_S = int(os.environ.get("PROBA_CZEKAJ_SILNIK_S", "420"))
CZEKAJ_DOZOR_S = int(os.environ.get("PROBA_CZEKAJ_DOZOR_S", "660"))
KROK_S = 15


class Niezaliczona(RuntimeError):
    """Ogniwo łańcucha nie zadziałało. To awaria zabezpieczenia, nie próby."""


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
        "SELECT count(*) AS n FROM risk.killswitch_event"
        " WHERE engine::text=%s AND mode::text=%s AND released_at IS NULL",
        (engine, mode))
    if aktywne["n"]:
        # Nie odróżniłaby swojego zdarzenia od zastanego i zwolniłaby cudze.
        raise Niezaliczona(
            f"kill-switch jest już aktywny ({aktywne['n']}) — zwolnij go najpierw")

    kapital = float(stan["equity"])
    prog_przed = float(lim["value"])
    prog_proby = round(kapital + 500)
    print(f"=== PRÓBA POŻAROWA KILL-SWITCHA: {engine}/{mode} ===")
    print(f"kapitał {kapital:.2f}   próg przed próbą {prog_przed:.2f}   aktywnych 0")

    if sucho:
        print(f"Tryb suchy: podniósłbym {LIMIT_PROBY} na {prog_proby} i czekał "
              f"na cykl silnika. Nic nie zmieniam.")
        return 0

    try:
        print(f"\n--- podnoszę {LIMIT_PROBY} na {prog_proby} (powyżej kapitału) ---")
        await execute(
            "UPDATE risk.limit_def SET value = %s, updated_at = now()"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s",
            (prog_proby, engine, mode, LIMIT_PROBY))

        print(f"\n--- OGNIWO 1-2: czekam na cykl silnika (do {CZEKAJ_SILNIK_S}s) ---")

        async def zdarzenie():
            return await fetch_one(
                "SELECT id, reason, limit_name FROM risk.killswitch_event"
                " WHERE engine::text=%s AND mode::text=%s AND released_at IS NULL"
                " ORDER BY triggered_at DESC LIMIT 1", (engine, mode))

        z = await _czekaj_na(zdarzenie, CZEKAJ_SILNIK_S, "zdarzenie")
        if not z:
            raise Niezaliczona(
                "silnik nie uruchomił kill-switcha mimo kapitału poniżej progu")
        print(f"  zadziałał: id={z['id']} limit={z['limit_name']} — {z['reason']}")
        if z["limit_name"] != LIMIT_PROBY:
            raise Niezaliczona(
                f"zadziałał inny limit ({z['limit_name']}) — próba mierzy nie to, "
                f"co zakłada")

        print(f"\n--- OGNIWO 4: czy dozór zgłosił (do {CZEKAJ_DOZOR_S}s) ---")

        async def powiadomienie():
            r = await fetch_one(
                "SELECT count(*) AS n FROM notify.notification"
                " WHERE event_code = 'killswitch.triggered'"
                "   AND created_at > (SELECT triggered_at FROM risk.killswitch_event"
                "                     WHERE id = %s)", (z["id"],))
            return r["n"] or None

        n = await _czekaj_na(powiadomienie, CZEKAJ_DOZOR_S, "powiadomienie")
        if not n:
            # Cichy kill-switch jest gorszy niz jego brak, bo daje zludzenie
            # kontroli. To jest osobne niepowodzenie od braku zadzialania.
            raise Niezaliczona(
                "kill-switch zadziałał, a nikt się o tym nie dowiedział")
        print(f"  powiadomień: {n}")

        # ZWOLNIENIE PRZEZ PROBE NIE JEST WZNOWIENIEM AUTOMATYCZNYM.
        #
        # `released_auto` istnieje po to, zeby dalo sie odpowiedziec na jedno
        # pytanie: CZY SYSTEM WROCIL DO HANDLU SAM. Ustawianie jej przez probe
        # zlewa dwa rozne zdarzenia w jedno - a wtedy odpowiedz na to pytanie
        # wymaga czytania tresci notatki, czyli flaga przestaje cokolwiek
        # rozstrzygac. Przy dobowej probie takich wierszy przybywa jeden dziennie
        # i po miesiacu to one zdominowalyby statystyke wznowien automatycznych.
        #
        # Proba sprzata wiec po sobie jako ZWALNIAJACY NAZWANY Z IMIENIA, tak
        # samo jak zrobilby to czlowiek w interfejsie.
        await execute(
            "UPDATE risk.killswitch_event SET released_at = now(),"
            " released_auto = false, released_by = %s, release_note = %s"
            " WHERE id = %s",
            ("proba-pozarowa",
             "PRÓBA POŻAROWA — zdarzenie wywołane celowo przez "
             "services/engine/proba_killswitcha.py", z["id"]))
        print("\n=== PRÓBA ZALICZONA: wszystkie ogniwa zadziałały ===")
        return 0
    finally:
        # PRZYWRACANIE W `finally`, nie na końcu bloku: próg musi wrócić także
        # wtedy, gdy próba padnie w połowie albo zostanie przerwana.
        print(f"\n--- przywracanie {LIMIT_PROBY} na {prog_przed:.2f} ---")
        await execute(
            "UPDATE risk.limit_def SET value = %s, updated_at = now()"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s",
            (prog_przed, engine, mode, LIMIT_PROBY))
        po = await fetch_one(
            "SELECT value FROM risk.limit_def WHERE engine::text=%s AND mode::text=%s"
            "   AND name=%s", (engine, mode, LIMIT_PROBY))
        print(f"    próg po przywróceniu: {float(po['value']):.2f}")


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
        return await przeprowadz(fetch_all, fetch_one, execute,
                                 engine=args.engine, mode=args.mode,
                                 sucho=args.sucho)
    except Niezaliczona as e:
        print(f"\nNIEZALICZONA: {e}", file=sys.stderr)
        print("  To jest awaria samego zabezpieczenia, nie próby.", file=sys.stderr)
        return 1
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
