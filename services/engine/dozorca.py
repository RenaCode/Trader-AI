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

ZNANE OGRANICZENIE: OSOBNY PROCES TO JESZCZE NIE OSOBNY POMIAR

Ten proces jest niezależny od silnika jako PROCES, ale nie jest od niego
niezależny jako POMIAR. Krzywą `exec.equity_point` zapisuje wyłącznie
`loop.update_equity` — czyli pętla, którą dozorca ma nadzorować. Zawieszona
pętla (pod żyje, cykl nie chodzi) zamraża krzywą, a dozorca oceniałby tę samą
zamrożoną liczbę co minutę i meldował `allow` — z bijącym własnym pulsem.
Byłby to ten sam kształt błędu, przed którym broni docstring wyżej, przesunięty
o jeden poziom niżej: nadzorca dzielący z nadzorowanym nie proces, tylko źródło
liczby.

Od 2026-09-09 ten układ nie jest już cichy: wiek pomiaru jest warunkiem handlu
(`risk.POMIAR_STARY_PO_MIN`), więc stara krzywa daje `block` zamiast `allow`,
a sam wiek idzie do heartbeatu i widać go z zewnątrz bez zaglądania do bazy.
Wykrycie to jednak nie jest pomiar: docelowo dozorca powinien liczyć kapitał
SAM z `exec.position` i `market.candle`, zamiast czytać liczbę nadzorowanego.
Dopóki tego nie robi, jest niezależny tylko z nazwy — a to osobna, duża zmiana
(wspólna wycena, uzgodnienie z pozycjami, własne prawa do tabel), nie poprawka
przy okazji.
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
# klaster podami) i tyle samo wynosiłoby wtedy opóźnienie reakcji. Ten proces
# kosztuje jeden mały pod.
#
# TO NIE JEST ROZDZIELCZOŚĆ POMIARU, TYLKO CZĘSTOTLIWOŚĆ PYTANIA. Wcześniej
# stało tu, że limit straty dziennej oceniany co 10 minut ma dziesięciominutowe
# okno ślepoty — co sugerowało, że pętla co 60 s daje okno jednominutowe. Nie
# daje. Dozorca czyta `exec.equity_point`, a ten punkt powstaje raz na cykl
# pętli decyzyjnej, czyli co 300 s (zmierzone na produkcji 2026-09-09: 3713
# odstępów, mediana 300 s, p99 360 s). Cztery na pięć przebiegów ocenia więc
# TĘ SAMĄ liczbę co poprzedni — okno ślepoty na zmianę
# kapitału ma pięć minut, nie minutę, i skrócić je może wyłącznie częstszy
# zapis krzywej albo własny pomiar (patrz „ZNANE OGRANICZENIE" wyżej).
#
# Minutowa pętla zarabia mimo to na siebie w trzech rzeczach, które NIE zależą
# od krzywej: reakcja na świeży punkt następuje w ciągu minuty od jego zapisu,
# wznowienia i wiek pomiaru sprawdzane są co minutę, a puls dozorcy ma
# rozdzielczość, której wymaga próg żywotności 180 s.
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


def ocen_konto(conn, engine: str, mode: str) -> tuple[str, str | None, float | None]:
    """Ocena limitów konta przez TEN SAM risk kernel, którego używa silnik.

    Druga implementacja reguł „na potrzeby dozorcy" byłaby gwarancją rozjazdu
    przy pierwszej zmianie limitu — to ten sam błąd, przed którym broni wspólny
    `risk_rules` między produkcją a symulacją. Dlatego wiek pomiaru też nie jest
    tu liczony osobno: `check_account()` sam odmawia zgody na starej krzywej,
    a dozorca dokłada do tego jedno — WYPOWIADA TĘ LICZBĘ NA ZEWNĄTRZ.

    Trzecia wartość to wiek najświeższego punktu krzywej w minutach (None =
    nie ma żadnego). Idzie do heartbeatu ZAWSZE, także przy werdykcie `allow`,
    bo pytanie „ile lat ma ta liczba" musi mieć odpowiedź, zanim odpowiedź
    zacznie być alarmująca. Werdykt mówi „wolno / nie wolno"; ten wiek mówi,
    na ile świeżym pomiarze ten werdykt się opiera — a to dwie różne rzeczy
    i przez trzynaście dni produkcji nikt nie pytał o tę drugą.
    """
    risk = RiskKernel(conn, engine, mode)
    v = risk.check_account()
    wiek = risk.wiek_pomiaru_min()
    conn.commit()
    return v.verdict, v.reason, wiek


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
                    werdykt, powod, wiek = ocen_konto(conn, engine, mode)
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
                    # WIEK POMIARU W PULSIE, ZAWSZE — także przy `allow`.
                    #
                    # Puls dozorcy odpowiadał do tej pory wyłącznie na pytanie
                    # „czy dozorca żyje". Zamrożona krzywa kapitału nie rusza
                    # ani tej odpowiedzi, ani werdyktu — więc jedyny ślad
                    # pięciodniowej ciszy w pomiarze był w kolumnie
                    # `event_time`, o którą nikt nie pytał. Tu ta liczba staje
                    # się widoczna z zewnątrz: `null` znaczy „krzywej nie ma
                    # w ogóle", a nie „wszystko dobrze".
                    await bij_heartbeat(execute, engine, mode,
                                        {"werdykt": werdykt, "powod": powod,
                                         "wiek_pomiaru_min": (
                                             round(wiek, 1) if wiek is not None
                                             else None)})
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
