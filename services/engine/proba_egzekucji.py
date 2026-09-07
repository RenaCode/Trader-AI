"""Próba ścieżki egzekucji: złóż, sprawdź, anuluj — bez tworzenia pozycji.

DLACZEGO TO ISTNIEJE

Ścieżka składania zleceń została napisana, otestowana na atrapach i włączona
na produkcji, ale do chwili pierwszej decyzji NIE DOTKNĘŁA PRAWDZIWEJ GIEŁDY.
Pierwsze prawdziwe zlecenie poszłoby więc w nocy, bez nadzoru — a błąd
w parametrach objawiłby się jako odrzucenie, czyli w logu jako brak sygnału.

Ta próba wykonuje CAŁY OBIEG na prawdziwej giełdzie: złożenie z limitem,
odczyt stanu po naszym identyfikatorze, anulowanie. Odpowiada na pytania,
na które atrapa odpowiedzieć nie może:

  * czy ten podmiot w ogóle przyjmuje zlecenia z limitem na spocie,
  * czy nasze zaokrąglenie ilości i ceny mieści się w jego regułach,
  * czy `orderLinkId` wraca tak, jak zakładamy przy idempotencji,
  * czy anulowanie działa — bo bez niego kill-switch jest tylko deklaracją.

CENA CELOWO NIEWYKONALNA. Limit kupna ustawiamy daleko poniżej rynku, więc
zlecenie nie ma jak się wypełnić. Próba sprawdza ŚCIEŻKĘ, a nie strategię —
wypełnienie utworzyłoby pozycję, której żadna decyzja nie popiera, i zaśmieciło
dziennik oraz uzgodnienie.

NIC NIE ZAPISUJEMY W NASZEJ BAZIE. Identyfikator ma własny prefiks, więc nie
może zostać wzięty za zlecenie z planu. Wiersz w `exec.order` opisywałby
zlecenie, za którym nie stoi żadna decyzja — a dziennik decyzji ma być kompletny
w drugą stronę też.

ANULOWANIE JEST W `finally`. Próba, która przy błędzie zostawia zlecenie
w arkuszu, jest gorsza niż jej brak.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.zlecenia import Filtry, sprawdz_zlecenie  # noqa: E402
from services.exchanges.base import ExchangeError  # noqa: E402

# Ile ponizej ceny rynkowej stawiamy limit. Jedna czwarta rynku jest DALEKO
# poza zasiegiem jednej swiecy, a jednoczesnie na tyle blisko, ze gielda nie
# odrzuci zlecenia jako absurdalnego.
UDZIAL_CENY = Decimal("0.25")
PREFIKS = "proba-egz"


class Niezaliczona(RuntimeError):
    """Któryś krok obiegu nie zadziałał. Każdy ma własny komunikat."""


async def przebieg(adapter, *, symbol: str, f: Filtry, cena_rynkowa: Decimal,
                   znacznik: str, kategoria: str = "spot") -> list[str]:
    """Pełny obieg na giełdzie. Zwraca listę zaliczonych kroków."""
    kroki: list[str] = []
    id_wlasne = f"{PREFIKS}-{znacznik}"

    cena_limitu = cena_rynkowa * UDZIAL_CENY
    # Ilosc z MINIMALNEJ WARTOSCI, nie z sufitu: proba ma kosztowac tyle, ile
    # musi. Zapas 20% pokrywa zaokraglenie ilosci w dol.
    minimum = f.min_wartosc or Decimal("5")
    ilosc, cena, powod = sprawdz_zlecenie(
        (minimum * Decimal("1.2")) / cena_limitu, cena_limitu, f,
        kupno=True, wyjscie=False)
    if powod:
        raise Niezaliczona(f"nie da się złożyć nawet zlecenia próbnego: {powod}")

    zlozone = None
    try:
        zlozone = await adapter.zloz_zlecenie(
            symbol=symbol, strona="buy", ilosc=ilosc, cena_limitu=cena,
            id_wlasne=id_wlasne, kategoria=kategoria)
        kroki.append(f"złożone: {ilosc} @ {cena} (id giełdy {zlozone.id_gieldy})")

        stan = await adapter.stan_zlecenia(id_wlasne, kategoria=kategoria)
        if stan is None:
            raise Niezaliczona(
                "giełda przyjęła zlecenie, ale nie zna go po NASZYM "
                "identyfikatorze — cała idempotencja opiera się na tym odczycie")
        kroki.append(f"odczytane po własnym id: status {stan.status}, "
                     f"wypełnione {stan.wypelniono}/{stan.ilosc}")

        if stan.wypelniono > 0:
            raise Niezaliczona(
                f"zlecenie próbne WYPEŁNIŁO SIĘ ({stan.wypelniono}) — limit miał "
                f"być poza zasięgiem; sprawdź cenę rynkową i mnożnik")

        # Idempotencja: drugie zlozenie tego samego id ma ODNALEZC pierwsze,
        # a nie utworzyc drugie. To jedyna ochrona przed podwojna pozycja przy
        # nieudanej odpowiedzi, wiec musi byc sprawdzona na prawdziwej gieldzie.
        ponowne = await adapter.zloz_zlecenie(
            symbol=symbol, strona="buy", ilosc=ilosc, cena_limitu=cena,
            id_wlasne=id_wlasne, kategoria=kategoria)
        if not ponowne.juz_istnialo:
            raise Niezaliczona(
                "ponowne złożenie tego samego identyfikatora UTWORZYŁO DRUGIE "
                "zlecenie zamiast odnaleźć pierwsze — to jest dokładnie ten "
                "mechanizm, który tworzy podwójną pozycję")
        kroki.append("idempotencja: ponowne złożenie odnalazło istniejące zlecenie")
    finally:
        if zlozone is not None:
            # ANULOWANIE W `finally`. Proba, ktora przy bledzie zostawia zlecenie
            # w arkuszu, jest gorsza niz jej brak.
            try:
                zdjete = await adapter.anuluj_zlecenie(
                    symbol=symbol, id_wlasne=id_wlasne, kategoria=kategoria)
                kroki.append(f"anulowane: {'tak' if zdjete else 'nie było czego'}")
            except ExchangeError as e:
                kroki.append(f"ANULOWANIE NIE POWIODŁO SIĘ: {e} — "
                             f"zlecenie {id_wlasne} zostaje w arkuszu")
    return kroki


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--symbol", default=None, help="domyślnie pierwszy dopuszczony")
    args = ap.parse_args()

    if args.mode != "demo":
        # Na zywym rachunku proba kosztuje prawdziwe pieniadze i tworzy wpis
        # w historii konta. Ma sens wylacznie na demo.
        sys.exit("próba egzekucji jest przewidziana wyłącznie dla trybu demo")
    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_one, open_pool
    from services.engine.loop import zbuduj_adapter

    await open_pool()
    try:
        inst = await fetch_one(
            "SELECT i.symbol, i.lot_size, i.tick_size, i.min_notional,"
            "       (i.meta->>'min_ilosc') AS min_ilosc,"
            "       (SELECT close FROM market.candle c WHERE c.instrument_id = i.id"
            "         ORDER BY c.event_time DESC LIMIT 1) AS cena"
            "  FROM ref.instrument i"
            " WHERE i.active AND i.tradeable AND i.engine::text = %s"
            + (" AND i.symbol = %s" if args.symbol else "")
            + " ORDER BY i.symbol LIMIT 1",
            (args.engine, args.symbol) if args.symbol else (args.engine,))
        if not inst or inst["cena"] is None:
            sys.exit("brak instrumentu z ceną — nie ma od czego odmierzyć limitu")

        f = Filtry(krok_ilosci=inst["lot_size"], krok_ceny=inst["tick_size"],
                   min_wartosc=inst["min_notional"],
                   min_ilosc=Decimal(inst["min_ilosc"]) if inst["min_ilosc"] else None)
        url = os.environ["DATABASE_URL"]
        adapter = await zbuduj_adapter(url, args.mode)
        print(f"próba egzekucji na {inst['symbol']}, cena rynkowa {inst['cena']}, "
              f"limit {UDZIAL_CENY:.0%} rynku (poza zasięgiem)")
        try:
            kroki = await przebieg(adapter, symbol=inst["symbol"], f=f,
                                   cena_rynkowa=Decimal(str(inst["cena"])),
                                   znacznik=args.engine)
        except Niezaliczona as e:
            print(f"  NIEZALICZONA: {e}")
            return 1
        finally:
            await adapter.close()

        for k in kroki:
            print(f"  {k}")
        if any("NIE POWIODŁO" in k for k in kroki):
            return 1
        print("ścieżka egzekucji przechodzi cały obieg na prawdziwej giełdzie")
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
