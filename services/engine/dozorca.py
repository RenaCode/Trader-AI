"""Dozorca ryzyka — nadzorca, który nie dzieli losu z nadzorowanym.

DLACZEGO OSOBNY PROCES

`RiskKernel.check_account()` — jedyne miejsce, które ocenia limity całego konta
i uruchamia kill-switch — było wołane WYŁĄCZNIE w pętli decyzyjnej silnika.
Konsekwencja jest jednozdaniowa: gdy silnik przestaje działać, przestaje też
działać ocena ryzyka.

Nie chodzi o awarię, po której proces ginie — wtedy kontener wstaje i widać to
w `kubectl get pods`. Chodzi o proces, który ŻYJE I NIC NIE ROBI: zawieszony na
zapytaniu, w pętli po wyjątku, zablokowany na sieci. Pozycje zostają otwarte,
kapitał może obsuwać się dalej, a obsunięcia NIKT NIE LICZY. Czujka żywotności
to wykryje i wyśle alert — ale alert nie jest zatrzymaniem.

Kill-switch, który nie może zadziałać, gdy jego proces stoi, chroni dokładnie
w tych sytuacjach, w których nic mu nie zagraża.

To ten sam kształt co błąd naprawiony wcześniej: `risk.check()` wołane wyłącznie
przy próbie wejścia, więc obsunięcie podczas trzymania pozycji nie było oceniane
przez nikogo. Tamta poprawka przeniosła ocenę na każdy cykl i została o jeden
poziom za nisko — nadal w tym samym procesie.

CO TEN PROCES ROBI, A CZEGO NIE

Robi trzy rzeczy w pętli co `INTERWAL_S`:

    1. ocenia limity konta NIEZALEŻNIE od silnika i wyzwala kill-switch
    2. wznawia zatrzymania, których warunek ustał — na dowodzie, nie z upływu
       czasu (patrz wznowienie.py)
    3. bije własny heartbeat, żeby czujka żywotności miała czego pilnować

NIE zamyka pozycji. Zatrzymuje wejścia; wyjścia zostają w gestii silnika.
Awaryjne domknięcie z tego procesu wymagałoby kluczy giełdy w kolejnym
kontenerze, co poszerza powierzchnię ataku — to osobna decyzja, nie oczywista
wygrana.

NIE jest duplikatem oceny w silniku. Silnik pyta „czy wolno mi teraz wejść"
i musi znać odpowiedź w chwili decyzji. Dozorca pyta „czy konto jest w stanie,
który wymaga zatrzymania" — niezależnie od tego, czy silnik czegokolwiek chce.
Podwójne wyzwolenie jest niemożliwe: `evaluate_account` przy aktywnym
kill-switchu zwraca `block` bez żądania kolejnego.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg.rows import dict_row

from services.engine.risk import RiskKernel
from services.engine.wznowienie import wznow

# Pętla co minutę. CronJob dałby najmniej 10 minut (częściej zasypywałoby
# klaster podami), a limit straty dziennej 2% oceniany co 10 minut ma
# dziesięciominutowe okno ślepoty. Ten proces kosztuje jeden mały pod.
INTERWAL_S = int(os.environ.get("DOZORCA_INTERWAL_S", "60"))
NAZWA = "dozorca-ryzyka"


async def bij_heartbeat(execute, engine: str, mode: str, detal: dict) -> None:
    """Własny puls dozorcy — w OSOBNEJ tabeli, nie w risk.engine_heartbeat.

    Tamta ma `engine` typu `ref.engine` w kluczu głównym, więc dopisanie się do
    niej wymagałoby rozszerzenia enuma silników handlowych o coś, co silnikiem
    nie jest. Osobna tabela jest tu tańsza i uczciwsza.
    """
    await execute(
        "INSERT INTO risk.dozorca_heartbeat (nazwa, last_beat_at, detail)"
        " VALUES (%s, now(), %s)"
        " ON CONFLICT (nazwa) DO UPDATE SET last_beat_at = now(),"
        " detail = EXCLUDED.detail",
        (f"{NAZWA}:{engine}:{mode}", psycopg.types.json.Jsonb(detal)))


def ocen_konto(conn, engine: str, mode: str) -> tuple[str, str | None]:
    """Ocena limitów konta przez TEN SAM risk kernel, którego używa silnik.

    Druga implementacja reguł „na potrzeby dozorcy" byłaby gwarancją rozjazdu
    przy pierwszej zmianie limitu — to ten sam błąd, przed którym broni wspólny
    `risk_rules` między produkcją a symulacją.
    """
    risk = RiskKernel(conn, engine, mode)
    v = risk.check_account()
    conn.commit()
    return v.verdict, v.reason


async def petla(engine: str, mode: str, raz: bool = False) -> int:
    url = os.environ["DATABASE_URL"]
    from services.api.db import close_pool, execute, fetch_all, fetch_one, open_pool

    await open_pool()
    print(f"Dozorca ryzyka: {engine}/{mode}, co {INTERWAL_S} s")
    print("Ocenia limity konta niezależnie od silnika, wznawia na dowodzie, "
          "bije własny puls.")
    poprzedni: str | None = None
    try:
        with psycopg.connect(url, row_factory=dict_row) as conn:
            while True:
                started = datetime.now(timezone.utc)
                try:
                    werdykt, powod = ocen_konto(conn, engine, mode)
                    # Zmiana stanu jest zdarzeniem; stan niezmieniony nie jest.
                    # Wypisywanie „allow” co minutę zamieniłoby log w szum,
                    # w którym nie widać momentu zatrzymania.
                    if werdykt != poprzedni:
                        print(f"[{started:%H:%M:%S}] stan konta: {werdykt}"
                              + (f" — {powod}" if powod else ""))
                        poprzedni = werdykt

                    await wznow(fetch_all, fetch_one, execute,
                                __import__("services.api.notifications",
                                           fromlist=["notify"]).notify,
                                engine=engine, mode=mode)
                    await bij_heartbeat(execute, engine, mode,
                                        {"werdykt": werdykt, "powod": powod})
                except Exception as e:                        # noqa: BLE001
                    # Dozorca NIE MOŻE zginąć od pojedynczego błędu — to on jest
                    # ostatnią linią. Błąd jest wypisany i pętla idzie dalej;
                    # gdyby błąd był trwały, heartbeat przestanie się odświeżać
                    # i czujka żywotności zgłosi to jako martwego dozorcę.
                    print(f"[{started:%H:%M:%S}] cykl dozorcy przerwany: "
                          f"{type(e).__name__}: {e}")
                    conn.rollback()

                if raz:
                    return 0
                await asyncio.sleep(INTERWAL_S)
    finally:
        await close_pool()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--raz", action="store_true", help="jeden cykl zamiast pętli")
    args = ap.parse_args()
    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")
    return await petla(args.engine, args.mode, raz=args.raz)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
