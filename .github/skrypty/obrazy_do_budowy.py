#!/usr/bin/env python3
"""Ktore obrazy zbudowac: te, ktorych kod rozni sie od kodu ich obecnego tagu.

    python .github/skrypty/obrazy_do_budowy.py --glowa <sha> [--wszystkie]

Wypisuje `api=true|false` i `web=true|false` (takze do $GITHUB_OUTPUT).

DLACZEGO (2026-10-14)

Do 14.10 job `changes` pytal filtr sciezek (dorny/paths-filter), co zmienil
OSTATNI push. PR #28 zmienil web, ale jego przebieg padl na tescie dymnym, wiec
obrazu web nie zbudowano. Nastepny push (#29) zmienial tylko CI - filtr nie
widzial web, build go pominal, a `values.yaml` zostal przy `web: sha-7ecd3ba`,
choc kod web byl nowszy. `galaz_deploy.py` slusznie odmowil przesuniecia
`deploy` (kod rozni sie od zrodla tagu) i po limicie czekania sie poddal.
Pomogl dopiero reczny `workflow_dispatch`.

REGULA: OBRAZ BUDUJE SIE, GDY `galaz_deploy.niezgodnosc_obrazu` GO ODRZUCA -
kod pod HEAD rozni sie od commita S z tagu `sha-S`, tag nie wskazuje commita,
commitu S nie ma w repozytorium albo obrazu nie ma w rejestrze. Ta sama
funkcja decyduje o `deploy`, wiec nie ma stanu, w ktorym `deploy` czeka na
obraz, ktorego zaden przebieg nie zbuduje. Nieudany przebieg nie gubi juz
zmiany: kolejny push widzi ja dalej, bo tag dalej wskazuje stary kod.

DODATKOWO dla api: zmiana samego workflow albo jego skryptow wzgledem
zrodla tagu api przebudowuje api. Bez tego edycja CI konczylaby sie
zielonym przebiegiem z pominietym buildem i podbiciem - czyli niesprawdzona
(tak przezyl 11 dni krok wolajacy usuniety plik). Do `deploy` te sciezki
nie wchodza: obraz ich nie zawiera.

BLAD REJESTRU (siec, 5xx) = BUDUJ. Zbedny build kosztuje minuty, pominiety -
produkcje stojaca na starym kodzie bez alarmu.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import galaz_deploy as gd  # noqa: E402

# Sciezki, ktorych zmiana wymaga przebiegu calego toru (build, podbicie,
# deploy), choc obraz ich nie zawiera.
WYZWALACZE_CI = {
    "api": [".github/workflows/docker-publish.yml", ".github/skrypty"],
    "web": [],
}


def do_budowy(glowa: str, w_rejestrze=gd.obraz_w_rejestrze,
              wszystkie: bool = False) -> dict[str, str | None]:
    """{usluga: powod budowy albo None}. CZYSTA wzgledem repozytorium w cwd."""
    if wszystkie:
        return {u: "workflow_dispatch: buduje wszystkie obrazy" for u in gd.SCIEZKI_OBRAZU}
    values = gd.git("show", f"{glowa}:{gd.VALUES}").stdout

    def rejestr(obraz, tag):
        try:
            return w_rejestrze(obraz, tag)
        except Exception as e:  # noqa: BLE001 - kazdy blad rejestru = buduj
            print(f"::warning::rejestr nie odpowiedzial dla {obraz}:{tag} ({e}) - buduje")
            return False

    wynik = {}
    for usluga, sciezki in gd.SCIEZKI_OBRAZU.items():
        wynik[usluga] = gd.niezgodnosc_obrazu(
            usluga, values, glowa, rejestr, sciezki + WYZWALACZE_CI[usluga])
    return wynik


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--glowa", required=True, help="commit tego przebiegu (github.sha)")
    ap.add_argument("--wszystkie", action="store_true",
                    help="buduj wszystkie obrazy (workflow_dispatch)")
    args = ap.parse_args(argv)

    wynik = do_budowy(args.glowa, wszystkie=args.wszystkie)
    linie = []
    for usluga, powod in wynik.items():
        print(f"{usluga}: {'BUDUJ - ' + powod if powod else 'aktualny, bez buildu'}")
        linie.append(f"{usluga}={'true' if powod else 'false'}")
    print("\n".join(linie))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("\n".join(linie) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
