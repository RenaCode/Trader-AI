#!/usr/bin/env python3
"""Test w przód „czy trening się uczy" (H-4, docs/hipotezy.md).

    python services/engine/w_przod.py [--engine crypto] [--bar 4h] [--kind spot_eu]
    python services/engine/w_przod.py --sucho --od 2026-08-03   # podgląd wstecz

PO CO. Trener porównuje się sam ze sobą na walk-forwardzie, którego okna
nakładają się w 29/30 dni, a zmiana miary 02-03.10 wyzerowała porównania
przebiegów (n = 1). Pytanie „czy wersja k jest lepsza od k−1" ma jedną uczciwą
odpowiedź: obie wersje zapisane i zamrożone PRZED oknem, porównane na oknie,
które zamknęło się PO zapisaniu obu — i liczba prób równa liczbie takich okien,
nie transakcji.

CO TEN PLIK ROBI
  * `pary_okien` — dla każdego pełnego tygodnia kalendarzowego (pon 00:00 UTC)
    wybiera wersję najnowszą przed jego początkiem i wersję, która była
    najnowsza tydzień wcześniej. Okna są rozłączne z konstrukcji; tydzień bez
    nowej wersji (trener padł, nic nie zapisał) nie daje pary.
  * `miara_okna` — zwrot portfela reguły ensemble z `params` wersji na oknie,
    po dopłacie z karty, w pesymistycznym modelu wypełnień, MINUS zwrot koszyka
    kup-i-trzymaj przy tej samej ekspozycji (definicja przy `miara_okna`).
  * `werdykt` — kryterium H-4 zapisane przed pomiarem: test znaków po oknach,
    rozstrzyga WYŁĄCZNIE pierwszych `N_OKIEN` okien z różnicą ≠ 0.
  * zapis każdej pary do `learn.w_przod_okno` (migracja 0095) z `git_sha`
    i `DEFINICJA_MIARY`; para raz policzona NIE jest liczona ponownie.

KODY WYJŚCIA: 0 — policzono albo nie było czego liczyć (okno otwarte, przed
2026-10-12, brak pary); `KOD_NIEGOTOWE` (3) — para była, ale okna nie dało się
policzyć (brak danych, bramka uniwersum equity), nic nie zapisano; każdy inny
kod — awaria. Skrypt zadania trenera traktuje 3 tak samo jak u trenera.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.bramki import bramki_z_modelu  # noqa: E402
from services.engine.horizons import Horizon  # noqa: E402
from services.engine.portfolio import (  # noqa: E402
    Egzekucja, indeks_swiec, kup_i_trzymaj, metrics as portfolio_metrics, precompute_panels,
    simulate_portfolio, wynik_po_doplacie,
)
from services.engine.risk_rules import MIN_CONFIDENCE  # noqa: E402
from services.engine.signals import DEFAULT_WEIGHTS  # noqa: E402
# Ten sam kod co u trenera: „nie bylo z czego policzyc", nie „policzylem i padlo".
from services.engine.train import (  # noqa: E402
    KOD_TOR_NIEGOTOWY as KOD_NIEGOTOWE, _koszty, _przekroj, _uniwersum, atr_threshold,
    load_panel, load_swiece_wykonania,
)

TYDZIEN = timedelta(days=7)
# Kryterium H-4. Zmiana którejkolwiek z tych liczb to nowa hipoteza.
N_OKIEN = 12        # okien z różnicą ≠ 0, po których zapada werdykt
PROG_LEPSZYCH = 10  # v_k lepsza w co najmniej tylu z N_OKIEN (p ≈ 0,019)
# Pierwszy tydzień PO zapisaniu H-4 (05.10, poniedziałek, w trakcie dnia).
# Okna wcześniejsze dało się już obejrzeć, więc nie są testem w przód.
POCZATEK = datetime(2026, 10, 12, tzinfo=timezone.utc)

# Tor, ktorego dotyczy kryterium H-4 (`crypto_signal_weighting_4h`). Inne tory
# licza sie ta sama miara i zapisuja do tej samej tabeli, ale werdykt dla nich
# jest wypisywany jako seria POMOCNICZA: kryterium zapisano przed pomiarem
# wylacznie dla tego toru, a przypisanie go innemu po fakcie to nowa hipoteza.
TOR_H4 = ("crypto", "4h")

# DEFINICJA MIARY - etykieta, nie sha. `git_sha` zmienia sie z kazdym
# wdrozeniem, takze takim, ktore miary nie dotyka, wiec seria filtrowana po sha
# rwalaby sie co tydzien bez powodu. Ta etykieta zmienia sie RECZNIE, razem
# z kazda zmiana tego, co liczy `miara_okna` albo `wczytaj_okno` (koszty, model
# wypelnien, rozgrzewka, uniwersum, odniesienie). Zmiana etykiety = nowa seria =
# nowa hipoteza (docs/hipotezy.md, H-4: „zmiana kodu miary przerywa serie").
# `git_sha` idzie obok, zeby dalo sie odtworzyc, z czego policzono kazde okno.
DEFINICJA_MIARY = "H-4/m1"

# Model wypelnien z H-1: limit wyslany 20 min po zamknieciu swiecy, cena
# wysylki z interpolacji 1h, pol spreadu instrumentu, wypelnienie tylko w godzinie
# po wysylce. Ten sam dla obu wersji pary - o to chodzi w porownaniu.
OPOZNIENIE_MIN = 20

# Karta kosztu per silnik - te same, na ktorych trenuje trainer-cron
# (`trainerCron.kind`, `trainerCron.equity.kind`). Wersja jest oceniana po
# cenniku, po ktorym ja kalibrowano.
KIND_SILNIKA = {"crypto": "spot_eu", "equity": "stock"}

@dataclass(frozen=True)
class Para:
    od: datetime
    do: datetime
    nowa: int         # numer wersji najnowszej przed `od`
    poprzednia: int   # numer wersji najnowszej przed `od - TYDZIEN`


def poczatek_tygodnia(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc) - timedelta(days=t.weekday())


def _najnowsza_przed(wersje: list[tuple[int, datetime]], chwila: datetime) -> int | None:
    przed = [v for v, kiedy in wersje if kiedy < chwila]
    return przed[-1] if przed else None


def pary_okien(wersje: list[tuple[int, datetime]], teraz: datetime,
               od: datetime = POCZATEK) -> list[Para]:
    """Pary (v_k, v_{k−1}) na oknach ZAMKNIĘTYCH przed `teraz`. CZYSTA FUNKCJA.

    `wersje`: (numer, created_at). Wersja zapisana w trakcie tygodnia W jest
    oceniana dopiero na W+1 — jej prognozy nie mogą dotknąć okna, w którym
    jeszcze jej nie było.
    """
    wersje = sorted(wersje, key=lambda w: w[1])
    if not wersje:
        return []
    pary = []
    okno = max(poczatek_tygodnia(wersje[0][1]) + TYDZIEN, poczatek_tygodnia(od))
    while okno + TYDZIEN <= teraz:
        nowa = _najnowsza_przed(wersje, okno)
        poprzednia = _najnowsza_przed(wersje, okno - TYDZIEN)
        if poprzednia is not None and nowa != poprzednia:
            pary.append(Para(okno, okno + TYDZIEN, nowa, poprzednia))
        okno += TYDZIEN
    return pary


def _ogon_dwumianowy(k: int, n: int) -> float:
    """P(X >= k) dla X ~ Bin(n, 1/2)."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n


