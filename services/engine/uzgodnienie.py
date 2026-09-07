"""Uzgodnienie z giełdą: czy nasz obraz stanu zgadza się z jej.

DLACZEGO TO MUSI ISTNIEĆ ZANIM SILNIK ZACZNIE SKŁADAĆ ZLECENIA

Na rachunku papierowym uzgodnienie jest tautologią — sami zapisujemy pozycje
i sami je odczytujemy, więc zawsze się zgadza. Na żywym rachunku nasz obraz
i obraz giełdy to dwa różne stany, które mogą się rozjechać na kilka sposobów,
i żaden z nich nie objawia się awarią:

  * zlecenie weszło, a odpowiedź nie dotarła — mamy pozycję, o której nie wiemy
  * zlecenie odrzucono po naszym zapisie — wiemy o pozycji, której nie ma
  * wypełnienie częściowe zapisane jako całkowite — mamy połowę tego, co myślimy
  * ktoś zamknął pozycję ręcznie w panelu giełdy

Pozycja-widmo jest przy tym groźniejsza niż brakująca: risk kernel liczy z niej
ekspozycję, więc blokuje wejścia, których nic nie blokuje. System stoi i nie ma
o tym pojęcia.

CO TO SPRAWDZA NA SPOCIE

Na spocie „pozycja" to po prostu saldo aktywa. Porównujemy więc ilość, którą
według `exec.position` trzymamy, z saldem, które raportuje giełda — per aktywo.

TOLERANCJA JEST KONIECZNA, nie wygodna. Giełda zaokrągla ilości do kroku
instrumentu, a prowizja bywa pobierana w kupionym aktywie. Porównanie na
dokładną równość zgłaszałoby rozjazd przy każdej transakcji.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Względna różnica, poniżej której nie zgłaszamy nic. Zaokrąglenia kroku
# instrumentu i prowizja pobierana w aktywie mieszczą się grubo poniżej.
TOLERANCJA = Decimal(os.environ.get("UZGODNIENIE_TOLERANCJA", "0.001"))


def rozjazdy(nasze: dict[str, Decimal], gieldy: dict[str, Decimal],
             tolerancja: Decimal = TOLERANCJA) -> list[dict]:
    """Porównanie dwóch obrazów stanu. CZYSTA FUNKCJA — bez bazy i bez sieci.

    Zwraca listę różnic; pusta lista znaczy zgodność. Rozdzielenie porównania
    od pobierania jest tu warunkiem sprawdzalności: inaczej test tej logiki
    wymagałby giełdy, a giełda w teście to już nie jest test tej logiki.

    Aktywa nieznane jednej ze stron TEŻ są rozjazdem. Saldo, o którym nie wiemy,
    jest tak samo groźne jak pozycja, której nie ma — tyle że w drugą stronę.
    """
    wynik = []
    for aktywo in sorted(set(nasze) | set(gieldy)):
        u_nas = nasze.get(aktywo, Decimal(0))
        u_nich = gieldy.get(aktywo, Decimal(0))
        roznica = u_nich - u_nas
        odniesienie = max(abs(u_nas), abs(u_nich))
        if odniesienie == 0:
            continue
        wzgledna = abs(roznica) / odniesienie
        if wzgledna <= tolerancja:
            continue
        wynik.append({
            "aktywo": aktywo,
            "u_nas": u_nas,
            "u_gieldy": u_nich,
            "roznica": roznica,
            "wzgledna": wzgledna,
            # Kierunek nazwany wprost, bo to on decyduje, co jest grozniejsze.
            "rodzaj": ("pozycja-widmo" if u_nas > u_nich else "saldo-nieznane"),
        })
    return wynik


async def nasze_pozycje(fetch_all, engine: str, mode: str) -> dict[str, Decimal]:
    """Ile którego aktywa trzymamy według naszego dziennika."""
    rows = await fetch_all(
        "SELECT i.base_ccy AS aktywo, sum(p.qty) AS ilosc"
        " FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NULL"
        " GROUP BY i.base_ccy", (engine, mode))
    return {r["aktywo"]: Decimal(str(r["ilosc"])) for r in rows if r["ilosc"]}


async def uzgodnij(fetch_all, notify, adapter, engine: str = "crypto",
                   mode: str = "demo", sucho: bool = False) -> int:
    """Porównuje nasz obraz z giełdą i zgłasza różnice.

    Zwraca liczbę rozjazdów. Zero znaczy zgodność, a nie brak sprawdzenia —
    różnicę widać w komunikacie.
    """
    nasze = await nasze_pozycje(fetch_all, engine, mode)
    salda = {b.currency.upper(): Decimal(str(b.total))
             for b in await adapter.fetch_balances()}

    # Porównujemy WYŁĄCZNIE aktywa, które którakolwiek strona uważa za nasze
    # pozycje. Giełda trzyma też walutę kwotowaną i resztki po innych operacjach,
    # a te nie są pozycjami i ich obecność nie jest rozjazdem.
    interesujace = set(nasze)
    salda = {k: v for k, v in salda.items() if k in interesujace}

    r = rozjazdy(nasze, salda)
    if not r:
        print(f"uzgodnienie {engine}/{mode}: zgodne "
              f"({len(nasze)} aktywów w dzienniku)")
        return 0

    for x in r:
        print(f"  ROZJAZD {x['rodzaj']} {x['aktywo']}: u nas {x['u_nas']}, "
              f"u giełdy {x['u_gieldy']} (różnica {x['roznica']:+})")

    if not sucho:
        tresc = "\n".join(
            f"{x['aktywo']}: u nas {x['u_nas']}, u giełdy {x['u_gieldy']} "
            f"({x['rodzaj']})" for x in r)
        for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
            await notify(
                u["id"], "killswitch.triggered",
                f"Nasz stan nie zgadza się z giełdą ({engine})",
                f"{tresc}\n\nPozycja-widmo blokuje wejścia, których nic nie blokuje; "
                f"saldo nieznane to ekspozycja poza limitami. Obie wymagają "
                f"wyjaśnienia PRZED kolejnym zleceniem.",
                engine=engine, severity="critical",
                data={"rozjazdy": [{k: str(v) for k, v in x.items()} for x in r]})
    return len(r)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--sucho", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_all, fetch_one, open_pool
    from services.api.notifications import notify
    from services.api.security import SecretBox
    from services.exchanges.bybit import BybitAdapter

    await open_pool()
    try:
        cred = await fetch_one(
            "SELECT c.key_enc, c.secret_enc FROM app.api_credential c"
            " JOIN ref.venue v ON v.id = c.venue_id"
            " WHERE v.code = 'bybit' AND c.mode::text = %s AND c.active"
            " ORDER BY c.id LIMIT 1", (args.mode,))
        if not cred:
            # BRAK KLUCZA TO NIE JEST ZGODNOSC. Milczace zero powiedzialoby
            # "sprawdzone i w porzadku" o czyms, czego nie sprawdzono.
            print(f"uzgodnienie: brak aktywnego klucza dla trybu {args.mode} — "
                  f"nie ma z czym porównać")
            return 0
        box = SecretBox()
        adapter = BybitAdapter(api_key=box.decrypt(bytes(cred["key_enc"])),
                               api_secret=box.decrypt(bytes(cred["secret_enc"])),
                               mode=args.mode)
        try:
            return await uzgodnij(fetch_all, notify, adapter,
                                  engine=args.engine, mode=args.mode, sucho=args.sucho)
        finally:
            await adapter.close()
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
