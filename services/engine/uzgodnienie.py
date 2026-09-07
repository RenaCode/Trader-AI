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

from services.exchanges.base import ExchangeError  # noqa: E402

# Względna różnica, poniżej której nie zgłaszamy nic. Zaokrąglenia kroku
# instrumentu i prowizja pobierana w aktywie mieszczą się grubo poniżej.
TOLERANCJA = Decimal(os.environ.get("UZGODNIENIE_TOLERANCJA", "0.001"))


class Nieuzgadnialne(RuntimeError):
    """Uzgodnienia nie da sie przeprowadzic — to NIE jest zgodnosc."""


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



# --------------------------------------------------------------- z wypelnien

def pozycje_z_wypelnien(wypelnienia, baza_symbolu: dict[str, str]) -> dict[str, Decimal]:
    """Pozycja odtworzona z REJESTRU TRANSAKCJI GIELDY. Czysta funkcja.

    TO NIE JEST TO SAMO PYTANIE CO SALDO i mieszanie ich byloby najgorszym
    mozliwym uproszczeniem:

      * saldo odpowiada „ile aktywa naprawde trzymamy" — lapie takze reczne
        zamkniecie w panelu, przelew, wyplate i wszystko, co nie jest handlem,
      * wypelnienia odpowiadaja „czy nasz dziennik zgadza sie z tym, co gielda
        zapisala jako nasze transakcje" — lapia zlecenie, ktore weszlo bez
        naszej wiedzy, i zlecenie zapisane u nas, a nieistniejace u niej.

    Odtworzenie jest wiec sprawdzeniem SLABSZYM, ale niezaleznym. Na hoscie,
    ktory sald nie wystawia, jest jedynym dostepnym — i lepszym niz nic,
    pod warunkiem ze nikt go nie przedstawi jako uzgodnienia sald.

    PROWIZJA POBRANA W KUPIONYM AKTYWIE JEST ODEJMOWANA. Na spocie Bybit
    potrafi pobrac ja wlasnie tak; pominiecie tego zanizyloby odtworzona
    pozycje dokladnie o wartosc prowizji przy kazdym kupnie.
    """
    stan: dict[str, Decimal] = {}
    for w in wypelnienia:
        baza = baza_symbolu.get(w.symbol)
        if baza is None:
            # Symbol spoza naszego uniwersum. NIE POMIJAMY GO PO CICHU tylko
            # wtedy, gdy dotyczy aktywa, ktore mamy w dzienniku - a tego tutaj
            # nie wiemy. Pomijamy, a rozjazd i tak wyjdzie po stronie „u nas".
            continue
        znak = Decimal(1) if w.strona.lower() == "buy" else Decimal(-1)
        stan[baza] = stan.get(baza, Decimal(0)) + znak * w.ilosc
        if w.prowizja_w and w.prowizja_w.upper() == baza.upper():
            stan[baza] -= w.prowizja
    return {k: v for k, v in stan.items() if v != 0}


def pokrycie_wypelnien(wypelnienia, najstarsze_otwarcie) -> str | None:
    """Czy rejestr gieldy siega tak daleko wstecz, jak nasz dziennik.

    NAJGROZNIEJSZY PRZYPADEK TEJ SCIEZKI. Gielda przechowuje wypelnienia przez
    ograniczony czas i potrafi zignorowac zbyt stary `startTime`, oddajac
    milczaco samo okno retencji. Odtworzenie z takiego okna pomija wszystko,
    co bylo wczesniej — czyli zaniza pozycje i produkuje falszywy alarm
    „pozycja-widmo" o pozycji, ktora naprawde istnieje.

    Zwraca powod, gdy pokrycia brak; None, gdy jest.
    """
    if najstarsze_otwarcie is None:
        return None                      # nie mamy pozycji — nie ma czego pokrywac
    if not wypelnienia:
        return ("gielda nie oddala zadnego wypelnienia, a nasz dziennik ma "
                f"pozycje otwarta {najstarsze_otwarcie:%Y-%m-%d %H:%M} — "
                "odtworzenie nie pokrywa naszej historii")
    najstarsze_u_gieldy = min(w.czas for w in wypelnienia)
    if najstarsze_u_gieldy > najstarsze_otwarcie:
        return (f"rejestr gieldy zaczyna sie {najstarsze_u_gieldy:%Y-%m-%d %H:%M}, "
                f"a nasza najstarsza otwarta pozycja pochodzi z "
                f"{najstarsze_otwarcie:%Y-%m-%d %H:%M} — okno retencji nie "
                "pokrywa naszej historii, wiec odtworzona pozycja bylaby zanizona")
    return None

async def nasze_pozycje(fetch_all, engine: str, mode: str) -> dict[str, Decimal]:
    """Ile którego aktywa trzymamy według naszego dziennika."""
    rows = await fetch_all(
        "SELECT i.base_ccy AS aktywo, sum(p.qty) AS ilosc"
        " FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
        " WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NULL"
        " GROUP BY i.base_ccy", (engine, mode))
    return {r["aktywo"]: Decimal(str(r["ilosc"])) for r in rows if r["ilosc"]}


