"""Tor R4: momentum przekrojowe na decyzji dobowej z filtrem trendu. KOD BADAWCZY.

DLACZEGO (audyt Fable R4 / sekcja 12, hipoteza H-3 w docs/hipotezy.md)

H-1 zamknelo kalibracje ensemble 4h: pod pesymistycznym modelem wypelnien
-11,2 pb na runde, 0/5 przesuniec ponad zerem. Jedyna klasa sygnalu z realnym
priorem w krypto, ktorej projekt nie zmierzyl, to wolne momentum przekrojowe:
ranking 30-dniowy, decyzja raz na dobe, long tylko gdy rynek (BTC) jest
w trendzie. Przy ruchu dobowym 1-2,5% koszt rundy 40-50 pb to ulamek ruchu,
a opoznienie 20 min przestaje miec znaczenie.

TO NIE JEST PETLA PRODUKCYJNA. Modul niczego nie zapisuje, nie laczy sie
z baza i nie jest importowany przez `loop.py`. Liczy wylacznie pomiar H-3 na
pliku panelu (`panel_plik`), w TYCH SAMYCH oknach WF co trener (`train.okna_wf`,
siatka zakotwiczona w `KOTWICA_SIATKI`), z TA SAMA karta kosztu
(`train._koszty`) i TYMI SAMYMI odniesieniami B1-B3 (`baseline.odniesienia_okna`).

PARAMETRY USTALONE Z GORY (H-3) - kazda zmiana to nowa hipoteza:

    sygnal      zwrot T-30 d .. T-1 d z zamkniec swiec 4h znanych w chwili T
    decyzja     zamkniecie doby UTC, top 3, long-only, rowne wagi przy wejsciu
    filtr       zamkniecie dobowe BTC > EMA 50 d (`baseline._trend_w_gore`)
    wyjscie     wypadniecie z top 3 albo filtr; bez EXIT_RATIO, bez stopow
    wejscie     limit = cena 20 min po decyzji - pol spreadu instrumentu,
                wypelnione tylko, gdy `low` kolejnej godziny dotknie limitu
                (`portfolio.wycena_wysylki`, model pesymistyczny z H-1)
    wyjscie     taker: cena 20 min po decyzji, poslizg 4 pb, prowizja taker
    ekspozycja  wariant P: 15% kapitalu (limit produkcyjny), wariant F: 100%

PRZYSZLOSC. Sygnal i filtr czytaja wylacznie swiece zamkniete najpozniej
w chwili decyzji (`_cena_na`, `_trend_w_gore` - te same funkcje co B2).
Swiece 1h po decyzji sluza WYLACZNIE do wykonania zlecenia, tak jak w modelu
pesymistycznym trenera. Test `tests/test_r4_momentum.py` psuje przyszlosc
i sprawdza, ze decyzja sie nie zmienia.
"""
from __future__ import annotations

import contextlib
import io
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from services.engine import baseline as odniesienia
from services.engine.baseline import _cena_na, _trend_w_gore
from services.engine.horizons import H4H, Horizon
from services.engine.koszty import (
    POSLIZG_TAKER, karta_rundy, koszt_niewypelnienia, koszty, pyl_rundy,
    zwroty_po_doplacie,
)
from services.engine.portfolio import Egzekucja, wycena_wysylki
from services.engine.risk_rules import START_EQUITY
from services.engine.statystyka import deflowany_sharpe, wilson

# PARAMETRY H-3 - USTALONE PRZED POMIAREM (docs/hipotezy.md). Te same liczby co
# B2 poza rebalansem: B2 zmienia sklad co tydzien, R4 codziennie.
PARAMETRY_R4 = {
    "okno_momentum_dni": 30,
    "pominiete_dni": 1,
    "top": 3,
    "ema_filtra_dni": 50,
    "opoznienie_min": 20,
}
# Laczna ekspozycja przy pelnym skladzie, jako udzial kapitalu.
WARIANTY = {"P": 0.15, "F": 1.0}
N_WARIANTOW = len(WARIANTY)        # N do DSR (H-3)
N_PERMUTACJI = 200
SEED_PERMUTACJI = 20261004

