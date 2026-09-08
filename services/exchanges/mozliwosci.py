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


# ------------------------------------------- którym kluczem w ogóle pytamy host
#
# Wybór klucza mieszka w `services/exchanges`, bo jest pytaniem o rozmowę
# z giełdą, a nie o silnik. Ta sama odpowiedź musi obowiązywać w KAŻDYM
# procesie, który mówi do giełdy — inaczej dwa procesy pytają o stan dwóch
# różnych rachunków i nikt tego nie widzi.

def wybierz_klucz(klucze: list[dict], mode: str) -> tuple[dict | None, str | None]:
    """NAJNOWSZY aktywny klucz trybu i ostrzeżenie, gdy aktywnych jest kilka.

    CZYSTA FUNKCJA — bez bazy i bez sieci. Wybór klucza jest rozstrzygany raz,
    w jednym miejscu i sprawdzalnie testem, bo objaw złego wyboru pojawia się
    dopiero przy rotacji i wtedy nie wygląda na błąd wyboru.

    DLACZEGO NAJNOWSZY, A NIE PIERWSZY Z BRZEGU

    Rotacja zostawia na chwilę dwa aktywne wiersze. Przy `ORDER BY c.id` wygrywał
    ten, który właśnie zastąpiono — bez śladu. Objaw wychodził dopiero w dniu
    wygaśnięcia starego klucza i wyglądał jak awaria uwierzytelnienia bez
    przyczyny, bo nowy klucz w bazie był przez cały czas poprawny.

    `id` zostaje jako rozstrzygnięcie remisu: dwa klucze wgrane w tej samej
    milisekundzie mają dawać ten sam wybór w każdym procesie i przy każdym
    starcie. Losowa kolejność znaczyłaby, że silnik handluje z jednego
    rachunku, a uzgodnienie sprawdza drugi.

    OSTRZEŻENIE, A NIE CISZA. Wybór jest jednoznaczny, ale dwa aktywne klucze
    naraz to stan, którego nikt nie zaplanował: albo zapomniano dezaktywować
    stary, albo ktoś wgrał klucz do złego trybu. Cisza w tym miejscu była
    połową poprzedniego błędu.
    """
    if not klucze:
        return None, None
    posortowane = sorted(klucze, key=lambda k: (k["created_at"], k["id"]),
                         reverse=True)
    cred = posortowane[0]
    if len(posortowane) == 1:
        return cred, None
    return cred, (f"{len(posortowane)} aktywnych kluczy dla trybu {mode} — "
                  f"używam najnowszego ({cred.get('key_hint')}); "
                  f"pozostałe dezaktywuj po zakończonej rotacji")


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