def werdykt(roznice: list[float]) -> tuple[str, str]:
    """('w toku' | 'UCZY SIĘ' | 'NIE WYKAZANO', opis). CZYSTA FUNKCJA.

    `roznice`: miara(v_k) − miara(v_{k−1}) po oknach, w kolejności kalendarza.
    Remis (obie wersje bez transakcji albo identyczne) nie jest próbą.
    Werdykt bierze PIERWSZE `N_OKIEN` rozstrzygniętych okien i ani jednego
    więcej: dokładanie okien do chwili, w której wynik „wyjdzie", to
    zatrzymanie warunkowe, nie test.
    """
    rozstrzygniete = [d for d in roznice if d != 0]
    remisy = len(roznice) - len(rozstrzygniete)
    if len(rozstrzygniete) < N_OKIEN:
        return "w toku", (f"{len(rozstrzygniete)}/{N_OKIEN} okien rozstrzygniętych"
                          f" (remisów {remisy}) — werdykt dopiero po {N_OKIEN}")
    lepsza = sum(d > 0 for d in rozstrzygniete[:N_OKIEN])
    p = _ogon_dwumianowy(lepsza, N_OKIEN)
    opis = (f"v_k lepsza w {lepsza}/{N_OKIEN} okien (p = {p:.3f}, próg "
            f"{PROG_LEPSZYCH}/{N_OKIEN}); remisów {remisy}")
    return ("UCZY SIĘ" if lepsza >= PROG_LEPSZYCH else "NIE WYKAZANO"), opis