# Kryteria STOP/GO 2 (H-3, K1-K4).
PROG_DODATNIE = 0.65
PROG_B1 = 0.65
PROG_DSR = 0.95
PROG_PERMUTACJI = 70.0
PROG_B2 = 0.55

DOBA = timedelta(days=1)


# ------------------------------------------------------------ decyzje

@dataclass(frozen=True)
class Wykonanie:
    """Cena w chwili wysylki i `low` godziny, ktora rozstrzyga wypelnienie
    limitu kupna. None w polach = brak swiec 1h (dziura w danych)."""
    cena: float | None
    low_nastepnej: float | None


@dataclass(frozen=True)
class Decyzja:
    """Wszystko, co jest potrzebne do jednej decyzji dobowej - policzone RAZ,
    zeby permutacje przeliczaly tylko ranking, nie dane."""
    t: datetime                       # polnoc UTC, chwila decyzji
    filtr: bool                       # BTC > EMA 50 d
    sygnaly: dict[str, float]         # momentum instrumentow z kompletem cen
    wykonanie: dict[str, Wykonanie]   # wszystkie instrumenty okna
    cena_decyzji: dict[str, float]    # zamkniecie doby (fallback wyjscia)


def momentum(indeks: dict[datetime, dict], chwila: datetime, bar_minutes: int,
             p: dict = PARAMETRY_R4) -> float | None:
    """Zwrot od T-30 d do T-1 d z zamkniec znanych w chwili `chwila`."""
    koniec = _cena_na(indeks, chwila - timedelta(days=p["pominiete_dni"]), bar_minutes)
    poczatek = _cena_na(indeks, chwila - timedelta(days=p["okno_momentum_dni"]),
                        bar_minutes)
    if not koniec or not poczatek:
        return None
    return koniec / poczatek - 1


def ranking(sygnaly: dict[str, float], top: int) -> list[str]:
    """Top N po sygnale; remis rozstrzyga symbol (jak B2)."""
    return [s for _, s in sorted((-v, s) for s, v in sygnaly.items())[:top]]


def dni_decyzji(test_from: datetime, test_to: datetime, bar_minutes: int) -> list[datetime]:
    """Polnoce UTC, na ktorych zapada decyzja w oknie: zamkniecie swiecy
    zaczynajacej sie w oknie i nie ostatniej (konwencja B2 - ostatnia swieca
    okna sluzy domknieciu)."""
    krok = timedelta(minutes=bar_minutes)
    t = test_from.replace(hour=0, minute=0, second=0, microsecond=0)
    while t < test_from + krok:
        t += DOBA
    wynik = []
    while t <= test_to - krok:
        wynik.append(t)
        t += DOBA
    return wynik


class Filtr:
    """`_trend_w_gore` dla BTC z pamiecia - decyzje przesuniec siatki
    dziela dni, a funkcja czyta cala historie."""

    def __init__(self, wiersze: list[dict] | None, bar_minutes: int, okres: int):
        self.wiersze, self.bar_minutes, self.okres = wiersze, bar_minutes, okres
        self.pamiec: dict[datetime, bool] = {}

    def __call__(self, chwila: datetime) -> bool:
        if self.wiersze is None:
            return False
        if chwila not in self.pamiec:
            self.pamiec[chwila] = _trend_w_gore(self.wiersze, chwila, self.bar_minutes,
                                                self.okres)
        return self.pamiec[chwila]


def decyzje_okna(panele: dict[str, list[dict]], indeksy: dict[str, dict],
                 swiece_1h: dict[str, dict] | None, aktywne: list[str],
                 test_from: datetime, test_to: datetime, filtr: Filtr,
                 bar_minutes: int = 240, p: dict = PARAMETRY_R4) -> list[Decyzja]:
    """Decyzje dobowe jednego okna. Sygnal i filtr wylacznie z przeszlosci."""
    out = []
    for t in dni_decyzji(test_from, test_to, bar_minutes):
        sygnaly, wyk, ceny = {}, {}, {}
        for s in aktywne:
            idx = indeksy[s]
            m = momentum(idx, t, bar_minutes, p)
            if m is not None:
                sygnaly[s] = m
            c = _cena_na(idx, t, bar_minutes)
            if c is not None:
                ceny[s] = c
            if c is None:
                wyk[s] = Wykonanie(None, None)
                continue
            row = {"t": t - timedelta(minutes=bar_minutes), "price": c}
            w = wycena_wysylki(row, bar_minutes, p["opoznienie_min"],
                               (swiece_1h or {}).get(s))
            wyk[s] = (Wykonanie(None, None) if w is None
                      else Wykonanie(w[0], w[1]["low"]))
        out.append(Decyzja(t, filtr(t), sygnaly, wyk, ceny))
    return out


