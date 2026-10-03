#!/usr/bin/env python3
"""Jedna komenda -> tabela pieciu przesuniec okien z pliku panelu (audyt Fable T3).

    python scripts/eksperyment.py --panel panele_crypto_4h_20261003.json.gz \\
        --kind spot_eu --limit [--egzekucja-pesymistyczna --opoznienie 20] \\
        [--logi katalog/]

PO CO PIEC PRZESUNIEC. Werdykt pojedynczego przebiegu zalezy od tego, gdzie
wypadly granice okien: 27.09 piec przesuniec o dobe dalo trzy razy
`validated` i dwa razy `learning` przy identycznych nastawach (`docs/03`).
Wynik, ktory nie przetrwa przesuniecia siatki o dobe, nie jest wynikiem.

OD 2026-10-03 (audyt Fable T4) przesuniecia liczy SAM TRENER: siatka okien
zakotwiczona w `--od` (domyslnie `train.KOTWICA_SIATKI`), przesuniecia
`--przesuniecia` (domyslnie -2..+2 dni) w jednym przebiegu, status z minimum.
Ten skrypt wola `train.train(..., panel=plik, od=..., przesuniecia=...)` RAZ
i sklada tabele z `PODSUMOWANIE_JSON["przesuniecia"]`. `--od teraz` to siatka
sprzed T4 (start = koniec panelu - days + przesuniecie), czyli dokladnie
pomiar z 27.09 i H-1 (`--days` 1400 z przesunieciami -2..+2 = dawne
`--days` 1402..1398).

Plik czytany raz, zero polaczen z baza. Pelny wydruk trenera trafia do
`--logi` (jesli podane), tabela i werdykt STOP/GO 1 na ekran.

KRYTERIUM STOP/GO 1 (docs/hipotezy.md, H-1) jest tu wypisane, nie wybrane:
zwrot na runde po doplacie > 0 w >= 3/5 przesuniec I bije B2 przy tej samej
ekspozycji w >= 60% okien (zbiorczo).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.engine import panel_plik  # noqa: E402
from services.engine import train as trener  # noqa: E402
from services.engine.horizons import HORIZONS, get as get_horizon  # noqa: E402
from services.engine.portfolio import Egzekucja  # noqa: E402

PROG_PRZESUNIEC = 3       # zwrot > 0 w co najmniej tylu z pieciu
PROG_B2 = 0.60            # bije B2 w co najmniej takim odsetku okien


def _f(x, fmt: str, brak: str = "—") -> str:
    return brak if x is None else format(x, fmt)


def tabela(wyniki: list[dict]) -> list[str]:
    linie = ["| przesunięcie | okna | dodatnie | transakcji | zwrot/rundę po dopłacie (pb) "
             "| DSR | wypełnialność | bije B1 | bije B2 | bije B3 | status |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    def bije(w, k):
        pob, por = w[k]
        return f"{pob}/{por} ({pob / por:.0%})" if por else "—"

    for w in wyniki:
        linie.append(
            f"| {w['przesuniecie']} | {w['okna']} | "
            f"{w['dodatnie']}/{w['okna']} ({w['dodatnie'] / w['okna']:.0%}) | "
            f"{w['transakcji']} | {_f(w['zwrot_na_runde_pb'], '+.2f')} | "
            f"{_f(w['dsr'], '.4f')} | {_f(w['wypelnialnosc'], '.1%')} | "
            f"{bije(w, 'bije_b1')} | {bije(w, 'bije_b2')} | {bije(w, 'bije_b3')} | {w['status']} |"
            if w["okna"] else f"| {w['przesuniecie']} | 0 | — | — | — | — | — | — | — | — | {w['status']} |")
    return linie


def tabela_odniesien(wyniki: list[dict]) -> list[str]:
    """Strategia obok B1-B3 w tych samych oknach: sredni zwrot w oknie (%
    kapitalu), srednia ekspozycja, okna z zyskiem - srednio po przesunieciach."""
    linie = ["| | średni zwrot w oknie (% kapitału) | średnia ekspozycja | okna z zyskiem |",
             "|---|---|---|---|"]
    for nazwa in ("strategia", "B1", "B2", "B3"):
        v = [w["odniesienia"][nazwa] for w in wyniki
             if w.get("odniesienia", {}).get(nazwa)]
        if not v:
            linie.append(f"| {nazwa} | — | — | — |")
            continue
        zysk, okna = sum(x[2] for x in v), sum(x[3] for x in v)
        linie.append(f"| {nazwa} | {sum(x[0] for x in v) / len(v):+.3f} | "
                     f"{sum(x[1] for x in v) / len(v):.1%} | {zysk}/{okna} ({zysk / okna:.0%}) |")
    return linie


def werdykt(wyniki: list[dict]) -> tuple[bool, str]:
    dodatnie = sum(1 for w in wyniki
                   if w["zwrot_na_runde_pb"] is not None and w["zwrot_na_runde_pb"] > 0)
    pob = sum(w["bije_b2"][0] for w in wyniki)
    por = sum(w["bije_b2"][1] for w in wyniki)
    udzial = pob / por if por else 0.0
    go = dodatnie >= PROG_PRZESUNIEC and por > 0 and udzial >= PROG_B2
    return go, (f"zwrot/rundę > 0 w {dodatnie}/{len(wyniki)} przesunięć "
                f"(próg {PROG_PRZESUNIEC}); bije B2 w {pob}/{por} okien "
                f"({udzial:.0%}, próg {PROG_B2:.0%}) → {'GO' if go else 'STOP'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--panel", required=True)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--bar", default="4h", choices=sorted(HORIZONS))
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--od", default=f"{trener.KOTWICA_SIATKI:%Y-%m-%d}",
                    help="kotwica siatki okien; `teraz` = siatka od konca panelu - days")
    ap.add_argument("--przesuniecia",
                    default=",".join(str(p) for p in trener.PRZESUNIECIA_SIATKI),
                    help="przesuniecia siatki w dniach (z 0); ujemne jako "
                         "--przesuniecia=-2,-1,0")
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--kind", default="spot_eu")
    ap.add_argument("--limit", action="store_true")
    ap.add_argument("--wybor", default="ic", choices=("argmax", "ic", "bazowe"))
    ap.add_argument("--egzekucja-pesymistyczna", action="store_true", dest="pesymistyczna")
    ap.add_argument("--opoznienie", type=int, default=0, dest="opoznienie_min")
    ap.add_argument("--spread-bps", type=float, default=None, dest="spread_bps")
    ap.add_argument("--logi", default="", help="katalog na pelne wydruki trenera")
    ap.add_argument("--hipoteza", required=True,
                    help="numer z docs/hipotezy.md, ktory ten pomiar rozstrzyga")
    args = ap.parse_args(argv)
    blad = trener.sprawdz_hipoteze(args.hipoteza)
    if blad:
        sys.exit(blad)
    if (args.pesymistyczna or args.opoznienie_min) and not args.limit:
        sys.exit("--egzekucja-pesymistyczna i --opoznienie wymagaja --limit")

    plik = panel_plik.wczytaj(args.panel)
    h = get_horizon(args.bar)
    egz = Egzekucja(limit=args.limit, opoznienie_min=args.opoznienie_min,
                    pesymistyczna=args.pesymistyczna)
    logi = Path(args.logi) if args.logi else None
    if logi:
        logi.mkdir(parents=True, exist_ok=True)
    wariant = (f"{args.kind}{' limit' if args.limit else ''}"
               f"{' pesymistyczna' if args.pesymistyczna else ''}"
               f"{f' opóźnienie {args.opoznienie_min} min' if args.opoznienie_min else ''}")
    print(f"Panel {args.panel}: {plik['engine']}/{plik['bar']} do "
          f"{plik['do']:%Y-%m-%d %H:%M} UTC; wariant: {wariant}; wybór {args.wybor}")

    od = (None if args.od == "teraz"
          else datetime.fromisoformat(args.od).replace(tzinfo=timezone.utc))
    przesuniecia = tuple(sorted({int(p) for p in args.przesuniecia.split(",") if p.strip()}))
    wydruk = io.StringIO()
    with contextlib.redirect_stdout(wydruk):
        try:
            calosc = trener.train(None, args.engine, args.days, args.trials, h, args.kind,
                                  egz, sucho=True, wybor=args.wybor,
                                  spread_bps=args.spread_bps, panel=plik,
                                  od=od, przesuniecia=przesuniecia,
                                  hipoteza=args.hipoteza)
        except SystemExit as e:
            calosc = {"okna": 0, "status": f"przerwany ({e.code})"}
    if logi:
        (logi / f"trener_d{args.days}.txt").write_text(wydruk.getvalue(), encoding="utf-8")
    po = calosc.get("przesuniecia") or ({"+0": calosc} if calosc.get("okna") else {})
    wyniki = [w | {"przesuniecie": p} for p, w in sorted(po.items(), key=lambda x: int(x[0]))]
    if not wyniki:
        wyniki = [{"przesuniecie": "—", "okna": 0, "status": calosc["status"]}]
    print(f"  status przebiegu (z minimum po przesunieciach): {calosc['status']}",
          file=sys.stderr)

    for linia in tabela(wyniki):
        print(linia)
    pelne = [w for w in wyniki if w.get("okna")]
    if pelne:
        print()
        for linia in tabela_odniesien(pelne):
            print(linia)
        print(werdykt(pelne)[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
