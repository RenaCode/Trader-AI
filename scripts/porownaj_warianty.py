#!/usr/bin/env python3
"""Porownanie dwoch wariantow trenera: test PAROWY po oknach, nie naglowki.

    python scripts/porownaj_warianty.py log_A.txt log_B.txt
    kubectl logs job/pomiar -n default | python scripts/porownaj_warianty.py -
        (jeden log z dwoma sekcjami `===== ... =====`)

DLACZEGO TO ISTNIEJE

Raport trenera podaje Sharpe'a ze ZLACZONEJ PROBY wszystkich transakcji OOS
i z niego liczy sie DSR, czyli bramka promocji. Ta liczba wazy LICZBA
TRANSAKCJI, wiec wariant, ktory handluje wiecej, moze ja podniesc NIE
POPRAWIAJAC ANI JEDNEGO OKNA.

Zmierzone 2026-09-27, `--progi wspolny` kontra `--progi per-rezim` (oba
`--sucho`, te same okna, `--wybor ic`, czyli deterministycznie):

    ze zlaczonej proby:   +0,1004 -> +0,1114  (+11%),  DSR 0,7887 -> 0,8985
    PAROWO po 36 oknach:  srednia roznica +0,0051, t = +0,20, lepszych 20/36
    transakcji:           +6,8%

Czyli zero. Skok DSR o 0,11 to 0,4 bledu standardowego Sharpe'a
(1/sqrt(1500) = 0,0254). Sam naglowek czytalby sie jako poprawa algorytmu.

PULAPKA WE WZORCU, KTORA KOSZTOWALA ZMIANE ZNAKU WYNIKU

Numery okien sa wyrownane do dwoch znakow (`okno  0`, `okno 10`), wiec wzorzec
`okno (\\d+)` gubi okna 0-9. Przy pierwszym podejsciu zgubil dokladnie te
dziesiec i oddal srednia roznice -0,0101 zamiast +0,0051, czyli PRZECIWNY
WNIOSEK. Wykryla to niezgodnosc liczby sparsowanych okien z liczba, ktora podal
trener (29 wobec 38) - nie oglad wzorca. Dlatego ten skrypt:

  * parsuje `okno\\s+(\\d+)`,
  * NAZYWA liczbe okien, ktore znalazl, oraz liczbe z podsumowania trenera,
  * PADA, gdy te dwie liczby sie nie zgadzaja.

CZEGO TEN SKRYPT NIE ROBI: nie orzeka o rezimach. Rozbicie po nich to cztery
porownania naraz i przy n rzedu pieciu okien nie jest ustaleniem - liczby sa
wypisane, ale opatrzone tym zdaniem, a nie gwiazdka.

OKNA MUSZA BYC ROZLACZNE, zeby efektywne n rownalo sie n. Jest tak, gdy
`step_days == test_days` (tor 4h: 30 i 30). Gdy nie jest - patrz pamiec
"Okna walk-forward nie sa niezalezne": okna nakladaja sie o 29 z 30 dni
i zawyzenie proby siega 17,8x.
"""
from __future__ import annotations

import math
import re
import statistics
import sys
from collections import defaultdict

# `okno\s+` a NIE `okno `: numery jednocyfrowe sa wyrownane dwiema spacjami.
WZOR_OKNA = re.compile(
    r"okno\s+(\d+)\s+(\S+)\s+Sharpe/transakcję=([+-][\d.]+)"
    r"\s+transakcje=(\d+)\s+reżim=(\w+)(.*)")
# "Dodatnie 24/38 okien out-of-sample" - liczba, ktora podal SAM trener.
WZOR_SUMY = re.compile(r"Dodatnie (?:tylko )?(\d+)/(\d+) okien")
WZOR_SEKCJI = re.compile(r"^===== (.*?) =====$", re.M)


def okna(blok: str) -> dict[str, tuple[float, int, str]]:
    """Okna OCENIANE, po okresie testowym. Pominiete w ocenie odpadaja.

    Kluczem jest OKRES, nie numer: dwa przebiegi o roznej liczbie okien mialyby
    inne numery dla tego samego kawalka kalendarza.
    """
    out: dict[str, tuple[float, int, str]] = {}
    for m in WZOR_OKNA.finditer(blok):
        _, okres, sharpe, n, rezim, ogon = m.groups()
        if "pominięte w ocenie" in ogon:
            continue
        out[okres] = (float(sharpe), int(n), rezim)
    return out


def ile_okien_podal_trener(blok: str) -> int | None:
    m = WZOR_SUMY.search(blok)
    return int(m.group(2)) if m else None


def sekcje(tekst: str) -> dict[str, str]:
    """Log z kilkoma przebiegami rozdzielonymi `===== nazwa =====`."""
    czesci = WZOR_SEKCJI.split(tekst)
    if len(czesci) == 1:
        return {"(caly log)": tekst}
    return {czesci[i].strip(): czesci[i + 1] for i in range(1, len(czesci), 2)}


def sprawdz_kompletnosc(nazwa: str, blok: str, znalezione: int) -> None:
    """Rozbieznosc z liczba trenera PRZERYWA, a nie ostrzega.

    Niepelny parsing daje wynik wygladajacy poprawnie - i to on zmienil tu raz
    znak sredniej roznicy. Ostrzezenie w takim miejscu zostaje przeczytane po
    tym, jak liczba zostala podana dalej.
    """
    podane = ile_okien_podal_trener(blok)
    if podane is None:
        print(f"  UWAGA {nazwa}: nie znalazlem podsumowania 'Dodatnie N/M okien'"
              f" — kompletnosci parsowania NIE sprawdzono", file=sys.stderr)
        return
    if podane != znalezione:
        raise SystemExit(
            f"{nazwa}: sparsowalem {znalezione} okien, a trener podal {podane}."
            f" Wzorzec gubi wiersze — wynik liczony na takiej probie bywa"
            f" PRZECIWNY prawdziwemu (zmierzone 2026-09-27: -0,0101 zamiast"
            f" +0,0051 po zgubieniu okien 0-9).")