# ------------------------------------------------------------ symulacja

@dataclass
class WynikOkna:
    zwrot: float = 0.0                  # % START_EQUITY, po doplacie
    ekspozycja: float = 0.0             # srednia czesc kapitalu w rynku
    rundy: list[float] = field(default_factory=list)   # zwrot rundy po doplacie
    wypelnione: int = 0
    niewypelnione: int = 0
    brak_swiec: int = 0
    utracone: list[float] = field(default_factory=list)
    wejscia: list[tuple[datetime, str]] = field(default_factory=list)


def _cena_konca(panele: dict[str, list[dict]], sym: str, test_from: datetime,
                test_to: datetime) -> float | None:
    """Ostatnie zamkniecie instrumentu w oknie (jak domkniecie B2)."""
    ostatnia = None
    for r in reversed(panele.get(sym, [])):
        if r["t"] < test_from:
            break
        if r["t"] < test_to:
            ostatnia = r["price"]
            break
    return ostatnia


def symuluj_okno(decyzje: list[Decyzja], ceny_konca: dict[str, float | None],
                 test_from: datetime, test_to: datetime, *, udzial: float,
                 spread_bps: dict[str, float], kind: str, doplata: float,
                 p: dict = PARAMETRY_R4, rng: random.Random | None = None) -> WynikOkna:
    """Jedno okno R4. `rng` - permutacja: przy kazdej decyzji wartosci sygnalu
    tasowane miedzy instrumentami; reszta (filtr, wykonanie, koszty) bez zmian.

    Zwrot rundy jak w B2 i `simulate_portfolio`: wyj/wej - 1 - prowizja_wej
    - prowizja_wyj * wyj/wej, potem doplata z karty (`zwroty_po_doplacie`).
    Wejscie placi makera (limit), wyjscie takera plus poslizg.
    """
    k = koszty(kind)
    notional = START_EQUITY * udzial / p["top"]
    opoznienie = timedelta(minutes=p["opoznienie_min"])
    w = WynikOkna()
    trzymane: dict[str, tuple[float, datetime]] = {}
    pnl = 0.0
    czas_w_rynku = 0.0

    def zamknij(sym: str, cena_wyj: float, t: datetime) -> None:
        nonlocal pnl, czas_w_rynku
        wej, t_wej = trzymane.pop(sym)
        wyj = cena_wyj * (1 - POSLIZG_TAKER)
        ret = wyj / wej - 1 - k.maker - k.taker * wyj / wej
        ret = zwroty_po_doplacie([ret], doplata)[0]
        w.rundy.append(ret)
        pnl += ret * notional
        czas_w_rynku += notional * (t - t_wej).total_seconds()

    for i, d in enumerate(decyzje):
        sygnaly = d.sygnaly
        if rng is not None and len(sygnaly) > 1:
            symbole = sorted(sygnaly)
            wartosci = [sygnaly[s] for s in symbole]
            rng.shuffle(wartosci)
            sygnaly = dict(zip(symbole, wartosci))
        cel = set(ranking(sygnaly, p["top"])) if d.filtr else set()
        t_wys = d.t + opoznienie
        for sym in sorted(set(trzymane) - cel):
            cena = d.wykonanie[sym].cena if sym in d.wykonanie else None
            if cena is None:
                w.brak_swiec += 1
                cena = d.cena_decyzji.get(sym)
                if cena is None:
                    continue        # brak jakiejkolwiek ceny: trzymamy dalej
            zamknij(sym, cena, t_wys)
        for sym in sorted(cel - set(trzymane)):
            wyk = d.wykonanie.get(sym)
            if wyk is None or wyk.cena is None or wyk.low_nastepnej is None:
                w.brak_swiec += 1
                w.niewypelnione += 1
                continue
            limit = wyk.cena * (1 - spread_bps.get(sym, 0.0) / 2 / 1e4)
            if wyk.low_nastepnej <= limit:
                trzymane[sym] = (limit, t_wys)
                w.wypelnione += 1
                w.wejscia.append((d.t, sym))
            else:
                w.niewypelnione += 1
                # Ruch, ktory przepadl: do ceny wysylki nastepnej decyzji
                # albo konca okna - material na skladnik karty.
                dalej = None
                if i + 1 < len(decyzje):
                    nast = decyzje[i + 1].wykonanie.get(sym)
                    dalej = nast.cena if nast is not None else None
                dalej = dalej if dalej is not None else ceny_konca.get(sym)
                if dalej:
                    w.utracone.append(dalej / limit - 1)
    for sym in sorted(trzymane):
        cena = ceny_konca.get(sym)
        if cena is None:
            cena = trzymane[sym][0]
        zamknij(sym, cena, test_to)
    w.zwrot = pnl / START_EQUITY * 100
    w.ekspozycja = czas_w_rynku / (test_to - test_from).total_seconds() / START_EQUITY
    return w