# ------------------------------------------------------------- miara okna

@dataclass(frozen=True)
class DaneOkna:
    """Wszystko, czego `miara_okna` potrzebuje do jednego tygodnia - WSPOLNE
    dla obu wersji pary. Wersje roznia sie wylacznie `params`."""
    od: datetime
    do: datetime
    h: Horizon
    kind: str
    panele: dict[str, list[dict]]          # wiersze z [od, do), niepuste
    progi_atr: dict[str, float | None]     # z rozgrzewki [od - train_days, od)
    cache: dict[str, list[tuple]]          # rezim i sygnaly - nie zaleza od wag
    egzekucja: Egzekucja
    swiece_wykonania: dict | None
    doplata: float                          # karta strategii, poza symulacja
    koszyk: float                           # B1: kup-i-trzymaj, % kapitalu
    inst_ids: dict[str, int]


def dane_okna(panele_pelne: dict[str, list[dict]], od: datetime, do: datetime,
              h: Horizon, kind: str, egzekucja: Egzekucja,
              swiece_wykonania: dict | None, doplata: float,
              doplata_odniesienia: float,
              inst_ids: dict[str, int] | None = None) -> DaneOkna | None:
    """Okno [od, do) z paneli siegajacych wstecz o rozgrzewke. CZYSTA FUNKCJA.

    WYLACZNIE DANE SPRZED DECYZJI. Cechy w panelu sa juz bitemporalne
    (`train.load_panel`), a jedyna liczba liczona tu z historii - prog ATR
    rezimu - bierze wiersze sprzed `od`, nie z okna. Petla decyzyjna liczy go
    kroczaco z `h.train_days` wstecz od kazdej decyzji (`loop.atr_percentile_80`),
    trener - raz na okno treningowe (`train.atr_threshold`, ta sama funkcja tu);
    jeden prog z chwili `od` rozni sie od tego o tydzien przesuniecia i jest
    TEN SAM dla obu wersji pary.

    None, gdy w oknie nie ma ani jednego wiersza panelu - to brak danych,
    nie remis (remis to dwie wersje, ktore MIALY na czym handlowac).
    """
    okno = {s: [r for r in p if od <= r["t"] < do] for s, p in panele_pelne.items()}
    okno = {s: v for s, v in okno.items() if v}
    if not okno:
        return None
    progi = {s: atr_threshold([r for r in panele_pelne[s] if r["t"] < od]) for s in okno}
    return DaneOkna(
        od=od, do=do, h=h, kind=kind, panele=okno, progi_atr=progi,
        cache=precompute_panels(okno, progi, h), egzekucja=egzekucja,
        swiece_wykonania=swiece_wykonania, doplata=doplata,
        koszyk=kup_i_trzymaj(okno, kind, doplata=doplata_odniesienia),
        inst_ids=inst_ids or {})


@dataclass(frozen=True)
class WynikWersji:
    miara: float        # zwrot - ekspozycja x koszyk, punkty procentowe kapitalu
    zwrot: float        # % kapitalu po doplacie
    ekspozycja: float   # ekspozycja_przenoszona, ulamek kapitalu
    rund: int
    wypelnione: int
    niewypelnione: int
    killswitch: str | None

    def jako_slownik(self) -> dict:
        return {"miara": self.miara, "zwrot": self.zwrot, "ekspozycja": self.ekspozycja,
                "rund": self.rund, "wypelnione": self.wypelnione,
                "niewypelnione": self.niewypelnione, "killswitch": self.killswitch}


