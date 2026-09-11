#!/usr/bin/env python3
"""Poślizg i ułamek wypełnienia MIERZONE, a nie zakładane.

DLACZEGO POMIAR, A NIE POPRAWIONA STAŁA

W `services/engine/koszty.py` stoją dwie liczby:

    POSLIZG_TAKER = 0.0004     # 4 punkty bazowe
    POSLIZG_MAKER = 0.0        # zero

Obie są ZAŁOŻENIAMI i nikt ich dotąd nie sprawdził, bo do dziś nie było czego
sprawdzać: egzekucja szła rachunkiem papierowym, który cenę wypełnienia sam
sobie wyliczał z tych właśnie stałych.

Waga jest policzalna. Runda kosztuje 0,200%, a zmierzona mediana zwrotu na
transakcję jest rzędu dziesiątych procenta — **błąd o cztery punkty bazowe
przesuwa wynik o tyle, ile cała zmierzona przewaga**. Podmiana jednej zgadywanej
liczby na inną zgadywaną nie byłaby więc poprawką, tylko zamianą jednego
założenia na drugie. Ten moduł nie zmienia żadnej stałej. Dostarcza liczbę,
którą da się z nimi zestawić — razem z uczciwą odpowiedzią „jeszcze nie wiadomo".

CO JEST CENĄ ODNIESIENIA I DLACZEGO TO NIE JEST OBOJĘTNE

Poślizg to odległość między ceną, NA KTÓREJ OPARTA BYŁA DECYZJA, a ceną, po
której transakcja faktycznie poszła. Pierwsza z nich mieszka w dwóch miejscach
i wybór między nimi nie jest kosmetyczny:

  * `journal.planned_order.limit_price` — cena, na którą system się zobowiązał
    składając zlecenie z limitem. Giełda ma obowiązek wypełnić po niej ALBO
    LEPIEJ, więc poślizg maker powinien wychodzić ujemny lub zerowy. Wynik
    dodatni jest sam w sobie informacją: albo zlecenie przeszło przez spread
    (marketable limit, czyli faktycznie taker), albo coś jest nie tak z zapisem.
  * `journal.decision.target_price` — cena z chwili decyzji, jedyna dostępna dla
    zlecenia rynkowego. To DOKŁADNIE to, co modeluje `POSLIZG_TAKER`: koszt
    przejścia przez spread między chwilą decyzji a chwilą wykonania.

Brakuje trzeciej — nie ma jej i to trzeba powiedzieć wprost: ceny mid z arkusza
w chwili składania zlecenia. Bez niej nie da się rozdzielić poślizgu na spread
i na ruch rynku w czasie oczekiwania. Mierzymy więc SUMĘ obu, i to jest liczba
właściwa dla wyceny strategii — bo to ona jest faktycznie płacona.

ZNAK: DODATNI ZAWSZE ZNACZY „NA NASZĄ NIEKORZYŚĆ"

Przy kupnie poślizg jest dodatni, gdy zapłaciliśmy więcej niż cena odniesienia;
przy sprzedaży — gdy dostaliśmy mniej. Bez tej normalizacji średnia z kupn
i sprzedaży kasowałaby się do zera przy dowolnie złej egzekucji.

PUŁAPKA, KTÓRA UNIEWAŻNIŁABY CAŁY POMIAR

Rachunek papierowy zapisuje cenę wypełnienia jako `cena * (1 ± POSLIZG)`.
Policzenie poślizgu z takich wierszy oddałoby więc DOKŁADNIE `POSLIZG_TAKER`
— i wyglądałoby jak świetne potwierdzenie założenia, będąc wyłącznie jego echem.
Dlatego mierzymy wyłącznie wiersze z `raw->>'execution' = 'gielda'`, a filtr
stoi w DWÓCH miejscach: w zapytaniu i w czystej funkcji. Zapytanie da się
przypadkiem poprawić, funkcja jest testowana.

CZEGO TEN MODUŁ NIE MIERZYŁ DO 2026-09-09 — I DLACZEGO TO WAŻNE PRZY CZYTANIU
STARYCH WYNIKÓW

Wyjść. `zapisz_wyjscie` nie zakładało wiersza w `exec.fill` (aktualizowało
wyłącznie `exec.position`), więc w rejestrze wypełnień były SAME WEJŚCIA:
18 pozycji zamkniętych, 18 wierszy `exec.fill`. Poślizg wyjścia był wtedy
niemierzalny — była to luka w zapisie, nie wynik pomiaru.

Zapis jest naprawiony i wyjścia trafiają tu na równi z wejściami. Ale wszystko,
co zmierzono PRZED tą datą, opisuje wyłącznie nogę wejściową, więc nie wolno
porównywać takiej liczby z nowszą jako „poprawy" ani „pogorszenia": zmieniła się
próba, nie egzekucja. Runda ma dwie nogi i dopiero od teraz obie są w próbie.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.engine.egzekucja import rozjazd_z_ulamkow  # noqa: E402
from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER  # noqa: E402
from services.engine.statystyka import przedzial_sredniej  # noqa: E402

# Jedyny zapis, ktory jest POMIAREM. Wszystko inne to nasze wlasne zalozenie
# zapisane do bazy i odczytane z powrotem.
ZRODLO_MIERZALNE = "gielda"

# Zalozenie, ktore ten pomiar ma sprawdzic. IMPORTOWANE, nie przepisane -
# przepisana kopia rozjechalaby sie przy pierwszej zmianie stalej, a wtedy
# pomiar porownywalby sie z nieaktualnym zalozeniem i milczal.
ZALOZENIE: dict[str, float] = {"maker": POSLIZG_MAKER, "taker": POSLIZG_TAKER}

# Werdykty. Nazwane, bo to na nich stoi rozroznienie „nie zmierzono" od
# „zmierzono i wyszlo tyle" - i to one, a nie liczby, sa czytane przez czlowieka.
NIE_ZMIERZONO = "nie zmierzono"
ZA_MALO = "za malo obserwacji"
NIEODROZNIALNE = "nieodroznialne od zalozenia"
ROZBIEZNE = "zalozenie poza przedzialem"


def rodzaj_zlecenia(order_type: str | None) -> str:
    """'maker' | 'taker' | 'nieznany' — po typie zlecenia z `exec.order`.

    OBIE STALE DOTYCZA CZEGOS INNEGO, wiec wspolna srednia nie mialaby sensu:
    zlecenie z limitem nie przechodzi przez spread (zalozenie: zero), rynkowe
    przechodzi zawsze (zalozenie: 4 pb). Zmieszanie ich daloby liczbe, ktorej
    nie da sie zestawic z zadna z nich.

    Typ spoza pary limit/market NIE JEST ZGADYWANY. Trafia do „nieznany"
    i wypada z pomiaru z nazwanym powodem — zaliczenie go na chybil trafil do
    makera porownaloby go z zalozeniem zero i wygladalo na katastrofe.

    OGRANICZENIE, KTORE TRZEBA ZNAC: zlecenie z limitem zlozone po drugiej
    stronie spreadu wypelnia sie natychmiast i placi stawke TAKER, a my
    zaliczymy je do makera. Widac to po wyniku - dodatni poslizg na zleceniu
    z limitem jest wlasnie tym objawem, bo limit gwarantuje cene albo lepsza.
    """
    t = (order_type or "").strip().lower()
    if t == "limit":
        return "maker"
    if t == "market":
        return "taker"
    return "nieznany"


def cena_odniesienia(w: dict) -> tuple[Decimal | None, str]:
    """Cena, na ktorej oparta byla decyzja. Zwraca (cena, skad).

    Kolejnosc nie jest dowolna. `limit_price` jest zobowiazaniem zlozonym
    gieldzie i to wzgledem niego mierzy sie jakosc wypelnienia zlecenia
    z limitem. `target_price` z dziennika decyzji jest zapasem dla zlecen bez
    limitu - i jedyna cena odniesienia, jaka wtedy istnieje.
    """
    for pole, skad in (("cena_planu", "plan"), ("cena_decyzji", "decyzja")):
        v = w.get(pole)
        if v is None:
            continue
        try:
            c = Decimal(str(v))
        except (InvalidOperation, ValueError):
            continue
        if c > 0:
            return c, skad
    return None, "brak"


def poslizg_wiersza(w: dict) -> tuple[float | None, str]:
    """Poślizg jednego wypełnienia, jako ułamek ceny odniesienia. CZYSTA FUNKCJA.

    Zwraca `(poślizg, skąd_odniesienie)` albo `(None, powód_odrzucenia)`.

    ODRZUCENIE NIE JEST ZEREM. Wiersz, którego nie da się zmierzyć, wypada
    z próby i jest policzony osobno wraz z powodem. Wliczenie go jako zera
    ściągałoby średnią do zera tym mocniej, im więcej rzeczy nie działa —
    czyli zepsuty zapis wyglądałby jak doskonała egzekucja.
    """
    if (w.get("zrodlo") or "") != ZRODLO_MIERZALNE:
        return None, ("zapis nie z giełdy — mierzyłby własne założenie, "
                      "bo rachunek papierowy wylicza cenę wprost z POSLIZG_*")
    strona = (w.get("strona") or "").strip().lower()
    if strona not in ("buy", "sell"):
        return None, f"nieznana strona zlecenia ({w.get('strona')!r})"
    surowa = w.get("cena_wykonania")
    if surowa is None:
        return None, "brak ceny wykonania w exec.fill"
    try:
        wykonana = Decimal(str(surowa))
    except (InvalidOperation, ValueError):
        return None, f"cena wykonania nie jest liczbą ({surowa!r})"
    if wykonana <= 0:
        return None, "cena wykonania niedodatnia"

    odniesienie, skad = cena_odniesienia(w)
    if odniesienie is None:
        return None, ("brak ceny odniesienia — ani planu zlecenia, "
                      "ani target_price w dzienniku decyzji")

    # Dodatni = na nasza niekorzysc, po obu stronach rynku. Bez tego znaku
    # kupna i sprzedaze kasowalyby sie w sredniej przy dowolnie zlej egzekucji.
    znak = Decimal(1) if strona == "buy" else Decimal(-1)
    return float(znak * (wykonana - odniesienie) / odniesienie), skad


def _proba(rodzaj: str, xs: list[float]) -> dict:
    """Statystyki jednej próby razem z werdyktem wobec założenia."""
    zalozenie = ZALOZENIE[rodzaj]
    if not xs:
        # NIE ZMIERZONO to nie to samo co ZMIERZONO ZERO i musi wygladac inaczej.
        # Zero w tym miejscu czytaloby sie jako „poslizgu nie ma", czyli jako
        # potwierdzenie zalozenia makera - z zera obserwacji.
        return {"rodzaj": rodzaj, "n": 0, "srednia": None, "mediana": None,
                "odchylenie": None, "przedzial": None,
                "zalozenie": zalozenie, "werdykt": NIE_ZMIERZONO}
    n = len(xs)
    przedzial = przedzial_sredniej(xs)
    return {
        "rodzaj": rodzaj,
        "n": n,
        "srednia": statistics.fmean(xs),
        # Mediana obok sredniej, bo jedno wypelnienie w cienkim arkuszu potrafi
        # przesunac srednia o wiecej niz cala mierzona wielkosc.
        "mediana": statistics.median(xs),
        "odchylenie": statistics.stdev(xs) if n > 1 else None,
        "przedzial": przedzial,
        "zalozenie": zalozenie,
        "werdykt": werdykt(n, przedzial, zalozenie),
    }


def werdykt(n: int, przedzial: tuple[float, float] | None,
            zalozenie: float) -> str:
    """Czy pomiar podważa założenie. CZYSTA FUNKCJA.

    PROGIEM JEST PRZEDZIAŁ UFNOŚCI, A NIE LICZBA DOBRANA NA OKO. To ta sama
    reguła, którą dozór stosuje do odsetka okien dodatnich, i z tego samego
    powodu: bez niej różnica dwóch punktów bazowych przy trzech obserwacjach
    wyglądałaby jak odkrycie.

    Trzy stany, nie dwa — i to jest tu istota:

      * `nie zmierzono` — próba pusta; o założeniu nie wiemy NIC,
      * `za mało obserwacji` — jedno wypełnienie. Ma cenę i ma poślizg, ale
        nie ma rozrzutu, więc nie jest pomiarem poślizgu; jest anegdotą,
      * `nieodróżnialne od założenia` — zmierzone, przedział obejmuje stałą.
        To NIE znaczy „założenie potwierdzone" przy szerokim przedziale;
        znaczy „nie mamy dość danych, żeby je odrzucić",
      * `założenie poza przedziałem` — dopiero to jest powodem, żeby stałą
        ruszyć, i dopiero wtedy jest czym ją zastąpić.
    """
    if n == 0:
        return NIE_ZMIERZONO
    if przedzial is None:
        return ZA_MALO
    lo, hi = przedzial
    return NIEODROZNIALNE if lo <= zalozenie <= hi else ROZBIEZNE


def statystyki(wiersze: list[dict]) -> dict:
    """Poślizg per rodzaj zlecenia, z listy wierszy. CZYSTA FUNKCJA — bez bazy.

    Rozdzielenie rachunku od pobierania jest tu warunkiem sprawdzalności:
    inaczej test tej logiki wymagałby bazy z wypełnieniami, a wypełnień
    jeszcze nie ma ani jednego — czyli narzędzie do ich mierzenia nie dałoby
    się sprawdzić przed pierwszym z nich. A po pierwszym byłoby za późno:
    właśnie ono jest tą obserwacją, której nie chcemy stracić.
    """
    proby: dict[str, list[float]] = {"maker": [], "taker": []}
    zrodla: dict[str, int] = {}
    pominiete: dict[str, int] = {}

    for w in wiersze:
        rodzaj = rodzaj_zlecenia(w.get("rodzaj_zlecenia"))
        poslizg, info = poslizg_wiersza(w)
        if poslizg is None:
            pominiete[info] = pominiete.get(info, 0) + 1
            continue
        if rodzaj == "nieznany":
            powod = (f"nieznany typ zlecenia ({w.get('rodzaj_zlecenia')!r}) — "
                     f"nie wiadomo, z którym założeniem to zestawić")
            pominiete[powod] = pominiete.get(powod, 0) + 1
            continue
        proby[rodzaj].append(poslizg)
        zrodla[info] = zrodla.get(info, 0) + 1

    return {
        "maker": _proba("maker", proby["maker"]),
        "taker": _proba("taker", proby["taker"]),
        # Skad wzieto cene odniesienia. Widoczne, bo pomiar wzgledem limitu
        # i wzgledem ceny decyzji odpowiadaja na dwa rozne pytania.
        "odniesienia": zrodla,
        "pominiete": pominiete,
    }


def ulamki(wiersze: list[dict]) -> dict:
    """Ułamek wypełnienia z zapisanych wierszy. CZYSTA FUNKCJA.

    Symulacja przyjmuje 1.0 dla każdego zlecenia, które dotknęło limitu, więc
    średnia z ułamków faktycznych jest bezpośrednią miarą tego, o ile backtest
    zawyża wielkość pozycji.

    Rachunek jest TEN SAM co w cyklu — `rozjazd_z_ulamkow` z `egzekucja.py`.
    Ułamek liczy się tam z rozstrzygnięć trzymanych w pamięci i przepada wraz
    z procesem; tutaj odczytujemy go z `exec.order.raw`, gdzie go zapisano.
    Dwie implementacje tej samej średniej rozjechałyby się przy pierwszej
    zmianie, i to niewidocznie.

    Zlecenia o rodzaju `brak` nie mają wiersza w `exec.fill` i nie mogą się tu
    pojawić — próba jest więc z natury warunkowana na „coś weszło". To jest
    zgodne z pytaniem: ułamek zerowy jest niewypełnieniem, a nie wypełnieniem
    zerowej wielkości, i `rozjazd_z_ulamkow` odsiewa go tak samo.
    """
    pary: list[tuple[str, Decimal]] = []
    bez_ulamka = 0
    for w in wiersze:
        if (w.get("zrodlo") or "") != ZRODLO_MIERZALNE:
            continue
        surowy = w.get("ulamek")
        if surowy is None:
            bez_ulamka += 1
            continue
        try:
            u = Decimal(str(surowy))
        except (InvalidOperation, ValueError):
            bez_ulamka += 1
            continue
        pary.append((w.get("rodzaj_wypelnienia") or "pelne", u))
    return rozjazd_z_ulamkow(pary) | {"bez_ulamka": bez_ulamka}


def pomiar(wiersze: list[dict]) -> dict:
    """Cały pomiar naraz: poślizg per rodzaj + ułamek wypełnienia."""
    return statystyki(wiersze) | {"ulamek": ulamki(wiersze)}


# ------------------------------------------------------------------ wypisanie

def _bp(x: float | None, znak: bool = True) -> str:
    """Punkty bazowe. Myslnik, gdy liczby NIE MA - zero byloby klamstwem.

    Znak jest wypisywany przy wielkosciach, ktore go maja (poslizg: dodatni to
    koszt, ujemny to zysk), i pomijany przy rozrzucie - odchylenie ze znakiem
    sugerowaloby kierunek, ktorego nie ma.
    """
    if x is None:
        return "—"
    return f"{x * 1e4:+.2f} pb" if znak else f"{x * 1e4:.2f} pb"


def opis(p: dict) -> list[str]:
    """Pomiar zamieniony na wiersze do wypisania.

    KAŻDY WIERSZ NIESIE LICZBĘ OBSERWACJI. Średnia bez `n` jest w tym miejscu
    najgorszym możliwym skrótem: przy trzech wypełnieniach wygląda dokładnie
    tak samo jak przy trzystu, a prowadzi do zupełnie innej decyzji.
    """
    linie = []
    for rodzaj in ("maker", "taker"):
        s = p[rodzaj]
        etykieta = f"poślizg {rodzaj} (założenie {_bp(s['zalozenie'])})"
        if s["werdykt"] == NIE_ZMIERZONO:
            linie.append(f"{etykieta}: n=0 — NIE ZMIERZONO. Stała pozostaje "
                         f"założeniem; to nie jest pomiar zerowego poślizgu.")
            continue
        przedzial = ("—" if s["przedzial"] is None
                     else f"[{_bp(s['przedzial'][0])}, {_bp(s['przedzial'][1])}]")
        linie.append(
            f"{etykieta}: n={s['n']}, mediana {_bp(s['mediana'])}, "
            f"średnia {_bp(s['srednia'])}, odch. {_bp(s['odchylenie'], znak=False)}, "
            f"przedział 95% {przedzial} — {s['werdykt'].upper()}")

    u = p["ulamek"]
    if u["n"] == 0:
        linie.append("ułamek wypełnienia: n=0 — NIE ZMIERZONO. Symulacja nadal "
                     "przyjmuje 1.0 i zawyża wielkość pozycji o nieznaną wartość.")
    else:
        przedzial = ("—" if u["przedzial"] is None
                     else f"[{u['przedzial'][0]:.3f}, {u['przedzial'][1]:.3f}]")
        odch = "—" if u["odchylenie"] is None else f"{u['odchylenie']:.3f}"
        linie.append(
            f"ułamek wypełnienia: n={u['n']}, średnio {float(u['sredni_ulamek']):.3f}"
            f" (symulacja przyjmuje 1.000), odch. {odch}, "
            f"przedział 95% {przedzial}; częściowych {u['czesciowych']}, "
            f"nie do zamknięcia {u['nie_do_zamkniecia']}")

    for skad, n in sorted(p["odniesienia"].items()):
        linie.append(f"  cena odniesienia z: {skad} ({n})")
    for powod, n in sorted(p["pominiete"].items()):
        linie.append(f"  pominięto {n}: {powod}")
    if p["ulamek"]["bez_ulamka"]:
        linie.append(f"  bez zapisanego ułamka: {p['ulamek']['bez_ulamka']} "
                     f"— wypełnienie z giełdy bez `ulamek_wypelnienia` w raw")
    return linie


# -------------------------------------------------------------- warstwa danych

# Zapytanie zakotwiczone na `exec.fill`, NIE na `exec.order`. Jeden plan ma
# w `exec.order` DWA wiersze - ten ze zlozenia i ten z rozstrzygniecia - a oba
# dostaja to samo `raw` przez `raw = raw || meta`. Liczenie po zleceniach
# podwajaloby wiec kazda obserwacje, i to niewidocznie: srednia wyszlaby ta sama,
# a `n` dwa razy za duze, czyli przedzial ufnosci o 40% za waski.
ZAPYTANIE = """
SELECT o.order_type                     AS rodzaj_zlecenia,
       o.side                           AS strona,
       o.raw->>'execution'              AS zrodlo,
       o.raw->>'ulamek_wypelnienia'     AS ulamek,
       o.raw->>'rodzaj'                 AS rodzaj_wypelnienia,
       p.limit_price                    AS cena_planu,
       d.target_price                   AS cena_decyzji,
       f.price                          AS cena_wykonania,
       f.qty                            AS ilosc,
       f.event_time                     AS czas,
       i.symbol                         AS symbol
  FROM exec.fill f
  JOIN exec.order o           ON o.id = f.order_id
  JOIN ref.instrument i       ON i.id = o.instrument_id
  LEFT JOIN journal.planned_order p ON p.id = o.planned_order_id
  LEFT JOIN journal.decision d      ON d.id = o.decision_id
 WHERE o.engine::text = %(engine)s
   AND o.mode::text = %(mode)s
   AND (%(h)s::int IS NULL
        OR f.event_time > now() - make_interval(hours => %(h)s::int))
 ORDER BY f.event_time