# ------------------------------------------------------------ pomiar H-3

@dataclass
class Okno:
    test_from: datetime
    test_to: datetime
    aktywne: list[str]
    decyzje: list[Decyzja]
    ceny_konca: dict[str, float | None]
    odn: dict


@dataclass
class Dane:
    """Wejscie pomiaru: to, co trener wczytuje z pliku, plus karta."""
    panele: dict[str, list[dict]]
    swiece_1h: dict[str, dict] | None
    spread_bps: dict[str, float]
    doplata_odniesienia: float
    karta_spread_bps: float | None
    karta_nasza_ksiega: bool
    kroki: dict[str, float | None]
    ceny_max: dict[str, float | None]
    now: datetime
    kind: str
    log: str = ""


def przygotuj(plik: dict, days: int, kind: str, h: Horizon = H4H) -> Dane:
    """Uniwersum, panele, swiece 1h, spread per instrument i doplata
    odniesien - TYMI SAMYMI funkcjami co `train.train(panel=...)`."""
    from services.engine import train as trener
    wydruk = io.StringIO()
    now = plik["do"]
    zapas = 2
    egz = Egzekucja(limit=True, opoznienie_min=PARAMETRY_R4["opoznienie_min"],
                    pesymistyczna=True)
    with contextlib.redirect_stdout(wydruk):
        instruments = trener._uniwersum(None, plik, plik["engine"], h, days + zapas, None)
        k = trener._koszty(None, plik, plik["engine"], kind, h, instruments, now, days,
                           egz, None, True, None)
        panele, swiece, _ = trener._wczytaj_panele(None, plik, instruments, h, "", now,
                                                   days + zapas, True)
    od = now - timedelta(days=days)
    ceny_max = {}
    for i in instruments:
        zam = [c["close"] for c in plik.get("swiece_1h", {}).get(i["symbol"], [])
               if od <= c["t"] <= now]
        ceny_max[i["symbol"]] = max(zam) if zam else None
    return Dane(panele, swiece, dict(k.egzekucja.spread_per_instrument or {}),
                k.doplata_odniesienia, k.spread_bps, k.spread_nasza_ksiega,
                {i["symbol"]: plik["kroki"].get(i["symbol"]) for i in instruments},
                ceny_max, now, kind, wydruk.getvalue())


def karta_wariantu(dane: Dane, udzial: float, niewypelnienie: float | None = None):
    """Karta rundy R4: maker przy wejsciu (limit), pyl dla notionalu wariantu.
    Prowizja takera i poslizg na wyjsciu sa w symulacji."""
    notional = START_EQUITY * udzial / PARAMETRY_R4["top"]
    return karta_rundy(dane.kind, True, spread_bps=dane.karta_spread_bps,
                       spread_nasza_ksiega=dane.karta_nasza_ksiega,
                       pyl=pyl_rundy(dane.kroki, dane.ceny_max, notional),
                       niewypelnienie=niewypelnienie, niewypelnienie_w_symulacji=True)


