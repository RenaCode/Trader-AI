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


# Kategorie instrumentow Bybita V5. `linear` to kontrakty wieczyste kwotowane
# w stablecoinie - jedyna kategoria, w ktorej da sie zajac pozycje KROTKA.
KATEGORIE = ("spot", "linear", "inverse", "option")


async def oferta_kategorii(adapter, kategorie: tuple[str, ...] = KATEGORIE,
                           host: str | None = None) -> dict[str, int]:
    """Ile instrumentow host wystawia w kazdej kategorii. PYTAMY, NIE ZAKLADAMY.

    DLACZEGO TO TU JEST

    Caly plan "odblokujmy krotka strone kontraktami perpetual na Bybicie, ta
    sama gielda, te same klucze" stoi albo upada na jednej liczbie: ile
    perpetuali wystawia host, na ktorym stoi konto. Odpowiedz byla w projekcie
    zapisana w trzech miejscach naraz (docstring `bybit.py`, `mozliwosci.py`,
    migracja 0018) jako "bybit.eu: 0 kontraktow wieczystych" - z pomiaru
    z 2026-09-04. Komentarz sprzed trzech tygodni nie jest pomiarem dzisiejszej
    oferty gieldy, a akurat ta liczba jest jedyna, od ktorej zalezy, czy caly
    kierunek prac jest wykonalny.

    Modul istnieje dokladnie po to: "Obie rzeczy byly do sprawdzenia w minute,
    gdyby ktokolwiek zapytal hosta wprost. Ten modul pyta wprost".

    PUSTA LISTA TO WYNIK, NIE BLAD. Host unijny odpowiada na `category=linear`
    kodem 0 i pusta lista (zmierzone 2026-09-24: `{"category": "", "list": []}`)
    - nie odmawia, tylko nie ma czego oddac. Zero w tym slowniku znaczy wiec
    "zapytalismy i nie ma", a brak klucza - "zapytanie sie nie udalo". Te dwie
    rzeczy musza byc rozroznialne, bo prowadza do zupelnie innych wnioskow.

    LICZBA JEST DOLNYM OGRANICZENIEM, NIE PELNYM SPISEM. `filtry_instrumentow`
    nie chodzi po stronach, a Bybit oddaje najwyzej 500 pozycji na zapytanie -
    zmierzone 2026-09-24: api.bybit.com odpowiada `linear: 500` i `option: 500`,
    choc kontraktow wieczystych jest tam 859. Dla pytania, ktore ta funkcja ma
    rozstrzygnac ("zero czy niezero"), nie ma to znaczenia; dla kazdego innego
    ma i dlatego jest tu napisane.

    Endpoint jest PUBLICZNY, wiec nie zuzywa limitu zapytan klucza i nie dotyka
    konta.
    """
    wynik: dict[str, int] = {}
    for kat in kategorie:
        try:
            wynik[kat] = len(await adapter.filtry_instrumentow(kat, host=host))
        except ExchangeError as e:
            # Brak wpisu = "nie wiadomo". Zapisanie tu zera zrownaloby odmowe
            # hosta z oferta pusta, a to sa dwa rozne stany swiata.
            print(f"  {kat}: zapytanie nieudane ({e})")
    return wynik


def ocena_perpetuali(oferta: dict[str, int]) -> tuple[bool, str]:
    """Czy na tym hoscie da sie w ogole zajac pozycje krotka. CZYSTA FUNKCJA.

    `linear` i `inverse` to kontrakty wieczyste; `spot` i `option` nie daja
    krotkiej nogi tej strategii (opcja to inny instrument, nie inna strona).
    """
    perpy = sum(oferta.get(k, 0) for k in ("linear", "inverse"))
    if perpy:
        # "CO NAJMNIEJ", bo zapytanie nie chodzi po stronach (patrz
        # `oferta_kategorii`). Dla werdyktu nie ma to znaczenia, dla liczby ma.
        return True, (f"host wystawia co najmniej {perpy} kontraktów "
                      "wieczystych — krótka strona jest na nim wykonalna")
    if "linear" not in oferta and "inverse" not in oferta:
        return False, ("nie udało się zapytać o żadną kategorię kontraktów — "
                       "to NIE znaczy, że ich nie ma; pomiaru po prostu nie ma")
    return False, (
        "host nie wystawia ANI JEDNEGO kontraktu wieczystego — strategia "
        "dwustronna jest na nim niewykonalna niezależnie od tego, co potrafi "
        "nasz kod; potrzebny jest inny podmiot giełdy albo inna giełda")


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
    # SAMA OFERTA HOSTA, BEZ KLUCZA I BEZ RESZTY BADANIA.
    #
    # Odpowiada na jedno pytanie: czy na tym hoscie w ogole SA kontrakty
    # wieczyste, czyli czy strona krotka jest na nim wykonalna. Od tej liczby
    # zalezy sens calego kierunku "odblokujmy shorty perpetualami"; do
    # 2026-09-24 byla w projekcie wylacznie jako komentarz z pomiaru
    # sprzed trzech tygodni.
    ap.add_argument("--kategorie", action="store_true",
                    help="policz instrumenty w każdej kategorii (spot, linear, "
                         "inverse, option) — endpoint publiczny, bez klucza")
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
            if args.kategorie:
                oferta = await oferta_kategorii(adapter, host=host)
                print(f"oferta hosta {host} (tryb {args.mode})")
                for kat in KATEGORIE:
                    if kat in oferta:
                        print(f"  {kat:8s} {oferta[kat]:5d} instrumentów")
                    else:
                        print(f"  {kat:8s}   NIE WIADOMO (zapytanie nieudane)")
                mozna_short, powod = ocena_perpetuali(oferta)
                print(f"\nkrótka strona: "
                      f"{'WYKONALNA' if mozna_short else 'NIEWYKONALNA'}")
                print(f"  {powod}")
                return 0 if mozna_short else 1

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
                    return 0
                if not zmierzone:
                    # NIE ZMIERZONO - to awaria sprawdzenia, nie stan do
                    # zgloszenia, wiec zostaje kodem wyjscia.
                    return 1
                from services.api.notifications import notify
                return await zglos_rozjazd_stawek(fetch_all, notify, args.kind,
                                                  zmierzone, rozjazdy_stawek)

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