def porownaj(nazwa_a: str, a: dict, nazwa_b: str, b: dict) -> None:
    wspolne = sorted(set(a) & set(b))
    print(f"\n{nazwa_b}  MINUS  {nazwa_a}")
    print(f"  okien: {len(a)} / {len(b)}, w OBU {len(wspolne)}")
    for etykieta, brak in (("tylko " + nazwa_a, sorted(set(a) - set(b))),
                           ("tylko " + nazwa_b, sorted(set(b) - set(a)))):
        if brak:
            print(f"  {etykieta}: {', '.join(brak)}")
    if len(wspolne) < 3:
        raise SystemExit("za malo wspolnych okien, zeby cokolwiek porownac")

    roznice = [b[o][0] - a[o][0] for o in wspolne]
    sr = statistics.fmean(roznice)
    sd = statistics.stdev(roznice)
    t = sr / (sd / math.sqrt(len(roznice))) if sd > 0 else float("inf")
    df = len(roznice) - 1
    print(f"  srednia roznica Sharpe/transakcje: {sr:+.4f}   sd {sd:.4f}")
    print(f"  t parowe: {t:+.2f} przy df={df}  (|t| > ~2,0 to p < 0,05)")
    print(f"  okien lepszych: {sum(1 for d in roznice if d > 0)}/{len(roznice)}")
    print(f"  mediana: {statistics.median(a[o][0] for o in wspolne):+.4f}"
          f" -> {statistics.median(b[o][0] for o in wspolne):+.4f}")
    na = sum(a[o][1] for o in wspolne)
    nb = sum(b[o][1] for o in wspolne)
    print(f"  transakcji: {na} -> {nb} ({(nb / na - 1) * 100:+.1f}%)"
          if na else f"  transakcji: {na} -> {nb}")
    print(f"  okien dodatnich: {sum(1 for o in wspolne if a[o][0] > 0)}"
          f" -> {sum(1 for o in wspolne if b[o][0] > 0)} z {len(wspolne)}")

    po_rez: dict[str, list[float]] = defaultdict(list)
    for o in wspolne:
        po_rez[a[o][2]].append(b[o][0] - a[o][0])
    if len(po_rez) > 1:
        print("  po rezimach (rezim okna z wariantu pierwszego):")
        for rez, lista in sorted(po_rez.items(), key=lambda x: -len(x[1])):
            if len(lista) > 1 and statistics.stdev(lista) > 0:
                tr = (statistics.fmean(lista)
                      / (statistics.stdev(lista) / math.sqrt(len(lista))))
                print(f"    {rez:<12} n={len(lista):>2}"
                      f"  srednia {statistics.fmean(lista):+.4f}  t {tr:+.2f}")
            else:
                print(f"    {rez:<12} n={len(lista):>2}"
                      f"  srednia {statistics.fmean(lista):+.4f}  t —")
        print("    (to CZTERY porownania naraz i n rzedu pieciu okien"
              " — nie jest to ustalenie)")


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        blok = sys.stdin.read() if argv[0] == "-" else open(
            argv[0], encoding="utf-8").read()
        s = sekcje(blok)
        if len(s) < 2:
            raise SystemExit("jeden log bez sekcji `===== ... =====`:"
                             " podaj dwa pliki albo log z dwoma przebiegami")
        nazwy = list(s)
    elif len(argv) == 2:
        # Drugi odczyt stdin jest pusty - `- -` dawalo "zostal jeden przebieg".
        if argv.count("-") > 1:
            raise SystemExit("stdin można podać raz: dwa przebiegi z jednego "
                             "wejścia to jeden `-` i log z sekcjami `===== ... =====`")
        s = {}
        nazwy = []
        for i, sciezka in enumerate(argv):
            tresc = sys.stdin.read() if sciezka == "-" else open(
                sciezka, encoding="utf-8").read()
            nazwa = sciezka if sciezka != "-" else f"wejscie {i + 1}"
            s[nazwa] = tresc
            nazwy.append(nazwa)
    else:
        raise SystemExit(__doc__)

    zebrane = {}
    for nazwa in nazwy:
        zebrane[nazwa] = okna(s[nazwa])
        sprawdz_kompletnosc(nazwa, s[nazwa], len(zebrane[nazwa]))
        print(f"{nazwa}: {len(zebrane[nazwa])} okien ocenianych")

    # PRZEBIEG BEZ OKIEN WYPADA Z POROWNANIA, A NIE PRZERYWA GO.
    #
    # Log pomiaru czesto zawiera tor, ktory sie NIE URUCHOMIL (kod 3 - np.
    # equity bez potwierdzonej korekty o podzialy). Zestawiany parami po
    # kolei, taki przebieg stawal miedzy dwoma wariantami, ktore mialy byc
    # porownane ze soba, i przerywal caly skrypt na "za malo wspolnych okien".
    puste = [n for n in nazwy if not zebrane[n]]
    for n in puste:
        print(f"  pomijam {n}: zero ocenianych okien (tor nieuruchomiony"
              f" albo przerwany)")
    nazwy = [n for n in nazwy if zebrane[n]]
    if len(nazwy) < 2:
        raise SystemExit("zostal jeden przebieg z oknami — nie ma czego porownac")

    for i in range(len(nazwy) - 1):
        porownaj(nazwy[i], zebrane[nazwy[i]], nazwy[i + 1], zebrane[nazwy[i + 1]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