def okna_przesuniecia(dane: Dane, przesuniecie: int, days: int, filtr: Filtr,
                      h: Horizon = H4H) -> list[Okno]:
    """Okna WF jednej siatki z decyzjami i odniesieniami B1-B3."""
    from services.engine import train as trener
    start = trener.poczatek_siatki(trener.KOTWICA_SIATKI, dane.now - timedelta(days=days),
                                   h.step_days) + timedelta(days=przesuniecie)
    indeksy = {s: {r["t"]: r for r in rows} for s, rows in dane.panele.items()}
    out = []
    for o in trener.okna_wf(dane.panele, h, start, dane.now):
        decyzje = decyzje_okna(dane.panele, indeksy, dane.swiece_1h, o.aktywne,
                               o.test_from, o.test_to, filtr, h.bar_minutes)
        ceny_konca = {s: _cena_konca(dane.panele, s, o.test_from, o.test_to)
                      for s in o.aktywne}
        odn = odniesienia.odniesienia_okna({s: dane.panele[s] for s in o.aktywne},
                                           o.test_from, o.test_to, h.bar_minutes,
                                           dane.kind, dane.doplata_odniesienia)
        out.append(Okno(o.test_from, o.test_to, o.aktywne, decyzje, ceny_konca, odn))
    return out


def ocen_przesuniecie(okna: list[Okno], dane: Dane, udzial: float, doplata: float,
                      n_permutacji: int, seed: int) -> dict:
    """Miary H-3 jednej siatki i jednego wariantu ekspozycji."""
    wyniki = [symuluj_okno(o.decyzje, o.ceny_konca, o.test_from, o.test_to,
                           udzial=udzial, spread_bps=dane.spread_bps, kind=dane.kind,
                           doplata=doplata) for o in okna]
    rundy = [r for w in wyniki for r in w.rundy]
    z_ekspozycja = [(o, w) for o, w in zip(okna, wyniki) if w.ekspozycja > 0]
    bije = {}
    for nazwa in ("B1", "B2", "B3"):
        por = [odniesienia.bije(w.zwrot, w.ekspozycja, o.odn[nazwa]) for o, w in z_ekspozycja]
        por = [x for x in por if x is not None]
        bije[nazwa] = (sum(por), len(por))
    wyp = sum(w.wypelnione for w in wyniki)
    niewyp = sum(w.niewypelnione for w in wyniki)
    statystyka = sum(w.zwrot for w in wyniki)
    rng = random.Random(seed)
    perm = []
    for _ in range(n_permutacji):
        perm.append(sum(symuluj_okno(o.decyzje, o.ceny_konca, o.test_from, o.test_to,
                                     udzial=udzial, spread_bps=dane.spread_bps,
                                     kind=dane.kind, doplata=doplata, rng=rng).zwrot
                        for o in okna))
    percentyl = (100 * (sum(1 for x in perm if x < statystyka)
                        + 0.5 * sum(1 for x in perm if x == statystyka)) / len(perm)
                 if perm else None)
    dsr = deflowany_sharpe(rundy, N_WARIANTOW)
    dsr_projektu = deflowany_sharpe(rundy, 59 + N_WARIANTOW)
    utracone = [u for w in wyniki for u in w.utracone]
    fill = wyp / (wyp + niewyp) if wyp + niewyp else None
    odn_srednie = {}
    for nazwa in ("B1", "B2", "B3"):
        v = [o.odn[nazwa] for o in okna if o.odn.get(nazwa) is not None]
        if v:
            odn_srednie[nazwa] = (statistics.fmean(x.zwrot for x in v),
                                  statistics.fmean(x.ekspozycja for x in v),
                                  sum(1 for x in v if x.zwrot > 0), len(v))
    return {
        "okna": len(okna),
        "z_ekspozycja": len(z_ekspozycja),
        "dodatnie": sum(1 for _, w in z_ekspozycja if w.zwrot > 0),
        "bije": bije,
        "rund": len(rundy),
        "zwrot_na_runde_pb": statistics.fmean(rundy) * 1e4 if rundy else None,
        "sharpe_rundy": dsr["sharpe"],
        "dsr": dsr["dsr"],
        "dsr_projektu": dsr_projektu["dsr"],
        "wypelnialnosc": fill,
        "brak_swiec": sum(w.brak_swiec for w in wyniki),
        "koszt_niewypelnienia_pb": (None if fill is None else
                                    (koszt_niewypelnienia(fill, utracone) or 0.0) * 1e4),
        "suma_zwrotow": statystyka,
        "sredni_zwrot_okna": statistics.fmean(w.zwrot for w in wyniki) if wyniki else None,
        "srednia_ekspozycja": (statistics.fmean(w.ekspozycja for w in wyniki)
                               if wyniki else None),
        "okna_z_zyskiem": sum(1 for w in wyniki if w.zwrot > 0),
        "percentyl_permutacji": percentyl,
        "permutacje": (min(perm), statistics.median(perm), max(perm)) if perm else None,
        "odniesienia": odn_srednie,
    }


