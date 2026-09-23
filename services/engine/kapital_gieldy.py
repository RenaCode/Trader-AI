"""Krzywa kapitalu silnika Z KSIAG GIELDY - dla silnika, ktory nie liczy jej sam.

DLACZEGO TO ISTNIEJE

`exec.equity_point` dla krypto pisze petla silnika z wlasnych wypelnien
i wyceny (`loop.py`). Silnik equity jeszcze nie decyduje, wiec nie pisze
niczego - a wykres "Kapital laczny obu platform" mial przez to jedna linie.
Konto paper Alpaki jest natomiast PRAWDZIWYM rachunkiem z prawdziwa wycena:
equity, gotowka i wartosc pozycji sa tam liczone przez gielde, nie przez nas.
Ten proces przepisuje je do tej samej tabeli, ta sama miara co krypto
(jednostka uczestnictwa z migracji 0031), wiec obie linie znacza to samo.

CZEGO NIE ROBI: nie decyduje, nie sklada zlecen, nie dotyka pozycji. Odczyt
i jeden INSERT na cykl, `ON CONFLICT DO NOTHING` na siatce minutowej.

JEDNOSTKI. Bez przeplywow w `exec.cash_flow` przyjmujemy start 100 000 jako
wplate (tak samo jak `loop.seed_equity` dla demo) - konto paper Alpaki startuje
z tej kwoty. To jest ZGADYWANIE i jest tak traktowane: saldo startowe konta
paper ustawia sie przy zakladaniu i zmienia przy kazdym resecie konta
(`/v2/account/configurations`), niezaleznie od tego repozytorium, wiec
`zapisz_punkt` sprawdza zgadnieta wplate wobec ksiag gieldy i odmawia zapisu,
gdy sie nie trzyma (patrz ZGADNIETA_ODCHYLKA_MAX). `realized_cum` = equity -
wplaty - unrealized, czyli to, co wynika z ksiag gieldy, a nie z naszego
dziennika; `unrealized` = suma `unrealized_pl` z pozycji.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from services.engine.loop import START_EQUITY, przeplywy  # noqa: E402
from services.exchanges.fabryka import adapter_danych  # noqa: E402
from services.ingest import puls  # noqa: E402

POD_RZAD_MAX = 5
PULS = "kapital"

# Jak daleko zgadnieta wplata moze sie rozjechac z ksiegami gieldy, zanim
# przestaniemy jej wierzyc - jako ulamek samej zgadnietej wplaty.
#
# DLACZEGO TO ISTNIEJE. Gdy `exec.cash_flow` jest puste, bierzemy START_EQUITY
# = 100 000, czyli kapital startowy SYMULACJI krypto, i twierdzimy, ze tyle
# samo ma konto paper Alpaki. Nic tego nie weryfikowalo, mimo ze `stan_konta()`
# zwraca komplet z ksiag gieldy.
#
# Scenariusz, ktory to wypuszczalo bez slowa: ktos resetuje konto paper na
# 50 000 USD (typowe przy czyszczeniu po testach egzekucji). Od nastepnego
# cyklu realized = 50 000 - 100 000 - unrealized, czyli okolo -50 000, i kafelek
# "Zrealizowany P&L" pokazuje pol rachunku straty, ktorej nikt nie poniosl.
# Odwrotny reset (200 000) pokazuje 100 000 fikcyjnego zysku. Zadnego bledu
# w logu - cykl konczyl sie powodzeniem i bil puls. Ten sam wiersz zapisuje
# `wplaty_netto`, ktore jest odniesieniem dla progu kapitalu wyrazonego
# ulamkiem (`RiskState.wplaty_netto`), wiec podloga kapitalu liczylaby sie
# od liczby zmyslonej.
#
# 20%, bo produkcja i tak nie dojedzie do takiego rozjazdu przy zdrowej
# ksiegowosci: kill-switch stoi na `min_equity` = 95 000, czyli 5% ponizej
# zgadnietej wplaty. Rozjazd tej skali znaczy "zgadujemy zle", a nie "handel
# poszedl zle". Lekarstwem jest zapisanie rzeczywistego przeplywu w
# `exec.cash_flow` - wtedy zgadywania nie ma w ogole.
ZGADNIETA_ODCHYLKA_MAX = Decimal("0.20")


def zapisz_punkt(cur, engine: str, mode: str, stan: dict, unrealized: Decimal,
                 as_of: datetime | None = None) -> dict:
    """Jeden punkt krzywej z odczytu konta. CZYSTA arytmetyka + INSERT."""
    as_of = (as_of or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    wplaty, units = przeplywy(cur, engine, mode)
    zgadniete = False
    if wplaty == 0:
        if mode != "demo":
            raise RuntimeError(f"tryb {mode} bez przeplywu w exec.cash_flow - nie zgaduje")
        wplaty, units = START_EQUITY, START_EQUITY
        zgadniete = True
    equity = Decimal(stan["equity"])
    realized = equity - wplaty - unrealized
    # Zgadnieta wplata wobec ksiag gieldy: `realized` to dokladnie roznica
    # miedzy tym, co konto ma, a tym, co mu przypisalismy. Przy zgadnietej
    # wplacie duze `realized` mowi o zlym zgadnieciu, nie o wyniku handlu.
    if zgadniete and abs(realized) > ZGADNIETA_ODCHYLKA_MAX * wplaty:
        raise RuntimeError(
            f"zgadnieta wplata {wplaty} nie zgadza sie z ksiegami gieldy: equity "
            f"{equity}, niezrealizowane {unrealized}, czyli zrealizowany P&L "
            f"{realized} - ponad {ZGADNIETA_ODCHYLKA_MAX:%} zgadnietej wplaty. "
            f"Zarejestruj rzeczywisty przeplyw w exec.cash_flow ({engine}/{mode}) "
            f"- punktu krzywej na zgadnietej liczbie nie zapisuje")
    nav = (equity / units) if units > 0 else Decimal(1)
    cur.execute(
        "INSERT INTO exec.equity_point (engine, mode, event_time, equity, cash, unrealized,"
        " realized_cum, exposure, nav, units, wplaty_netto)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (engine, mode, event_time) DO NOTHING",
        (engine, mode, as_of, equity, Decimal(stan["cash"]), unrealized, realized,
         Decimal(stan["ekspozycja"]), nav, units, wplaty))
    return {"equity": equity, "nav": nav, "realized": realized, "unrealized": unrealized,
            "ekspozycja": Decimal(stan["ekspozycja"]),
            "zapisany": getattr(cur, "rowcount", 1) > 0}


async def zmierz_unrealized(adapter) -> Decimal:
    """Niezrealizowany P&L z ksiag gieldy. BLAD, gdy adapter go nie mierzy.

    DLACZEGO BLAD, A NIE ZERO

    Do 2026-09-23 stalo tu `unrealized = Decimal(0)` plus `if hasattr(...)`,
    czyli BRAK POMIARU byl zwracany jako POMIAR ROWNY ZERU. `pozycje_szczegoly`
    ma wylacznie `AlpacaAdapter`; `BybitAdapter` go nie ma, a `--engine`
    przyjmuje dowolny napis bez `choices=`, wiec `--engine crypto` dawalo
    adapter Bybita, falszywy `hasattr` i zero.

    Zero w tym miejscu nie jest ostrozne, tylko klamliwe: `realized = equity -
    wplaty - unrealized`, wiec zerowe `unrealized` ksieguje CALY zysk otwartych
    pozycji jako ZREALIZOWANY. Przy otwartej pozycji z papierowym zyskiem
    1200 USD kafelek "Zrealizowany P&L" (overview.py -> Overview.jsx) mowi
    "zrealizowany 1200" - o pieniadzach, ktorych nie ma i ktore rynek moze
    zabrac. Kolumna `unrealized` szla przy tym do bazy jako 0, wiec z samego
    zapisu nie da sie pozniej poznac, ze pomiaru nie bylo.

    To ta sama zasada, ktora trzyma `loop.Wycena.pomiar_pelny` i
    `statystyka.przedzial_sredniej`: brak pomiaru musi wygladac inaczej niz
    pomiar. Tutaj "inaczej" znaczy: nie zapisujemy punktu w ogole.
    """
    if not hasattr(adapter, "pozycje_szczegoly"):
        raise RuntimeError(
            f"adapter {type(adapter).__name__} nie mierzy niezrealizowanego P&L "
            f"- punkt krzywej ksiegowalby zysk otwartych pozycji jako "
            f"zrealizowany, wiec go nie zapisuje")
    return sum((p["unrealized_pl"] for p in await adapter.pozycje_szczegoly()), Decimal(0))


async def cykl(url: str, engine: str, mode: str) -> dict:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        adapter = adapter_danych(conn, engine, mode)
        try:
            stan = await adapter.stan_konta()
            unrealized = await zmierz_unrealized(adapter)
        finally:
            await adapter.close()
        cur = conn.cursor(row_factory=dict_row)
        wynik = zapisz_punkt(cur, engine, mode, stan, unrealized)
        conn.commit()
        return wynik


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="equity")
    ap.add_argument("--mode", default="demo", choices=("demo", "live"))
    ap.add_argument("--interval", type=int, default=900)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")
    nazwa = f"{PULS}-{args.engine}"
    pod_rzad = 0
    while True:
        t0 = datetime.now(timezone.utc)
        try:
            w = await cykl(url, args.engine, args.mode)
            print(f"[{t0:%H:%M:%S}] {args.engine}/{args.mode}: kapitał {w['equity']:.2f}, "
                  f"nav {w['nav']:.4f}, ekspozycja {w['ekspozycja']:.2f}"
                  f"{'' if w['zapisany'] else ' (punkt tej minuty już był)'}")
            with psycopg.connect(url, row_factory=dict_row) as conn:
                puls.bij(conn, nazwa, args.interval, {"equity": float(w["equity"])})
            pod_rzad = 0
        except Exception as e:                                # noqa: BLE001
            pod_rzad += 1
            print(f"[{t0:%H:%M:%S}] cykl nieudany ({pod_rzad}/{POD_RZAD_MAX}): "
                  f"{type(e).__name__}: {e}")
            try:
                with psycopg.connect(url, row_factory=dict_row) as conn:
                    puls.zglos_awarie(conn, nazwa, args.interval, pod_rzad,
                                      f"{type(e).__name__}: {e}")
            except Exception:                                 # noqa: BLE001
                pass
            if pod_rzad >= POD_RZAD_MAX:
                return 1
        if args.once:
            return 0 if pod_rzad == 0 else 1
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
