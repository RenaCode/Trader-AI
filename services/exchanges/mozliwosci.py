"""Czego dany host giełdy NIE wystawia — zmierzone, nie założone.

DLACZEGO TO JEST OSOBNY MODUŁ

Podmiot unijny Bybita nie jest tą samą giełdą pod innym adresem. Ma własne
hosty, węższą ofertę (brak perpetuali) i — co okazało się najbardziej
kosztowne — NIE WYSTAWIA CZĘŚCI ENDPOINTÓW KONTA. Dowiadywaliśmy się o tym
dwa razy i za każdym razem przez awarię:

  * raz przez kod 10003, nie do odróżnienia od złego klucza, gdy zapytanie
    poszło na host globalny kluczem z unijnego,
  * raz przez 404 na `/v5/account/wallet-balance`, gdy uzgodnienie chciało
    porównać salda.

Obie rzeczy były do sprawdzenia w minutę, gdyby ktokolwiek zapytał hosta
wprost. Ten moduł pyta wprost.

CO TO ZNACZY DLA UZGODNIENIA

Uzgodnienie porównuje nasz obraz pozycji z saldami giełdy. Host, który salda
nie wystawia, czyni je NIEWYKONALNYM — a nie „zgodnym". Różnica jest cała:
milczące zero powiedziałoby „sprawdzone i w porządku" o czymś, czego sprawdzić
się nie da. Dlatego wynik tego sprawdzenia wchodzi do decyzji o dopuszczeniu
trybu do handlu, a nie tylko do logu.

WYŁĄCZNIE ODCZYT. Żaden endpoint tutaj nie zmienia stanu konta.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.exchanges.base import ExchangeError  # noqa: E402

# Endpointy, od których zależy konkretna zdolność systemu. Każdy wpis mówi,
# CO PRZESTAJE DZIALAC, gdy go nie ma — bo bez tego lista jest tylko listą.
SPRAWDZANE: tuple[tuple[str, str, dict, str], ...] = (
    ("saldo_unified", "/v5/account/wallet-balance", {"accountType": "UNIFIED"},
     "uzgodnienie sald i odczyt kapitalu"),
    ("saldo_spot", "/v5/account/wallet-balance", {"accountType": "SPOT"},
     "uzgodnienie sald (wariant SPOT)"),
    ("saldo_aktywa", "/v5/asset/transfer/query-account-coins-balance",
     {"accountType": "UNIFIED"}, "uzgodnienie sald (sciezka zapasowa)"),
    ("metadane_klucza", "/v5/user/query-api", {},
     "odczyt uprawnien klucza"),
    ("prowizje", "/v5/account/fee-rate", {"category": "spot"},
     "sprawdzenie poswiadczen i stawek"),
    ("zlecenia_otwarte", "/v5/order/realtime", {"category": "spot"},
     "sprawdzanie stanu zlecenia przed ponowieniem"),
    ("zlecenia_historia", "/v5/order/history", {"category": "spot", "limit": "1"},
     "odnajdywanie zlecen juz wypelnionych"),
    ("wypelnienia", "/v5/execution/list", {"category": "spot", "limit": "1"},
     "pomiar poslizgu i ceny wykonania"),
)

# Bez tych trzech nie wolno skladac zlecen. Uzgodnienie odpada przy braku
# KAZDEJ ze sciezek do salda, dlatego saldo jest sprawdzane osobno nizej.
WYMAGANE_DO_HANDLU = ("zlecenia_otwarte", "zlecenia_historia")
SCIEZKI_DO_SALDA = ("saldo_unified", "saldo_spot", "saldo_aktywa")


async def zbadaj(adapter) -> dict[str, dict]:
    """Pyta host o każdy endpoint i zapisuje, co odpowiedział.

    Nie przerywa na pierwszym błędzie: celem jest OBRAZ CAŁOŚCI, a nie pierwsza
    napotkana przeszkoda. Błąd endpointu jest tu wynikiem pomiaru, nie awarią.
    """
    wynik: dict[str, dict] = {}
    for nazwa, sciezka, params, do_czego in SPRAWDZANE:
        try:
            odp = await adapter._get(sciezka, params)
            wynik[nazwa] = {"jest": True, "sciezka": sciezka, "do_czego": do_czego,
                            "klucze": sorted(odp)[:5] if isinstance(odp, dict) else None}
        except ExchangeError as e:
            wynik[nazwa] = {"jest": False, "sciezka": sciezka, "do_czego": do_czego,
                            "kod": e.code, "blad": str(e)}
    return wynik


def ocena(wynik: dict[str, dict]) -> tuple[bool, list[str]]:
    """Czy na tym hoście da się handlować bezpiecznie. CZYSTA FUNKCJA.

    Zwraca (mozna, powody). `mozna=False` nie znaczy „host zepsuty" — znaczy
    „brakuje zdolnosci, ktorej wymaga bezpieczna egzekucja".
    """
    powody = []
    for nazwa in WYMAGANE_DO_HANDLU:
        if not wynik.get(nazwa, {}).get("jest"):
            powody.append(f"brak {nazwa} — bez tego nie da sie {SPRAWDZANE_OPIS[nazwa]}")
    if not any(wynik.get(n, {}).get("jest") for n in SCIEZKI_DO_SALDA):
        powody.append("zadna sciezka do salda nie odpowiada — uzgodnienie z gielda "
                      "jest NIEWYKONALNE, a nie zgodne")
    return (not powody), powody


SPRAWDZANE_OPIS = {n: d for n, _, _, d in SPRAWDZANE}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="demo")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_one, open_pool
    from services.api.security import SecretBox
    from services.exchanges.bybit import BybitAdapter

    await open_pool()
    try:
        cred = await fetch_one(
            "SELECT c.key_enc, c.secret_enc, v.demo_host, v.live_host"
            " FROM app.api_credential c JOIN ref.venue v ON v.id = c.venue_id"
            " WHERE v.code = 'bybit' AND c.mode::text = %s AND c.active"
            " ORDER BY c.id LIMIT 1", (args.mode,))
        if not cred:
            print(f"brak aktywnego klucza dla trybu {args.mode}")
            return 2
        box = SecretBox()
        host = cred["demo_host"] if args.mode == "demo" else cred["live_host"]
        adapter = BybitAdapter(api_key=box.decrypt(bytes(cred["key_enc"])),
                               api_secret=box.decrypt(bytes(cred["secret_enc"])),
                               mode=args.mode, host=host)
        try:
            wynik = await zbadaj(adapter)
        finally:
            await adapter.close()

        print(f"host {host} (tryb {args.mode})")
        for nazwa, _, _, _ in SPRAWDZANE:
            w = wynik[nazwa]
            if w["jest"]:
                print(f"  JEST     {nazwa:18s} {w['sciezka']}")
            else:
                print(f"  BRAK     {nazwa:18s} {w['sciezka']} — kod {w['kod']}")
        mozna, powody = ocena(wynik)
        print("\nhandel na tym hoscie:", "dopuszczalny" if mozna else "NIEDOPUSZCZALNY")
        for p in powody:
            print(f"  - {p}")
        return 0 if mozna else 1
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