async def sprawdz_cennik(adapter, fetch_one,
                         kind: str) -> tuple[str | None, dict, list[str]]:
    """Porównuje stawki naliczane przez giełdę z tablicą CENNIK. Bez wypisywania.

    Zwraca (symbol, zmierzone, rozjazdy). `symbol` jest None, gdy nie ma na czym
    pytać — to jest inny wynik niż zgodność i wywołujący musi go rozróżnić.

    JEDNA PARA WYSTARCZA. Odczyt z 2026-09-04 na api-demo.bybit.eu dał tę samą
    stawkę dla wszystkich 140 par, a pytanie o każdą z osobna zamieniłoby
    czujkę w generator ruchu na endpoincie prywatnym.
    """
    from services.engine.koszty import koszty

    cur = await fetch_one(
        "SELECT symbol FROM ref.instrument WHERE active AND tradeable"
        "   AND engine::text = 'crypto' ORDER BY symbol LIMIT 1")
    if not cur:
        # BRAK INSTRUMENTU TO NIE ZGODNOSC. Milczace zero powiedzialoby
        # "cennik sprawdzony" o przebiegu, ktory nie zapytal o nic.
        return None, {}, ["uniwersum nie ma ani jednego instrumentu handlowalnego — "
                          "cennik pozostaje NIESPRAWDZONY"]
    try:
        zmierzone = await stawki_gieldy(adapter, cur["symbol"])
    except ExchangeError as e:
        # ODMOWA HOSTA TO WYNIK POMIARU, nie awaria zadania. Wyjatek przerwalby
        # przebieg tracebackiem, a tresc bledu jest tu cala informacja: klucz
        # bez uprawnienia do odczytu konta i host bez tego endpointu znacza co
        # innego, a oba konczylyby sie tak samo.
        return cur["symbol"], {}, [
            f"host odmówił odczytu stawek ({e}) — cennik w kodzie pozostaje "
            f"NIESPRAWDZONY, a to on wyznacza cały modelowany wynik"]
    return cur["symbol"], zmierzone, rozjazd_stawek(zmierzone, koszty(kind))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="demo")
    # SAM CENNIK, BEZ RESZTY BADANIA. Pełny przebieg kończy się kodem 1 na
    # każdym hoście, który nie wystawia sald — a taki jest właśnie podmiot
    # unijny w demo. Czujka meldująca awarię przy każdym przebiegu przestaje
    # być czujką, więc sprawdzenie cyklicznie uruchamiane jest WĘŻSZE: pyta
    # tylko o stawki i tylko na nich opiera kod wyjścia.
    ap.add_argument("--stawki", action="store_true",
                    help="sprawdź wyłącznie zgodność cennika ze stawkami giełdy")
    # MUSI BYC TEN SAM `--kind`, KTORYM CHODZI SILNIK (values.yaml). Cennik
    # sprawdzany dla innego rodzaju instrumentu porównywałby stawki giełdy
    # z wierszem CENNIKA, którego nikt nie używa — czyli meldowałby zgodność,
    # nie sprawdzając niczego, co wchodzi do wyniku.
    ap.add_argument("--kind", default="spot_eu")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_all, fetch_one, open_pool
    from services.api.security import SecretBox
    from services.exchanges.bybit import BybitAdapter

    await open_pool()
    try:
        # `ORDER BY` powtarza to, co robi `wybierz_klucz` — celowo. Baza
        # oddaje wiersze w kolejności, w której wybór jest już poprawny,
        # a funkcja rozstrzyga go jeszcze raz, sprawdzalnie i bez bazy.
        cred, ostrzezenie = wybierz_klucz(await fetch_all(
            "SELECT c.id, c.key_hint, c.created_at, c.key_enc, c.secret_enc,"
            "       v.demo_host, v.live_host"
            " FROM app.api_credential c JOIN ref.venue v ON v.id = c.venue_id"
            " WHERE v.code = 'bybit' AND c.mode::text = %s AND c.active"
            " ORDER BY c.created_at DESC, c.id DESC", (args.mode,)), args.mode)
        if ostrzezenie:
            print(f"UWAGA: {ostrzezenie}")
        if not cred:
            print(f"brak aktywnego klucza dla trybu {args.mode}")
            return 2
        box = SecretBox()
        host = cred["demo_host"] if args.mode == "demo" else cred["live_host"]
        # JEDNO POLACZENIE NA CALY PRZEBIEG. Poprzednia wersja budowala drugi
        # adapter na samo pytanie o stawki, bo pierwszy byl juz zamkniety —
        # czyli deszyfrowala klucz drugi raz po to, zeby zadac jedno zapytanie.
        adapter = BybitAdapter(api_key=box.decrypt(bytes(cred["key_enc"])),
                               api_secret=box.decrypt(bytes(cred["secret_enc"])),
                               mode=args.mode, host=host)
        try:
            if args.stawki:
                symbol, zmierzone, rozjazdy_stawek = await sprawdz_cennik(
                    adapter, fetch_one, args.kind)
                print(f"cennik {args.kind} kontra host {host} (tryb {args.mode})"
                      f"{f', para {symbol}' if symbol else ''}")
                if zmierzone:
                    # LICZBY, A NIE SAM WERDYKT. Stawki pelzaja z progami VIP
                    # i promocjami; wypisane przy kazdym przebiegu pokazuja
                    # ruch, zanim przekroczy prog i zamieni sie w rozjazd.
                    print(f"  giełda nalicza: maker {zmierzone['maker']:.4%}, "
                          f"taker {zmierzone['taker']:.4%}")
                for r in rozjazdy_stawek:
                    print(f"  ROZJAZD STAWEK {r}")
                if not rozjazdy_stawek:
                    print("  zgodny")
                return 1 if rozjazdy_stawek else 0

            wynik = await zbadaj(adapter)
            print(f"host {host} (tryb {args.mode})")
            for nazwa, _, _, _ in SPRAWDZANE:
                w = wynik[nazwa]
                if w["jest"]:
                    print(f"  JEST     {nazwa:18s} {w['sciezka']}")
                else:
                    print(f"  BRAK     {nazwa:18s} {w['sciezka']} — kod {w['kod']}")
            # STAWKI TEZ SA MOZLIWOSCIA HOSTA, i to najdrozsza w skutkach.
            if wynik.get("prowizje", {}).get("jest"):
                symbol, zmierzone, rozjazdy_stawek = await sprawdz_cennik(
                    adapter, fetch_one, args.kind)
                if zmierzone:
                    print(f"\nstawki dla {symbol}: maker {zmierzone['maker']:.4%}, "
                          f"taker {zmierzone['taker']:.4%} (cennik {args.kind})")
                for r in rozjazdy_stawek:
                    print(f"  ROZJAZD STAWEK {r}")
                if rozjazdy_stawek:
                    return 1
        finally:
            await adapter.close()

        mozna, powody = ocena(wynik)
        print("\nhandel na tym hoscie:", "dopuszczalny" if mozna else "NIEDOPUSZCZALNY")
        for p in powody:
            print(f"  - {p}")
        return 0 if mozna else 1
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