def miara_okna(dane: DaneOkna, params: dict | None) -> WynikWersji:
    """Miara H-4 jednej wersji na jednym oknie. CZYSTA FUNKCJA (bez bazy).

    `params` - `learn.model_version.params` (wagi per rezim, progi per rezim,
    `min_confidence`); None = zalozenia poczatkowe (DEFAULT_WEIGHTS, progi
    poczatkowe) - tylko informacyjnie, poza kryterium.

    MIARA = zwrot konta w oknie po doplacie z karty MINUS zwrot koszyka
    kup-i-trzymaj (B1) przy tej samej ekspozycji: `r - f x B1`, gdzie `f` to
    `ekspozycja_przenoszona`. To jest „przy tej samej ekspozycji" z H-4
    w znaczeniu, jakie to slowo ma w calym rejestrze (porownania z B1/B2,
    `baseline.bije`), i z tego samego powodu:
      * goly zwrot tygodnia jest w ~calosci kierunkiem rynku razy ekspozycja.
        Wersja, ktora czesciej siedzi w rynku, „wygrywalaby" kazdy tydzien
        wzrostow i przegrywala kazdy tydzien spadkow - test znakow mierzylby
        wtedy pogode i roznice ekspozycji, nie to, czy trening sie uczy;
      * `r - f x B1` jest liniowe w wielkosci pozycji, wiec nie da sie go
        poprawic samym zmniejszeniem albo zwiekszeniem ekspozycji;
      * obie wersje dostaja TEN SAM budzet (limity `DEFAULT_LIMITS`, kapital
        START_EQUITY), te same dane, progi ATR, karte i model wypelnien.
    Roznica pary to wiec `(r_k - r_k-1) - (f_k - f_k-1) x B1`; przy rownej
    ekspozycji - po prostu roznica zwrotow. Brak transakcji: r = 0, f = 0,
    miara 0 - dwie wersje bez transakcji albo z identycznymi decyzjami daja
    roznice DOKLADNIE zero, czyli remis.
    """
    if params is None:
        wagi, bramki, min_conf = DEFAULT_WEIGHTS, bramki_z_modelu(None), MIN_CONFIDENCE
    else:
        if not params.get("weights"):
            # Wersja bez wag to przebieg, ktory nie doszedl do zapisu wyniku.
            # Podstawienie zalozen udawaloby, ze cos wytrenowal.
            raise ValueError("wersja modelu bez `weights` — nie ma czego oceniać")
        wagi, bramki = params["weights"], bramki_z_modelu(params)
        min_conf = float(params.get("min_confidence", MIN_CONFIDENCE))
    res = simulate_portfolio(
        dane.panele, wagi, dane.progi_atr, bramki, dane.h, min_conf,
        instrument_ids=dane.inst_ids, cache=dane.cache, kind=dane.kind,
        egzekucja=dane.egzekucja, swiece_wykonania=dane.swiece_wykonania)
    res = wynik_po_doplacie(res, dane.doplata)
    m = portfolio_metrics(res, dane.h.min_trades)
    zwrot, f = m["total_return"], m["ekspozycja_przenoszona"]
    return WynikWersji(miara=zwrot - f * dane.koszyk, zwrot=zwrot, ekspozycja=f,
                       rund=m["n_trades"], wypelnione=m["wypelnione"],
                       niewypelnione=m["niewypelnione"], killswitch=m["killswitch"])


# ------------------------------------------------------------------- baza

def wczytaj_okno(cur, engine: str, h: Horizon, kind: str, od: datetime,
                 do: datetime) -> tuple[DaneOkna | None, str]:
    """(dane okna albo None, odcisk uniwersum): panel z bazy
    [od - h.train_days, do), swiece 1h i karta kosztu.

    UZYWA FUNKCJI TRENERA, nie ich kopii: uniwersum (`_uniwersum`, z bramkami
    equity - kod 3), odcisk przekroju (`_przekroj`), karta i spread per
    instrument (`_koszty`), panel bitemporalny (`load_panel`). Wersja jest
    wtedy oceniana na tym, na czym ja kalibrowano.

    KOSZTY I SPREAD Z DANYCH SPRZED OKNA: `_koszty` dostaje „teraz" = sekunde
    przed `od` i `h.train_days` wstecz. Spread tygodnia, ktorego dotyczy
    porownanie, nie wchodzi wiec do ceny limitu.

    UNIWERSUM: biezace `tradeable` w chwili liczenia, nie z chwili `od` -
    `ref.instrument` nie trzyma historii tej flagi. Zadanie liczy okno w niedziele
    po jego zamknieciu, wiec rozjazd to najwyzej tydzien; odcisk uniwersum idzie
    do zapisu, zeby zmiana byla widoczna.
    """
    rozgrzewka = timedelta(days=h.train_days)
    instruments = _uniwersum(cur, None, engine, h, h.train_days, None)
    przed_oknem = od - timedelta(seconds=1)
    egz = Egzekucja(limit=True, opoznienie_min=OPOZNIENIE_MIN, pesymistyczna=True)
    k = _koszty(cur, None, engine, kind, h, instruments, przed_oknem, h.train_days,
                egz, None, True, None)
    fp, _ = _przekroj(cur, None, instruments, h, h.train_days, None)
    panele, swiece = {}, {}
    for inst in instruments:
        p = load_panel(cur, inst["id"], h, od - rozgrzewka, do, fp=fp)
        if p:
            panele[inst["symbol"]] = p
            swiece[inst["symbol"]] = indeks_swiec(
                load_swiece_wykonania(cur, inst["id"], od, do))
    return dane_okna(panele, od, do, h, kind, k.egzekucja, swiece, k.doplata,
                     k.doplata_odniesienia,
                     {i["symbol"]: i["id"] for i in instruments}), fp