def pomiar(plik: dict, days: int = 1400, kind: str = "spot_eu",
           przesuniecia: tuple[int, ...] = (-2, -1, 0, 1, 2),
           n_permutacji: int = N_PERMUTACJI, seed: int = SEED_PERMUTACJI) -> dict:
    """Caly pomiar H-3: {wariant: {przesuniecie: miary}} plus karty."""
    dane = przygotuj(plik, days, kind)
    btc = next((s for s in sorted(dane.panele) if s.startswith("BTC")), None)
    filtr = Filtr(dane.panele.get(btc) if btc else None, H4H.bar_minutes,
                  PARAMETRY_R4["ema_filtra_dni"])
    siatki = {p: okna_przesuniecia(dane, p, days, filtr) for p in przesuniecia}
    wynik = {"warianty": {}, "karty": {}, "log": dane.log,
             "doplata_odniesienia": dane.doplata_odniesienia,
             "spread_bps": dane.spread_bps}
    for nazwa, udzial in WARIANTY.items():
        karta = karta_wariantu(dane, udzial)
        doplata = karta.doplata_poza_symulacja
        po = {p: ocen_przesuniecie(okna, dane, udzial, doplata, n_permutacji, seed + p)
              for p, okna in siatki.items()}
        wynik["warianty"][nazwa] = po
        sredni_koszt = statistics.fmean(m["koszt_niewypelnienia_pb"] for m in po.values()
                                        if m["koszt_niewypelnienia_pb"] is not None)
        wynik["karty"][nazwa] = karta_wariantu(dane, udzial, sredni_koszt / 1e4)
        wynik.setdefault("doplaty", {})[nazwa] = doplata
    return wynik


# ------------------------------------------------------------ werdykt i wydruk