async def stawki_gieldy(adapter, symbol: str, kategoria: str = "spot") -> dict:
    """Prowizje, ktore gielda NAPRAWDE nalicza dla tego konta.

    Zwraca {"maker": Decimal, "taker": Decimal} albo pusty slownik, gdy host
    tego nie wystawia.
    """
    from decimal import Decimal

    res = await adapter._get("/v5/account/fee-rate",
                            {"category": kategoria, "symbol": symbol})
    lista = res.get("list") or []
    if not lista:
        return {}
    r = lista[0]
    return {"maker": Decimal(r.get("makerFeeRate") or "0"),
            "taker": Decimal(r.get("takerFeeRate") or "0")}


def rozjazd_stawek(zmierzone: dict, modelowane, prog=None) -> list[str]:
    """Czy cennik w kodzie zgadza sie z tym, co nalicza gielda. CZYSTA FUNKCJA.

    DLACZEGO TO MA ZNACZENIE WIEKSZE, NIZ WYGLADA

    Runda kosztuje 0,200%, a zmierzona mediana zwrotu na transakcje jest rzedu
    dziesiatych procenta. Cala przewaga strategii miesci sie wiec w kilku
    punktach bazowych - a stawki nie sa stale: zmienia je progi VIP, promocje
    i zmiany regulaminu podmiotu.

    ROZJAZD NIE OBJAWIA SIE NICZYM. Rachunek papierowy dalej liczy po starej
    stawce, backtest dalej pokazuje przewage, a prawdziwy wynik po prostu jest
    inny - i wyglada jak pech albo jak zmiana rynku. To jedna z niewielu
    pomylek, ktore falszuja WSZYSTKIE liczby naraz, wlacznie z tymi, ktorymi
    sprawdzalibysmy pomylke.

    Prog domyslny to jeden punkt bazowy: mniejsza roznica nie zmienia wnioskow,
    a wieksza zmienia je natychmiast.
    """
    from decimal import Decimal

    if prog is None:
        prog = Decimal("0.0001")
    if not zmierzone:
        # BRAK ODCZYTU TO NIE ZGODNOSC. Milczace zero powiedzialoby "sprawdzone
        # i w porzadku" o czyms, czego nie sprawdzono.
        return ["host nie wystawia stawek — cennik w kodzie pozostaje "
                "NIESPRAWDZONY, a to on wyznacza cały modelowany wynik"]
    out = []
    for nazwa, w_kodzie in (("maker", modelowane.maker), ("taker", modelowane.taker)):
        z_gieldy = zmierzone[nazwa]
        roznica = abs(z_gieldy - Decimal(str(w_kodzie)))
        if roznica > prog:
            out.append(
                f"{nazwa}: giełda nalicza {z_gieldy:.4%}, kod zakłada "
                f"{Decimal(str(w_kodzie)):.4%} (różnica {roznica:.4%}) — "
                f"rachunek papierowy i backtest liczą po nieprawdziwej stawce")
    return out