_SQL_WERSJE = (
    "SELECT v.id, v.version, v.created_at, v.params FROM learn.model_version v"
    " JOIN learn.model m ON m.id = v.model_id"
    # `params ? 'weights'`: wersja bez wag to przebieg, ktory padl przed zapisem
    # wyniku (zostal w `training`). Nie ma czego oceniac, a gdyby wchodzila do
    # `pary_okien`, zaslanialaby ostatnia PRAWDZIWA wersje przed oknem.
    " WHERE m.name = %s AND v.status <> 'failed' AND v.params ? 'weights'"
    " ORDER BY v.created_at")

_SQL_ZAPISANE = (
    "SELECT okno_od, definicja_miary, roznica FROM learn.w_przod_okno"
    " WHERE engine = %s AND bar = %s ORDER BY okno_od")

_SQL_ZAPIS = (
    "INSERT INTO learn.w_przod_okno (engine, bar, okno_od, okno_do,"
    " wersja_nowa_id, wersja_poprzednia_id, miara_nowa, miara_poprzednia, roznica,"
    " szczegoly, definicja_miary, git_sha)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    " ON CONFLICT (engine, bar, okno_od, definicja_miary) DO NOTHING")


def _opis(w: WynikWersji) -> str:
    return (f"miara {w.miara:+.4f} pp (zwrot {w.zwrot:+.4f}%, ekspozycja "
            f"{w.ekspozycja:.4f}, rund {w.rund}, wypełnione {w.wypelnione}/"
            f"{w.wypelnione + w.niewypelnione})"
            + (f", KILL-SWITCH: {w.killswitch}" if w.killswitch else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", default="crypto", choices=sorted(KIND_SILNIKA))
    ap.add_argument("--bar", default="4h")
    ap.add_argument("--kind", default=None,
                    help="karta kosztu; domyślnie ta, na której trenuje trainer-cron")
    ap.add_argument("--sucho", action="store_true",
                    help="licz i wypisz, nic nie zapisuj")
    ap.add_argument("--od", type=lambda s: datetime.fromisoformat(s).replace(
        tzinfo=timezone.utc), default=None,
        help=f"pierwsze okno (tylko z --sucho; seria liczy się od {POCZATEK:%Y-%m-%d})")
    args = ap.parse_args(argv)
    if args.od is not None and not args.sucho:
        # Okno sprzed zapisania hipotezy dalo sie juz obejrzec - zapisane obok
        # okien w przod zatarloby granice, na ktorej stoi caly test.
        ap.error("--od tylko z --sucho: okna sprzed 2026-10-12 nie są testem w przód")

    import psycopg
    from psycopg.rows import dict_row

    from services.engine.horizons import get as get_horizon
    from services.engine.wersja import sha_kodu

    h = get_horizon(args.bar)
    kind = args.kind or KIND_SILNIKA[args.engine]
    nazwa = f"{args.engine}_signal_weighting_{args.bar}"
    teraz = datetime.now(timezone.utc)
    tor_h4 = (args.engine, args.bar) == TOR_H4
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as c:
        cur = c.cursor()
        cur.execute(_SQL_WERSJE, (nazwa,))
        wiersze = cur.fetchall()
        params = {w["version"]: w["params"] for w in wiersze}
        ids = {w["version"]: w["id"] for w in wiersze}
        pary = pary_okien([(w["version"], w["created_at"]) for w in wiersze], teraz,
                          od=args.od or POCZATEK)
        print(f"H-4 {nazwa} ({'kryterium H-4' if tor_h4 else 'seria pomocnicza, poza kryterium H-4'}"
              f"): wersji z wagami {len(wiersze)}, okien zamkniętych z parą {len(pary)};"
              f" miara {DEFINICJA_MIARY}, karta {kind}, limit z opóźnieniem "
              f"{OPOZNIENIE_MIN} min, model pesymistyczny"
              + (" — SUCHO, nic nie zapisuję" if args.sucho else ""))
        if not pary:
            pierwsze = poczatek_tygodnia(args.od or POCZATEK)
            print(f"  brak okna do policzenia: pierwsze okno {pierwsze:%Y-%m-%d}, "
                  f"zamyka się {pierwsze + TYDZIEN:%Y-%m-%d}; para wymaga nowej "
                  f"wersji zapisanej w tygodniu przed oknem")

        cur.execute(_SQL_ZAPISANE, (args.engine, args.bar))
        zapisane = cur.fetchall()
        seria = {w["okno_od"]: w["roznica"] for w in zapisane
                 if w["definicja_miary"] == DEFINICJA_MIARY}
        inne = sorted({w["definicja_miary"] for w in zapisane} - {DEFINICJA_MIARY})
        if inne:
            print(f"  UWAGA: w bazie są okna policzone inną definicją miary "
                  f"({', '.join(inne)}) — nie wchodzą do tej serii")

        niegotowe = 0
        sha = sha_kodu()
        for p in pary:
            etykieta = (f"  {p.od:%Y-%m-%d} – {p.do:%Y-%m-%d}: v{p.nowa} wobec "
                        f"v{p.poprzednia}")
            if p.od in seria and not args.sucho:
                # Zamrozone: okno policzone raz nie jest liczone ponownie.
                # Przeliczenie po rewizji swiec albo po wdrozeniu zmienialoby
                # wynik, ktory juz wszedl do serii - po cichu.
                print(f"{etykieta}: d = {seria[p.od]:+.4f} (zapisane)")
                continue
            dane, fp = wczytaj_okno(cur, args.engine, h, kind, p.od, p.do)
            if dane is None:
                niegotowe += 1
                print(f"{etykieta}: BRAK DANYCH — ani jednego wiersza panelu "
                      f"w oknie; nie remis, nie próba, nic nie zapisano "
                      f"(sprawdź potok cech świecy {h.bar})")
                continue
            nowa = miara_okna(dane, params[p.nowa])
            poprz = miara_okna(dane, params[p.poprzednia])
            zal = miara_okna(dane, None)
            d = nowa.miara - poprz.miara
            print(f"{etykieta}: d = {d:+.4f}"
                  + (" (REMIS)" if d == 0 else (" (v_k lepsza)" if d > 0 else " (v_k gorsza)")))
            print(f"    v{p.nowa}: {_opis(nowa)}")
            print(f"    v{p.poprzednia}: {_opis(poprz)}")
            print(f"    założenia początkowe (informacyjnie, poza kryterium): {_opis(zal)}")
            print(f"    koszyk B1 {dane.koszyk:+.4f}%, instrumentów {len(dane.panele)}, "
                  f"dopłata {dane.doplata * 1e4:.2f} pb/rundę")
            seria[p.od] = d
            if args.sucho:
                continue
            szczegoly = {"nowa": nowa.jako_slownik(), "poprzednia": poprz.jako_slownik(),
                         "zalozenia": zal.jako_slownik(),
                         "wersja_nowa": p.nowa, "wersja_poprzednia": p.poprzednia,
                         "koszyk_b1": dane.koszyk, "doplata": dane.doplata,
                         "kind": kind, "opoznienie_min": OPOZNIENIE_MIN,
                         "instrumenty": sorted(dane.panele), "odcisk_uniwersum": fp,
                         "swiec": sum(len(v) for v in dane.panele.values())}
            cur.execute(_SQL_ZAPIS, (args.engine, args.bar, p.od, p.do, ids[p.nowa],
                                     ids[p.poprzednia], nowa.miara, poprz.miara, d,
                                     json.dumps(szczegoly), DEFINICJA_MIARY, sha))
            c.commit()

        stan, opis = werdykt([seria[k] for k in sorted(seria)])
        print(f"  {'WERDYKT H-4' if tor_h4 else 'stan serii pomocniczej'}"
              f"{' (SUCHO, nie test w przód)' if args.sucho else ''}: {stan} — {opis}")
    return KOD_NIEGOTOWE if niegotowe else 0


if __name__ == "__main__":
    sys.exit(main())