# Co ile przypominac o rozjezdzie, ktory sie NIE ZMIENIL. Cennik poprawia sie
# commitem, nie z minuty na minute - przypomnienie co 10 minut (rytm czujki)
# nie przyspieszy go, a tydzien nie pozwoli o nim zapomniec.
STAWKI_PRZYPOMNIENIE_H = 7 * 24


async def zglos_rozjazd_stawek(fetch_all, notify, kind: str, zmierzone: dict,
                               rozjazdy: list[str]) -> int:
    """Jedno powiadomienie `koszty.rozjazd` na ZMIANE stawek, nie na przebieg.

    DO 2026-10-05 JEDYNYM SYGNALEM BYL KOD WYJSCIA. Rozjazd jest STANEM - trwa,
    dopoki ktos nie poprawi CENNIKA albo gielda nie wroci do starej stawki -
    a czujka chodzi co 10 minut. Zmierzone tego dnia: maker na api-demo.bybit.eu
    skoczyl z 0,100% na 0,250% okolo 10:10 UTC i od tej chwili KAZDY Job
    `trader-zywotnosc` padal, ArgoCD trzymalo `trader` w Degraded, a monitoring
    VPS slal alarm "Job zakonczony niepowodzeniem" bez slowa o przyczynie.
    To ta sama wada, ktora `dozor.py` opisuje przy `kod_wyjscia`.

    Teraz jak kazda czujka w `dozor.py`: powiadomienie, gdy zmierzone stawki
    sa INNE niz w ostatnim zgloszeniu (albo minelo STAWKI_PRZYPOMNIENIE_H),
    kod 1 tylko w tym przebiegu, a stan przy kazdym przebiegu w logu.
    """
    stawki = {k: str(v) for k, v in zmierzone.items()}
    juz = await fetch_all(
        "SELECT 1 FROM notify.notification WHERE event_code = 'koszty.rozjazd'"
        "   AND data->>'kind' = %(kind)s"
        "   AND data->>'maker' = %(maker)s AND data->>'taker' = %(taker)s"
        "   AND created_at > now() - make_interval(hours => %(h)s)"
        " LIMIT 1", {"kind": kind, "maker": stawki.get("maker"),
                     "taker": stawki.get("taker"), "h": STAWKI_PRZYPOMNIENIE_H})
    if juz:
        # Ten sam zapis co `dozor.kod_wyjscia` - zero musi powiedziec, dlaczego
        # jest zerem. Bez importu: `dozor` ciagnie dozorce i cala baze.
        print(f"  {len(rozjazdy)} × rozjazd stawek: stan TRWAŁY i JUŻ ZGŁOSZONY, "
              f"powiadomienie w tłumieniu — kod wyjścia z tego przebiegu tego "
              f"NIE powtarza (opis stanu wyżej; to nie jest zgoda na stan)")
        return 0

    tytul = f"Cennik {kind}: giełda nalicza inne stawki niż kod"
    tresc = ("\n".join(f"• {r}" for r in rozjazdy)
             + "\n\nRachunek papierowy, backtest i trening liczą po stawce z "
               "`services/engine/koszty.py` (CENNIK). Popraw cennik albo "
               "sprawdź, czy zmiana po stronie giełdy jest trwała.")
    odbiorcy = await fetch_all('SELECT id FROM app."user" WHERE is_active')
    for u in odbiorcy:
        await notify(u["id"], "koszty.rozjazd", tytul, tresc,
                     data={"kind": kind, **stawki})
    print(f"  powiadomiono odbiorców: {len(odbiorcy)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