async def kontekst_wypelnien(fetch_all, engine: str, mode: str):
    """Mapa symbol->aktywo bazowe i data najstarszego otwarcia. Z bazy, nie z gieldy."""
    rows = await fetch_all(
        "SELECT i.symbol, i.base_ccy FROM ref.instrument i WHERE i.active")
    baza = {r["symbol"]: r["base_ccy"] for r in rows}
    r = await fetch_all(
        "SELECT min(p.opened_at) AS od FROM exec.position p"
        " WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NULL",
        (engine, mode))
    return baza, (r[0]["od"] if r else None)


async def uzgodnij(fetch_all, notify, adapter, engine: str = "crypto",
                   mode: str = "demo", sucho: bool = False) -> int:
    """Porównuje nasz obraz z giełdą i zgłasza różnice.

    Zwraca liczbę rozjazdów. Zero znaczy zgodność, a nie brak sprawdzenia —
    różnicę widać w komunikacie.
    """
    nasze = await nasze_pozycje(fetch_all, engine, mode)
    try:
        salda = {b.currency.upper(): Decimal(str(b.total))
                 for b in await adapter.fetch_balances()}
        zrodlo = "salda"
    except ExchangeError as e:
        # HOST BEZ SALD NIE JEST HOSTEM ZGODNYM. Zwrocenie zera powiedzialoby
        # "sprawdzone i w porzadku" o czyms, czego sprawdzic sie nie da. Tak
        # zachowuje sie demo podmiotu unijnego: 404 na kazdej sciezce do salda.
        #
        # Zostaje sciezka SLABSZA, ale niezalezna: odtworzenie pozycji
        # z rejestru transakcji gieldy. Odpowiada na wezsze pytanie i dlatego
        # jest OSOBNO NAZWANA az do komunikatu wlacznie.
        if e.code != "404":
            raise Nieuzgadnialne(
                f"odczyt sald nie powiodl sie ({e}); uzgodnienie NIEWYKONALNE") from e
        baza, najstarsze = await kontekst_wypelnien(fetch_all, engine, mode)
        try:
            wyp = await adapter.wypelnienia(
                od_ms=int(najstarsze.timestamp() * 1000) if najstarsze else None)
        except ExchangeError as e2:
            raise Nieuzgadnialne(
                f"ten host nie wystawia sald ({e}), a rejestru wypelnien nie da "
                f"sie pobrac ({e2}) — uzgodnienie NIEWYKONALNE") from e2
        brak = pokrycie_wypelnien(wyp, najstarsze)
        if brak:
            raise Nieuzgadnialne(f"ten host nie wystawia sald, a {brak}")
        salda = pozycje_z_wypelnien(wyp, baza)
        zrodlo = "wypelnienia"

    # Porównujemy WYŁĄCZNIE aktywa, które którakolwiek strona uważa za nasze
    # pozycje. Giełda trzyma też walutę kwotowaną i resztki po innych operacjach,
    # a te nie są pozycjami i ich obecność nie jest rozjazdem.
    interesujace = set(nasze)
    salda = {k: v for k, v in salda.items() if k in interesujace}

    r = rozjazdy(nasze, salda)
    if not r:
        if zrodlo == "salda":
            print(f"uzgodnienie {engine}/{mode}: zgodne z saldami giełdy "
                  f"({len(nasze)} aktywów w dzienniku)")
        else:
            # NIGDY nie piszemy tu "zgodne z saldami". Sprawdzone zostalo
            # wezsze pytanie i komunikat musi to niesc, bo to on trafia do
            # decyzji o dopuszczeniu trybu do handlu.
            print(f"uzgodnienie {engine}/{mode}: zgodne z REJESTREM TRANSAKCJI "
                  f"giełdy ({len(nasze)} aktywów) — UWAGA: ten host nie wystawia "
                  f"sald, więc ręczne zamknięcie w panelu i przelewy pozostają "
                  f"niesprawdzone")
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
            "SELECT c.key_enc, c.secret_enc, v.demo_host, v.live_host"
            " FROM app.api_credential c"
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
        # HOST Z ref.venue, nie z tablicy w adapterze.
        #
        # Podmiot unijny ma wlasne hosty, a klucz z bybit.eu odbity od hosta
        # globalnego wraca z kodem 10003 - komunikatem nie do odroznienia od
        # zlego klucza. Adapter ostrzega przed tym w komentarzu, a mimo to
        # pierwsza wersja tego modulu podala sam tryb i dostala "API key is
        # invalid" na kluczu, ktory jest w porzadku.
        adapter = BybitAdapter(api_key=box.decrypt(bytes(cred["key_enc"])),
                               api_secret=box.decrypt(bytes(cred["secret_enc"])),
                               mode=args.mode,
                               host=cred["demo_host"] if args.mode == "demo"
                               else cred["live_host"])
        try:
            return await uzgodnij(fetch_all, notify, adapter,
                                  engine=args.engine, mode=args.mode, sucho=args.sucho)
        except Nieuzgadnialne as e:
            print(f"uzgodnienie {args.engine}/{args.mode}: NIEWYKONALNE — {e}")
            return 2
        finally:
            await adapter.close()
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