def werdykt(po: dict[int, dict]) -> tuple[bool, list[str]]:
    """STOP/GO 2 jednego wariantu wg K1-K4 (H-3). CZYSTA FUNKCJA."""
    m = list(po.values())
    z_eksp = sum(x["z_ekspozycja"] for x in m)
    dod = sum(x["dodatnie"] for x in m)
    b1 = [sum(x["bije"]["B1"][i] for x in m) for i in (0, 1)]
    b2 = [sum(x["bije"]["B2"][i] for x in m) for i in (0, 1)]
    u_dod = dod / z_eksp if z_eksp else 0.0
    u_b1 = b1[0] / b1[1] if b1[1] else 0.0
    u_b2 = b2[0] / b2[1] if b2[1] else 0.0
    dsr_ok = sum(1 for x in m if (x["dsr"] or 0.0) >= PROG_DSR)
    perm_ok = sum(1 for x in m if (x["percentyl_permutacji"] or 0.0) >= PROG_PERMUTACJI)
    k1 = u_dod >= PROG_DODATNIE and u_b1 >= PROG_B1
    k2 = dsr_ok == len(m)
    k3 = perm_ok == len(m)
    k4 = u_b2 >= PROG_B2
    def ci(k, n):
        lo, hi = wilson(k, n)
        return f"CI95 {lo:.0%}–{hi:.0%}"
    linie = [
        f"K1 okna dodatnie {dod}/{z_eksp} ({u_dod:.0%}, {ci(dod, z_eksp)}; próg "
        f"{PROG_DODATNIE:.0%}) i bije B1 {b1[0]}/{b1[1]} ({u_b1:.0%}, {ci(*b1)}; próg "
        f"{PROG_B1:.0%}) → {'TAK' if k1 else 'NIE'}",
        f"K2 DSR ≥ {PROG_DSR} na {dsr_ok}/{len(m)} przesunięć → {'TAK' if k2 else 'NIE'}",
        f"K3 percentyl permutacyjny ≥ {PROG_PERMUTACJI:.0f} na {perm_ok}/{len(m)} "
        f"przesunięć → {'TAK' if k3 else 'NIE'}",
        f"K4 bije B2 {b2[0]}/{b2[1]} ({u_b2:.0%}, {ci(*b2)}; próg {PROG_B2:.0%}) → "
        f"{'TAK' if k4 else 'NIE'}",
    ]
    go = k1 and k2 and k3 and k4
    linie.append(f"STOP/GO 2: {'GO' if go else 'STOP'}")
    return go, linie


def _f(x, fmt: str) -> str:
    return "—" if x is None else format(x, fmt)


def tabela(po: dict[int, dict]) -> list[str]:
    linie = ["| przesunięcie | okna (z ekspozycją) | dodatnie | rund | zwrot/rundę po "
             "dopłacie (pb) | Sharpe/rundę | DSR (N=2) | DSR (N=61) | wypełnialność "
             "| bije B1 | bije B2 | bije B3 | percentyl perm. |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in sorted(po):
        m = po[p]
        def b(n, m=m):
            k, c = m["bije"][n]
            return f"{k}/{c} ({k / c:.0%})" if c else "—"
        dod = (f"{m['dodatnie']}/{m['z_ekspozycja']} "
               f"({m['dodatnie'] / m['z_ekspozycja']:.0%})" if m["z_ekspozycja"] else "—")
        linie.append(
            f"| {p:+d} | {m['okna']} ({m['z_ekspozycja']}) | {dod} | {m['rund']} | "
            f"{_f(m['zwrot_na_runde_pb'], '+.1f')} | {_f(m['sharpe_rundy'], '+.4f')} | "
            f"{_f(m['dsr'], '.4f')} | {_f(m['dsr_projektu'], '.4f')} | "
            f"{_f(m['wypelnialnosc'], '.1%')} | {b('B1')} | {b('B2')} | {b('B3')} | "
            f"{_f(m['percentyl_permutacji'], '.1f')} |")
    return linie


def tabela_odniesien(po: dict[int, dict], nazwa: str) -> list[str]:
    """R4 obok B1-B3 w tych samych oknach, srednio po przesunieciach."""
    m = list(po.values())
    linie = ["| | średni zwrot w oknie (% kapitału) | średnia ekspozycja | okna z zyskiem |",
             "|---|---|---|---|"]
    zysk = sum(x["okna_z_zyskiem"] for x in m)
    okna = sum(x["okna"] for x in m)
    linie.append(f"| R4 {nazwa} | {statistics.fmean(x['sredni_zwrot_okna'] for x in m):+.3f} "
                 f"| {statistics.fmean(x['srednia_ekspozycja'] for x in m):.1%} | "
                 f"{zysk}/{okna} ({zysk / okna:.0%}) |")
    for n in ("B1", "B2", "B3"):
        v = [x["odniesienia"][n] for x in m if n in x["odniesienia"]]
        if not v:
            continue
        z, o = sum(x[2] for x in v), sum(x[3] for x in v)
        linie.append(f"| {n} | {statistics.fmean(x[0] for x in v):+.3f} | "
                     f"{statistics.fmean(x[1] for x in v):.1%} | {z}/{o} ({z / o:.0%}) |")
    return linie