"""


async def pobierz(fetch_all, *, engine: str = "crypto", mode: str = "demo",
                  okno_h: int | None = None) -> list[dict]:
    """Cienka warstwa: wypełnienia z bazy. Bez rachunku — ten jest wyżej.

    OKNO DOMYSLNIE PUSTE, czyli od poczatku. Kazde inne domyslne okno wyrzucaloby
    czesc jedynych obserwacji, jakie sa - przy probie rzedu kilku sztuk to nie
    jest oszczednosc, tylko utrata pomiaru.
    """
    return list(await fetch_all(ZAPYTANIE,
                                {"engine": engine, "mode": mode, "h": okno_h}))


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Zmierzony poślizg i ułamek wypełnienia — z prawdziwych "
                    "wypełnień. Nie zmienia żadnej stałej.")
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--okno-h", type=int, default=None, dest="okno_h",
                    help="okno w godzinach; domyslnie od poczatku")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, fetch_all, open_pool

    await open_pool()
    try:
        wiersze = await pobierz(fetch_all, engine=args.engine, mode=args.mode,
                                okno_h=args.okno_h)
        okno = "od poczatku" if args.okno_h is None else f"{args.okno_h} h"
        print(f"poślizg ({args.engine}/{args.mode}, {okno}), "
              f"wypełnień w rejestrze: {len(wiersze)}")
        for linia in opis(pomiar(wiersze)):
            print(f"  {linia}")
    finally:
        await close_pool()
    # ZAWSZE ZERO. To jest raport, a nie czujka - kod niezerowy zapalilby
    # zadanie cykliczne na czerwono, czyli podnioslby alarm bez tresci.
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
