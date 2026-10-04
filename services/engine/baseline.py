"""Odniesienia B0-B3: proste strategie, ktore ensemble musi pobic. CZYSTE FUNKCJE.

DLACZEGO (audyt Fable R3 / T5, 2026-10-03)

Odsetek dodatnich okien nie mowi, czy dziewiec sygnalow i piec rezimow wnosi
cokolwiek ponad „badz long, gdy rynek rosnie". Do tej pory jedynym punktem
odniesienia byl koszyk kupiony i trzymany (B1), a to odniesienie ma inna
ekspozycje i inna czestotliwosc decyzji niz strategia. Ponizej trzy kolejne,
liczone na TYCH SAMYCH panelach, w TYCH SAMYCH oknach, z TYM SAMYM cennikiem
(`koszty.koszty(kind)`, taker + poslizg) i ta sama doplata z karty rynkowej
co B1 - inaczej przewaga nad odniesieniem bylaby przewaga cennika.

    B0  gotowka: 0% i 4% w skali roku. Informacyjnie - nie skaluje sie
        ekspozycja, bo gotowka to alternatywa dla CALEGO kapitalu.
    B1  koszyk rownych wag, kupiony na poczatku okna i trzymany do konca
        (`portfolio.kup_i_trzymaj`).
    B2  momentum przekrojowe z filtrem trendu - parametry w `PARAMETRY_B2`,
        zapisane w docs/hipotezy.md (H-B2) PRZED pierwszym przebiegiem.
    B3  trend na BTC i ETH: long, gdy zamkniecie dobowe > EMA 50 dni, inaczej
        gotowka; stala ekspozycja na instrument.

PORÓWNANIE „PRZY TEJ SAMEJ EKSPOZYCJI". Zwrot odniesienia dzielony przez jego
srednia ekspozycje (zwrot na zlotowke zaangazowana), razy ekspozycja strategii -
ta sama logika co dotychczasowe porownanie z B1, uogolniona na odniesienia,
ktore same bywaja w gotowce. Odniesienie, ktore cale okno przesiedzialo
w gotowce, ma zwrot zero i ekspozycje zero: strategia bije je wtedy, gdy
zarobila cokolwiek. Ekspozycja strategii to `ekspozycja_przenoszona`
(patrz `portfolio.PortfolioResult.avg_exposure_przenoszona`) - ta sama
definicja co tutaj: to, co portfel niesie przez przedzial po kazdej swiecy.

KONWENCJA WYKONANIA odniesien B2/B3: decyzja na zamknieciu swiecy, ktora konczy
dobe UTC, wykonanie zleceniem rynkowym po cenie zamkniecia NASTEPNEJ swiecy
(tak samo jak `simulate_portfolio` przy zleceniach rynkowych), domkniecie
wszystkiego na ostatniej swiecy okna. Sygnal liczony wylacznie z cen znanych
w chwili decyzji.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from services.engine.koszty import POSLIZG_TAKER, koszty, zwroty_po_doplacie
from services.engine.portfolio import kup_i_trzymaj
from services.engine.risk_rules import START_EQUITY

# PARAMETRY B2 - USTALONE Z GORY, Z LITERATURY, NIE STROJONE (docs/hipotezy.md,
# H-B2). Kazda zmiana to nowa hipoteza z nowym wpisem, nie poprawka.
PARAMETRY_B2 = {
    "okno_momentum_dni": 30,      # zwrot od T-30 d ...
    "pominiete_dni": 1,           # ... do T-1 d (pominiecie ostatniego dnia)
    "top": 3,                     # tyle instrumentow z najwyzszym zwrotem
    "rebalans_dni": 7,            # sklad zmieniany co tydzien
    "ema_filtra_dni": 50,         # BTC > EMA 50 d na zamknieciach dobowych
}
# B3: BTC i ETH, ten sam filtr EMA 50 d, stala czesc kapitalu na instrument.
PARAMETRY_B3 = {"ema_dni": 50, "udzial_na_instrument": 0.05}
STOPA_B0 = 0.04


@dataclass(frozen=True)
class Odniesienie:
    nazwa: str
    zwrot: float          # % kapitalu START_EQUITY w oknie, po kosztach
    ekspozycja: float     # srednia czesc kapitalu w rynku
    rund: int = 0
    uwaga: str = ""

    @property
    def na_jednostke(self) -> float:
        """Zwrot na jednostke sredniej ekspozycji. Zero przy samej gotowce."""
        return self.zwrot / self.ekspozycja if self.ekspozycja > 0 else 0.0


def bije(zwrot_strategii: float, ekspozycja_strategii: float,
         odn: Odniesienie | None) -> bool | None:
    """Czy strategia pobila odniesienie PRZY TEJ SAMEJ EKSPOZYCJI.

    None, gdy porownania nie ma: brak odniesienia (np. brak BTC w uniwersum)
    albo strategia bez ekspozycji - zero razy cokolwiek „przegrywa" z kazdym
    dodatnim zwrotem i nie mowi nic.
    """
    if odn is None or ekspozycja_strategii <= 0:
        return None
    return zwrot_strategii > odn.na_jednostke * ekspozycja_strategii


def b0_gotowka(od: datetime, do: datetime, stopa_roczna: float = 0.0) -> Odniesienie:
    dni = (do - od).total_seconds() / 86400
    return Odniesienie(f"B0 {stopa_roczna:.0%}", stopa_roczna * dni / 365 * 100, 1.0)


def b1_koszyk(okno: dict[str, list[dict]], kind: str, doplata: float) -> Odniesienie:
    return Odniesienie("B1", kup_i_trzymaj(okno, kind, doplata=doplata), 1.0,
                       rund=sum(1 for r in okno.values() if len(r) >= 2))


# ---------------------------------------------------------------- narzedzia

def _koniec_doby(t: datetime, bar_minutes: int) -> bool:
    """Czy swieca zaczynajaca sie w `t` zamyka sie o polnocy UTC.

    PRZELICZENIE NA UTC (przeglad 2026-10-04, T-18). Znaczniki z bazy przychodza
    w strefie sesji (`TimeZone`), a `.hour == 0` sprawdzane w innej strefie
    nie trafialo w ani jedna polnoc: te same swiece dawaly 60 zamkniec dobowych
    w UTC i 0 w `Europe/Warsaw` - EMA None, filtr False i caly sklad po cichu
    do gotowki. Dzis dzialalo, bo baza ma `Etc/UTC`; wystarczylby `PGTZ` w podzie.
    `tzinfo=None` traktujemy jako UTC (tak zapisuja testy i symulacja).
    """
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc)
    k = t + timedelta(minutes=bar_minutes)
    return k.hour == 0 and k.minute == 0


def _cena_na(wiersze: dict[datetime, dict], chwila: datetime,
             bar_minutes: int) -> float | None:
    """Zamkniecie swiecy konczacej sie dokladnie o `chwila` (albo do doby
    wczesniej, gdy tej brakuje). Wylacznie przeszlosc wzgledem `chwila`."""
    krok = timedelta(minutes=bar_minutes)
    t = chwila - krok
    granica = chwila - timedelta(days=1)
    while t >= granica:
        w = wiersze.get(t)
        if w is not None:
            return w["price"]
        t -= krok
    return None


def _ema(wartosci: list[float], okres: int) -> float | None:
    if len(wartosci) < okres:
        return None
    a = 2 / (okres + 1)
    e = sum(wartosci[:okres]) / okres
    for v in wartosci[okres:]:
        e = a * v + (1 - a) * e
    return e


def _zamkniecia_dobowe(wiersze: list[dict], do: datetime,
                       bar_minutes: int) -> list[float]:
    """Zamkniecia swiec konczacych dobe UTC, do chwili `do` wlacznie."""
    krok = timedelta(minutes=bar_minutes)
    return [r["price"] for r in wiersze
            if _koniec_doby(r["t"], bar_minutes) and r["t"] + krok <= do]


def _trend_w_gore(wiersze: list[dict], chwila: datetime, bar_minutes: int,
                  okres: int) -> bool:
    """Zamkniecie dobowe > EMA(okres) zamkniec dobowych. Za krotka historia =
    False (gotowka): brak wiedzy nie jest sygnalem wejscia."""
    zam = _zamkniecia_dobowe(wiersze, chwila, bar_minutes)
    e = _ema(zam, okres)
    return e is not None and zam[-1] > e


def _symuluj_cele(historia: dict[str, list[dict]], od: datetime, do: datetime,
                  bar_minutes: int, kind: str, doplata: float,
                  cele, nazwa: str) -> Odniesienie:
    """Wspolny silnik B2/B3: `cele(chwila, numer_doby) -> {symbol: notional}`
    wolane na kazdej swiecy konczacej dobe; wykonanie po zamknieciu nastepnej
    swiecy, domkniecie na ostatniej swiecy okna.

    Zwrot transakcji jak w `simulate_portfolio` (zlecenie rynkowe): poslizg
    po obu stronach, prowizja taker od obu notionalow, doplata karty raz na
    runde (`koszty.zwroty_po_doplacie`).
    """
    k = koszty(kind)
    fee = k.taker
    okno = {s: [r for r in rows if od <= r["t"] < do] for s, rows in historia.items()}
    po_czasie = {s: {r["t"]: r for r in rows} for s, rows in okno.items()}
    os_czasu = sorted({r["t"] for rows in okno.values() for r in rows})
    if len(os_czasu) < 3:
        return Odniesienie(nazwa, 0.0, 0.0, uwaga="za krotkie okno")

    trzymane: dict[str, tuple[float, float]] = {}   # symbol -> (cena wejscia, notional)
    oczekujace: dict[str, float] | None = None       # cel do wykonania na nastepnej swiecy
    pnl = 0.0
    rund = 0
    suma_ekspozycji = 0.0
    doba = 0

    def zamknij(sym: str, cena: float) -> None:
        nonlocal pnl, rund
        wej, notional = trzymane.pop(sym)
        wyj = cena * (1 - POSLIZG_TAKER)
        ret = wyj / wej - 1 - fee * (1 + wyj / wej)
        ret = zwroty_po_doplacie([ret], doplata)[0]
        pnl += ret * notional
        rund += 1

    for t in os_czasu[:-1]:
        if oczekujace is not None:
            for sym in list(trzymane):
                if sym not in oczekujace and sym in po_czasie and t in po_czasie[sym]:
                    zamknij(sym, po_czasie[sym][t]["price"])
            for sym, notional in oczekujace.items():
                if sym in trzymane:
                    continue
                w = po_czasie.get(sym, {}).get(t)
                if w is None:
                    continue   # brak swiecy - wejscie przepada, jak w symulacji
                trzymane[sym] = (w["price"] * (1 + POSLIZG_TAKER), notional)
            oczekujace = None
        if _koniec_doby(t, bar_minutes):
            oczekujace = cele(t + timedelta(minutes=bar_minutes), doba)
            doba += 1
        suma_ekspozycji += sum(n for _, n in trzymane.values())

    ostatnia = os_czasu[-1]
    for sym in list(trzymane):
        w = po_czasie.get(sym, {}).get(ostatnia)
        if w is None:
            # Instrument bez ostatniej swiecy: ostatnia znana cena w oknie.
            w = okno[sym][-1]
        zamknij(sym, w["price"])
    ekspozycja = suma_ekspozycji / (len(os_czasu) - 1) / START_EQUITY
    return Odniesienie(nazwa, pnl / START_EQUITY * 100, ekspozycja, rund=rund)


def b2_momentum(historia: dict[str, list[dict]], od: datetime, do: datetime,
                bar_minutes: int, kind: str, doplata: float,
                p: dict = PARAMETRY_B2) -> Odniesienie | None:
    """Momentum przekrojowe top-N, rowne wagi, filtr BTC > EMA. None bez BTC."""
    btc = next((s for s in sorted(historia) if s.startswith("BTC")), None)
    if btc is None:
        return None
    po_czasie = {s: {r["t"]: r for r in rows} for s, rows in historia.items()}
    notional = START_EQUITY / p["top"]
    sklad: list[str] = []

    def cele(chwila: datetime, doba: int) -> dict[str, float]:
        nonlocal sklad
        if not _trend_w_gore(historia[btc], chwila, bar_minutes, p["ema_filtra_dni"]):
            sklad = []
            return {}
        if doba % p["rebalans_dni"] == 0 or not sklad:
            wyniki = []
            for s, w in po_czasie.items():
                koniec = _cena_na(w, chwila - timedelta(days=p["pominiete_dni"]), bar_minutes)
                poczatek = _cena_na(w, chwila - timedelta(days=p["okno_momentum_dni"]),
                                    bar_minutes)
                if koniec and poczatek:
                    wyniki.append((koniec / poczatek - 1, s))
            wyniki.sort(key=lambda x: (-x[0], x[1]))
            sklad = [s for _, s in wyniki[:p["top"]]]
        return dict.fromkeys(sklad, notional)

    return _symuluj_cele(historia, od, do, bar_minutes, kind, doplata, cele, "B2")


def b3_trend(historia: dict[str, list[dict]], od: datetime, do: datetime,
             bar_minutes: int, kind: str, doplata: float,
             p: dict = PARAMETRY_B3) -> Odniesienie | None:
    """BTC i ETH, kazdy osobno: long przy zamknieciu dobowym > EMA. None bez obu."""
    nogi = [s for s in sorted(historia) if s.startswith(("BTC", "ETH"))]
    if not nogi:
        return None
    notional = START_EQUITY * p["udzial_na_instrument"]

    def cele(chwila: datetime, _doba: int) -> dict[str, float]:
        return {s: notional for s in nogi
                if _trend_w_gore(historia[s], chwila, bar_minutes, p["ema_dni"])}

    return _symuluj_cele(historia, od, do, bar_minutes, kind, doplata, cele, "B3")


def odniesienia_okna(historia: dict[str, list[dict]], od: datetime, do: datetime,
                     bar_minutes: int, kind: str, doplata: float
                     ) -> dict[str, Odniesienie | None]:
    """Komplet odniesien dla jednego okna testowego [od, do).

    `historia` - pelne panele (takze sprzed okna: momentum 30 d i EMA 50 d
    potrzebuja przeszlosci); B1 dostaje wylacznie wycinek okna, tak samo jak
    dotad w `train()`.
    """
    okno = {s: [r for r in rows if od <= r["t"] < do] for s, rows in historia.items()}
    okno = {s: v for s, v in okno.items() if v}
    return {
        "B0": b0_gotowka(od, do, 0.0),
        "B0_4": b0_gotowka(od, do, STOPA_B0),
        "B1": b1_koszyk(okno, kind, doplata),
        "B2": b2_momentum(historia, od, do, bar_minutes, kind, doplata),
        "B3": b3_trend(historia, od, do, bar_minutes, kind, doplata),
    }
