"""Ile warta jest strona KROTKA - policzone, a nie oszacowane.

PYTANIE, NA KTORE TEN SKRYPT ODPOWIADA

Audyt zmierzyl, ze z 1668 decyzji w calej historii 180 przekroczylo prog
wejscia, a 135 z nich (75%) to sygnaly KROTKIE - odrzucone, bo silnik handluje
dziesiecioma parami SPOT, gdzie shortowac sie nie da. Strona dluga konwertuje
sie w 78%, krotka w 0%. System wyrzuca trzy czwarte wlasnego sygnalu.

Nikt nie policzyl, ile te 135 sygnalow bylo warte. Bez tej liczby "odblokujmy
shorty" jest zakladem, a nie decyzja - bo wcale nie wiadomo, czy krotka noga
zarabia. Pomiar z 2026-09-04 (koszty.py) mowi wrecz przeciwnie: wariant perp
przegrywal ze spotem dlugim w 3 przesunieciach okien na 4, i to BEZ naliczonego
finansowania. Ten skrypt ma to rozstrzygnac na danych, w obie strony.

CO DOKLADNIE LICZY, I CZEGO NIE UDAJE

Dwa przebiegi TEJ SAMEJ symulacji portfelowej na TYCH SAMYCH panelach, wagach
i progach - roznia sie wylacznie rodzajem instrumentu:

    spot   -> krotkie pomijane (`koszty.krotka_pozycja is False`)
    perp   -> krotkie dozwolone, prowizja maker/taker perpetuala
              I NALICZONE FINANSOWANIE (od 2026-09-24, patrz koszty.py)

Roznica miedzy nimi JEST wartoscia odblokowania krotkiej strony - przy tym
uniwersum, tych wagach i tych kosztach.

TRZY RZECZY, KTORYCH TA LICZBA NIE MOWI, I TRZEBA JE CZYTAC RAZEM Z NIA:

 1. UNIWERSUM ZOSTAJE SPOTOWE. Panele buduje sie z instrumentow
    `kind <> 'perp'` (`uniwersum.WARUNEK_UNIWERSUM_DECYZYJNEGO`), bo tylko one
    maja historie swiec i cech. Wariant "perp" stosuje wiec cennik i reguly
    perpetuala do szeregu cen pary spotowej. Ceny perpetuala i spotu tego
    samego aktywa chodza blisko siebie, ale nie sa tym samym szeregiem -
    a bazy (premii kontraktu nad spotem) ten rachunek NIE zawiera.

 2. NIE MA LIKWIDACJI. Symulacja nie modeluje wymuszonego zamkniecia pozycji
    przez gielde. Przy `max_total_exposure` na poziomie 15% kapitalu depozyt
    jest od likwidacji daleko - ale "daleko" nie znaczy "nigdy", a strata na
    shorcie nie ma gornego konca.

 3. STAWKA FINANSOWANIA POCHODZI Z PERPETUALA *USDT, a instrumenty sa
    kwotowane w USDC (migracja 0021 zapisuje to jako `funding_proxy`). Jest to
    przyblizenie i zostalo tak nazwane w bazie.

 4. MODEL WYPELNIEN NIE ZNA ODRZUCENIA POSTONLY - i to jest najgrozniejsze
    z tych czterech zastrzezen, bo dziala W JEDNA STRONE.

    Symulacja uznaje zlecenie z limitem za wypelnione, gdy nastepna swieca
    dotknela naszej ceny. Produkcja sklada je z `timeInForce: PostOnly`
    (`bybit.zloz_zlecenie`), wiec zlecenie przecinajace spread zostaje
    ODRZUCONE, a nie wypelnione takerem. To nie jest szczegol: cene limitu
    liczymy z ksiegi podmiotu GLOBALNEGO, a zlecenie ladzie w ksiedze UNIJNEJ.
    Zmierzone 2026-09-07 na 1600 swiecach 4 h: mediana rozjazdu zamkniec
    5,9 pb, p90 28,4 pb, a w 16,8% swiec rozjazd przekracza CALA runde kosztowa;
    dla NEARUSDC w polowie swiec.

    Skutkiem jest to, ze konwersja sygnalu na pozycje jest w produkcji NIZSZA,
    niz wychodzi tutaj - i dotyczy to tak samo nogi dlugiej, jak krotkiej. Ten
    skrypt liczy wiec GORNE OGRANICZENIE wartosci krotkiej strony, a nie jej
    wartosc oczekiwana.

 5. POSLIZG JEST ZALOZENIEM, NIE POMIAREM. Ma na 2026-09-24 ZERO mierzalnych
    obserwacji: wszystkie piec wypelnien gieldowych ma `exec.order.limit_price`
    rowne cenie wypelnienia co do grosza, bo do wdrozenia z 2026-09-23 17:58
    kolumna "cena wyslana" byla nadpisywana cena wykonania. Dlatego wynik idzie
    tu jako TABELA WRAZLIWOSCI po zakresie zalozen (`--poslizg-bps`), a nie
    jako jedna liczba - patrz `simulate_portfolio(poslizg=...)`.

TYLKO ODCZYT. Ten skrypt nie zapisuje do bazy ani jednego wiersza i nie dotyka
gieldy.

Uzycie:
    DATABASE_URL=... python services/engine/wartosc_krotkiej.py --bar 4h --dni 400
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.horizons import get as get_horizon  # noqa: E402
from services.engine.portfolio import (  # noqa: E402
    Egzekucja, PortfolioResult, metrics, simulate_portfolio,
)
from services.engine.risk_rules import MIN_CONFIDENCE  # noqa: E402
from services.engine.uniwersum import (  # noqa: E402
    odcisk, sql_uniwersum_decyzyjnego,
)

# Ile losowan przy przedziale ufnosci. Bootstrap, a nie rozklad t-Studenta:
# zwroty z transakcji maja grube ogony i sa skosne, wiec wzor zakladajacy
# normalnosc dalby przedzial wezszy, niz jest naprawde - czyli mylilby sie
# w strone "przewaga istnieje".
LOSOWAN = 2000


def odrzucone_shorty(cur, engine: str, mode: str,
                     od: datetime) -> tuple[int, int, int]:
    """Ile sygnalow krotkich dziennik ODRZUCIL, i ile bylo w ogole ponad progiem.

    To jest ta liczba z audytu, wzieta u zrodla zamiast przepisana. Zwraca
    (krotkie_odrzucone, dlugie_podjete, wszystkie_decyzje).

    `powod_holdu = 'short_niewykonalny'` istnieje od migracji 0040 i ma
    ograniczenie CHECK na zamkniety zbior wartosci, wiec ta nazwa albo jest
    w bazie, albo zapytanie nic nie zwroci - nie ma trzeciej mozliwosci,
    w ktorej liczymy cos innego, niz myslimy.
    """
    cur.execute(
        "SELECT count(*) FILTER (WHERE powod_holdu = 'short_niewykonalny') AS krotkie,"
        "       count(*) FILTER (WHERE action = 'enter_long')              AS dlugie,"
        "       count(*)                                                   AS wszystkie"
        "  FROM journal.decision"
        " WHERE engine = %s AND mode = %s AND decided_at >= %s",
        (engine, mode, od))
    r = cur.fetchone()
    return int(r["krotkie"] or 0), int(r["dlugie"] or 0), int(r["wszystkie"] or 0)


def zbuduj_panele(cur, engine: str, h, od: datetime, do: datetime) -> dict:
    """Panele cech dla calego uniwersum decyzyjnego. Import trenera jest lokalny,
    bo `train` ciagnie za soba caly aparat kalibracji, a tutaj potrzebne jest
    z niego jedno zapytanie."""
    from services.engine.train import atr_threshold, load_panel

    cur.execute(sql_uniwersum_decyzyjnego("id, symbol"), (engine,))
    instrumenty = cur.fetchall()
    if not instrumenty:
        raise SystemExit(f"uniwersum decyzyjne silnika {engine} jest puste")
    # ODCISK LICZONY Z TEGO SAMEGO ZBIORU, ktory wlasnie wczytalismy - inaczej
    # `load_panel` odfiltrowalby cechy przekrojowe po nazwie, ktorej nikt nie
    # zapisal, i `cross_section` zwracalby None BEZ SLADU (patrz uniwersum.py).
    fp = odcisk([i["symbol"] for i in instrumenty])

    panele, atr, ids = {}, {}, {}
    for inst in instrumenty:
        rows = load_panel(cur, inst["id"], h, od, do, fp)
        if len(rows) < 3:
            continue
        panele[inst["symbol"]] = rows
        atr[inst["symbol"]] = atr_threshold(rows)
        ids[inst["symbol"]] = inst["id"]
    if not panele:
        raise SystemExit("zaden instrument nie ma panelu w tym oknie")
    return {"panele": panele, "atr": atr, "ids": ids, "fp": fp}


def przedzial(zwroty: list[float], ufnosc: float = 0.95) -> tuple[float, float]:
    """Bootstrapowy przedzial dla SREDNIEGO zwrotu, w procentach.

    Przedzial, a nie sama srednia, bo przy kilkudziesieciu transakcjach srednia
    bez niego nie odroznia przewagi od szumu - a caly ten projekt stoi dzis na
    Sharpe 0,0452, czyli na liczbie nieodroznialnej od zera.
    """
    if len(zwroty) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(20260924)      # stale ziarno: ten sam wynik przy powtorce
    n = len(zwroty)
    srednie = sorted(
        statistics.fmean(rng.choices(zwroty, k=n)) for _ in range(LOSOWAN))
    lo = srednie[int((1 - ufnosc) / 2 * LOSOWAN)]
    hi = srednie[int((1 + ufnosc) / 2 * LOSOWAN) - 1]
    return (lo * 100, hi * 100)


def opisz_noge(nazwa: str, transakcje: list[dict]) -> None:
    if not transakcje:
        print(f"  {nazwa:12s} brak transakcji")
        return
    zwroty = [t["ret"] for t in transakcje]
    fin = sum(t.get("finansowanie", 0.0) for t in transakcje)
    lo, hi = przedzial(zwroty)
    sd = statistics.pstdev(zwroty) if len(zwroty) > 1 else 0.0
    sharpe = statistics.fmean(zwroty) / sd if sd else float("nan")
    print(f"  {nazwa:12s} n={len(zwroty):4d}  "
          f"średni zwrot {statistics.fmean(zwroty) * 100:+.4f}%  "
          f"[{lo:+.4f}%, {hi:+.4f}%]  "
          f"trafność {sum(1 for r in zwroty if r > 0) / len(zwroty):.1%}  "
          f"Sharpe/transakcję {sharpe:+.4f}  "
          f"finansowanie {fin:+.2f}")


def opisz(kind: str, res: PortfolioResult, min_trades: int) -> None:
    m = metrics(res, min_trades)
    print(f"\n{kind.upper()}")
    print(f"  zwrot z okna {m['total_return']:+.4f}%   "
          f"obsunięcie {m['max_dd']:.4f}%   "
          f"Sharpe {m['sharpe']:+.4f}   transakcji {m['n_trades']}")
    print(f"  pominięte shorty {res.pominiete_shorty}   "
          f"zablokowane przez ryzyko {res.blocked}   "
          f"niewypełnione {m['niewypelnione']}")
    # WYPLATY FINANSOWANIA WYPISUJEMY ZAWSZE, takze gdy jest ich zero.
    # Koszt 0,00 przy 0 wyplat znaczy "nie bylo za co placic", a przy 240
    # wyplatach - "stawka byla zerowa". Bez licznika te dwa stany sa
    # nieodroznialne, a pierwszy z nich oznacza, ze pomiaru NIE MA.
    print(f"  finansowanie {res.finansowanie_zaplacone:+.2f} "
          f"({res.wyplat_finansowania} wypłat"
          + (f", {res.wyplat_bez_stawki} BEZ ZNANEJ STAWKI"
             if res.wyplat_bez_stawki else "") + ")")
    if res.wyplat_bez_stawki:
        print("  UWAGA: część wypłat przeszła przez pozycję bez znanej stawki — "
              "koszt finansowania jest ZANIŻONY o nieznaną wielkość")
    opisz_noge("długa", [t for t in res.trades if t["side"] > 0])
    opisz_noge("krótka", [t for t in res.trades if t["side"] < 0])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--bar", default="4h")
    ap.add_argument("--dni", type=int, default=400)
    # DOMYSLNIE TAK, JAK CHODZI PRODUKCJA: `spot_eu` z limitem
    # (charts/trader/values.yaml: kind spot_eu, limit true). Porownanie z innym
    # wariantem bazowym mierzyloby roznice dwoch rzeczy naraz.
    ap.add_argument("--baza", default="spot_eu", choices=("spot", "spot_eu"))
    ap.add_argument("--limit", action="store_true", default=True)
    ap.add_argument("--rynkowe", dest="limit", action="store_false",
                    help="zlecenia rynkowe zamiast limitu (produkcja chodzi z limitem)")
    ap.add_argument("--min-trades", type=int, default=20)
    # POSLIZG JAKO ZAKRES, NIE JAKO LICZBA.
    #
    # Poslizg ma dzis ZERO mierzalnych obserwacji: wszystkie piec wypelnien
    # gieldowych na produkcji ma `exec.order.limit_price` rowne cenie
    # wypelnienia co do grosza - nie dlatego, ze poslizgu nie bylo, tylko
    # dlatego, ze do wdrozenia z 2026-09-23 17:58 kolumna "cena wyslana" byla
    # nadpisywana cena wykonania.
    #
    # Wynik podany przy jednej wartosci takiego zalozenia wyglada jak pomiar
    # i nie da sie go obronic. Domysl przejezdza wiec ZAKRES i pokazuje, gdzie
    # wniosek sie lamie - bo to jest jedyna uczciwa forma tej liczby, dopoki
    # poslizg nie zostanie zmierzony.
    ap.add_argument("--poslizg-bps", type=float, nargs="+",
                    default=[0.0, 2.0, 5.0, 10.0],
                    help="zakres założeń o poślizgu, w punktach bazowych na nogę "
                         "(domyślnie 0/2/5/10) — poślizg NIE JEST dziś zmierzony")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")

    import psycopg
    from psycopg.rows import dict_row

    do = datetime.now(timezone.utc)
    od = do - timedelta(days=args.dni)
    h = get_horizon(args.bar)

    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.cursor()

        krotkie, dlugie, wszystkie = odrzucone_shorty(cur, args.engine, args.mode, od)
        print(f"DZIENNIK DECYZJI ({args.dni} dni, {args.engine}/{args.mode})")
        print(f"  decyzji razem            {wszystkie}")
        print(f"  wejść długich podjętych  {dlugie}")
        print(f"  sygnałów krótkich ODRZUCONYCH (short_niewykonalny) {krotkie}")
        if krotkie == 0:
            print("  UWAGA: zero odrzuconych shortów w tym oknie. Albo okno jest "
                  "za krótkie, albo `powod_holdu` nie jest wypełniany — i wtedy "
                  "ta liczba NIE ZNACZY „shortów nie było”.")

        from services.engine.loop import active_model
        model = active_model(cur, args.engine, h)
        if not model["weights"]:
            sys.exit(
                "brak AKTYWNEJ wersji modelu — pomiar na wagach zastępczych "
                "mierzyłby inną strategię niż ta, która podjęła tamte 135 decyzji")
        print(f"\nModel: wersja {model['version']}, próg wejścia "
              f"{model['enter']:.4f}, wyjścia {model['exit']:.4f}, "
              f"min. pewność {model.get('min_conf', MIN_CONFIDENCE):.2f}")

        dane = zbuduj_panele(cur, args.engine, h, od, do)

    gates = {s: (model["enter"], model["exit"]) for s in dane["panele"]}
    egz = Egzekucja(limit=args.limit)
    print(f"\nEgzekucja: zlecenia {'z limitem' if args.limit else 'rynkowe'}, "
          f"{len(dane['panele'])} instrumentów, okno {od:%Y-%m-%d}..{do:%Y-%m-%d}")

    def przebieg(kind: str, slip_bps: float):
        # FINANSOWANIE JEST NALICZANE TYLKO NA `perp` i ma prawo PRZERWAC
        # przebieg, gdy panel nie ma stawki (`simulate_portfolio` rzuca
        # ValueError). Nie lapiemy tego: brak danych o finansowaniu znaczy,
        # ze wariantu perp nie da sie policzyc - a policzenie go z zerowym
        # kosztem byloby wynikiem wygladajacym na zmierzony.
        return simulate_portfolio(
            dane["panele"], model["weights"], dane["atr"], gates, h,
            model.get("min_conf", MIN_CONFIDENCE),
            instrument_ids=dane["ids"], kind=kind, egzekucja=egz,
            poslizg=slip_bps / 10_000.0)

    print("\nPOŚLIZG NIE JEST DZIŚ ZMIERZONY — ma zero obserwacji (wszystkie 5")
    print("wypełnień ma limit_price == cenie wypełnienia, bo kolumna była")
    print("nadpisywana do 2026-09-23 17:58). Poniżej ZAKRES ZAŁOŻEŃ, nie pomiar.")

    # Pelny rozbior tylko dla pierwszego zalozenia - inaczej wydruk mialby
    # kilkanascie ekranow i przestalby byc czytelny. Reszta idzie do tabeli
    # wrazliwosci, ktora odpowiada na jedyne pytanie, jakie ma sens przy
    # niezmierzonym zalozeniu: PRZY KTOREJ WARTOSCIA WNIOSEK SIE LAMIE.
    bazowy_slip = args.poslizg_bps[0]
    wyniki = {}
    for kind in (args.baza, "perp"):
        wyniki[kind] = przebieg(kind, bazowy_slip)
        opisz(kind, wyniki[kind], args.min_trades)

    print("\n" + "=" * 78)
    print("WRAŻLIWOŚĆ NA ZAŁOŻENIE O POŚLIZGU (perp minus baza)")
    print(f"{'poślizg':>10} {'Δ zwrot':>12} {'Δ Sharpe':>10} "
          f"{'krótkich':>9} {'śr. zwrot krótkiej':>20} {'przedział 95%':>24}")
    for slip_bps in args.poslizg_bps:
        b = metrics(przebieg(args.baza, slip_bps), args.min_trades)
        p_res = przebieg("perp", slip_bps)
        p = metrics(p_res, args.min_trades)
        krotkie_t = [t for t in p_res.trades if t["side"] < 0]
        if krotkie_t:
            zwroty = [t["ret"] for t in krotkie_t]
            lo, hi = przedzial(zwroty)
            sr = statistics.fmean(zwroty) * 100
            opis_kr = f"{sr:+.4f}%"
            opis_ci = f"[{lo:+.4f}%, {hi:+.4f}%]"
            # GWIAZDKA ZNACZY "PRZEDZIAL OBEJMUJE ZERO", czyli brak wykazanej
            # przewagi przy tym zalozeniu. Bez tej kolumny tabela pokazywalaby
            # same srednie i kazda z nich wygladalaby na wynik.
            if lo <= 0 <= hi:
                opis_ci += " *"
        else:
            opis_kr, opis_ci = "brak", "-"
        print(f"{slip_bps:>8.1f}bp {p['total_return'] - b['total_return']:>+11.4f} "
              f"{p['sharpe'] - b['sharpe']:>+10.4f} {len(krotkie_t):>9d} "
              f"{opis_kr:>20} {opis_ci:>24}")
    print("  * przedział obejmuje zero — przy tym założeniu krótka noga NIE MA")
    print("    wykazanej przewagi. To jest wynik, nie brak wyniku.")

    print("\nCzego te liczby nie zawierają: bazy kontraktu nad spotem, likwidacji,")
    print("oraz tego, że stawka finansowania pochodzi z perpetuala *USDT")
    print("(przybliżenie, migracja 0021). Poślizg jest ZAŁOŻENIEM w całej tabeli.")
    print("Uzasadnienie w docstringu na górze pliku.")


if __name__ == "__main__":
    main()
