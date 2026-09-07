"""Pobranie reguł handlu z giełdy do `ref.instrument`.

DLACZEGO TO NIE JEST DROBIAZG KONFIGURACYJNY

Dziesięć z dwunastu instrumentów dopuszczonych do handlu nie miało w bazie ani
kroku ilości, ani kroku ceny, a minimalnej wartości zlecenia nie miał ŻADEN.
Pierwsze zlecenie na produkcji zostałoby odrzucone jako błąd parametru — a taki
błąd w nocnym logu jest nie do odróżnienia od braku sygnału. System „nie
handluje" i nikt nie wie, że coś się zepsuło.

ENDPOINT JEST PUBLICZNY. Nie wymaga klucza, więc uzupełnienie reguł da się
zrobić zanim ktokolwiek dotknie prawdziwych pieniędzy — i nie zależy od tego,
którego podmiotu jest konto.

NIEKOMPLETNE REGUŁY BLOKUJĄ HANDEL, a nie tylko zapisują ostrzeżenie. Zwracany
kod wyjścia jest niezerowy, dopóki którykolwiek instrument dopuszczony do handlu
nie ma kompletu — bo to jest stan, w którym egzekucji uruchamiać nie wolno.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def rozbieznosci(w_bazie: list[dict], z_gieldy: dict[str, dict]) -> list[dict]:
    """Co trzeba zmienić, żeby baza zgadzała się z giełdą. CZYSTA FUNKCJA.

    Zwraca wyłącznie instrumenty WYMAGAJĄCE zmiany. Instrument, którego giełda
    nie zna, też jest zmianą — ale opisaną jako brak, nie jako zerowe reguły:
    zero przeszłoby przez każde sprawdzenie i wywróciło się dopiero przy
    dzieleniu.
    """
    out = []
    for r in w_bazie:
        g = z_gieldy.get(r["symbol"])
        if g is None:
            out.append({"symbol": r["symbol"], "id": r["id"], "brak_u_gieldy": True})
            continue
        nowe = {"lot_size": g["krok_ilosci"], "tick_size": g["krok_ceny"],
                "min_notional": g["min_wartosc"]}
        zmiany = {k: v for k, v in nowe.items()
                  if v is not None and (r.get(k) is None or r[k] != v)}
        # PORÓWNANIE PO TEKŚCIE. `meta` jest jsonb, więc wraca jako string,
        # a giełda daje Decimal — porównanie wprost jest zawsze różne i meta
        # przepisywałaby się przy każdym uruchomieniu bez końca.
        min_z_gieldy = str(g["min_ilosc"]) if g["min_ilosc"] is not None else None
        if zmiany or r.get("min_ilosc_meta") != min_z_gieldy:
            out.append({"symbol": r["symbol"], "id": r["id"], "zmiany": zmiany,
                        "min_ilosc": g["min_ilosc"], "status": g["status"],
                        "brak_u_gieldy": False})
    return out


def niekompletne(w_bazie: list[dict]) -> list[str]:
    """Instrumenty dopuszczone do handlu, którym wciąż brakuje reguł."""
    return sorted(r["symbol"] for r in w_bazie if r.get("tradeable")
                  and (r.get("lot_size") is None or r.get("tick_size") is None
                       or r.get("min_notional") is None))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaz, co by sie zmienilo, i nie zapisuj")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, execute, fetch_all, open_pool
    from services.exchanges.bybit import BybitAdapter

    await open_pool()
    try:
        # BEZ KLUCZA. Endpoint instrumentow jest publiczny, wiec adapter
        # powstaje z pustymi poswiadczeniami - i to jest cecha, nie brak:
        # uzupelnienie regul nie zalezy od tego, czy klucz w ogole istnieje.
        adapter = BybitAdapter(api_key="", api_secret="", mode="demo")
        try:
            z_gieldy = await adapter.filtry_instrumentow("spot")
        finally:
            await adapter.close()

        w_bazie = await fetch_all(
            "SELECT id, symbol, lot_size, tick_size, min_notional, tradeable,"
            "       (meta->>'min_ilosc') AS min_ilosc_meta"
            " FROM ref.instrument WHERE active AND engine::text = %s ORDER BY symbol",
            (args.engine,))
        print(f"giełda zna {len(z_gieldy)} instrumentów; w bazie {len(w_bazie)} "
              f"aktywnych ({args.engine})")

        do_zmiany = rozbieznosci(w_bazie, z_gieldy)
        for z in do_zmiany:
            if z["brak_u_gieldy"]:
                print(f"  BRAK U GIEŁDY {z['symbol']} — reguł nie ma skąd wziąć")
                continue
            opis = ", ".join(f"{k}={v}" for k, v in z["zmiany"].items()) or "(tylko meta)"
            print(f"  {z['symbol']}: {opis}")
            if args.sucho:
                continue
            if z["zmiany"]:
                sety = ", ".join(f"{k} = %({k})s" for k in z["zmiany"])
                await execute(f"UPDATE ref.instrument SET {sety} WHERE id = %(id)s",
                              {**z["zmiany"], "id": z["id"]})
            await execute(
                "UPDATE ref.instrument SET meta = coalesce(meta,'{}'::jsonb)"
                " || jsonb_build_object('min_ilosc', %s::text,"
                "                       'status_gieldy', %s::text) WHERE id = %s",
                (str(z["min_ilosc"]) if z["min_ilosc"] is not None else None,
                 z["status"], z["id"]))

        if args.sucho:
            print("\n(sucho — nic nie zapisano)")
            return 0

        po = await fetch_all(
            "SELECT symbol, lot_size, tick_size, min_notional, tradeable"
            " FROM ref.instrument WHERE active AND engine::text = %s", (args.engine,))
        brakuje = niekompletne(po)
        if brakuje:
            print(f"\nNADAL BEZ KOMPLETU REGUŁ ({len(brakuje)}): {', '.join(brakuje)}")
            print("Egzekucji nie wolno uruchamiać, dopóki ta lista nie jest pusta —"
                  " zlecenie bez reguł zostanie odrzucone, a odrzucenie w logu"
                  " wygląda jak brak sygnału.")
            return 1
        print("\nwszystkie instrumenty dopuszczone do handlu mają komplet reguł")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
