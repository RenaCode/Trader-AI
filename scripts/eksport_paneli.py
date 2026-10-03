#!/usr/bin/env python3
"""Eksport paneli treningowych do pliku - harness offline (audyt Fable T3).

    DATABASE_URL=... python scripts/eksport_paneli.py --bar 4h --days 1400 \\
        --wyjscie panele_crypto_4h_20261003.json.gz [--do 2026-10-03T04:34:00+00:00]

Potem kazdy eksperyment idzie z pliku, bez bazy:

    python services/engine/train.py --panel panele_crypto_4h_20261003.json.gz \\
        --bar 4h --days 1400 --kind spot_eu --limit --sucho
    python scripts/eksperyment.py --panel panele_crypto_4h_20261003.json.gz ...

TYLKO ODCZYT. Skrypt wykonuje wylacznie SELECT-y - inaczej niz `train.py
--sucho`, ktory zaklada wiersze w `learn.model_version` i `learn.training_run`.
Mozna go puscic raz w podzie `diagnostyka-trener` i sciagnac plik `kubectl cp`,
albo na kopii bazy odtworzonej z backupu.

`--do` zamraza koniec okna. Domyslnie chwila eksportu - na kopii z backupu
lepiej podac chwile zrobienia kopii, zeby `--days` znaczylo to samo, co na
produkcji tego dnia.

Format i zawartosc pliku: `services/engine/panel_plik.py`. Spread i mianowniki
DSR sa liczone RAZ, na oknie eksportu - przebieg z pliku z krotszym `--days`
dostaje te same liczby (spread z `book_spread_bps` siega i tak tylko kilku
ostatnich tygodni).
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.engine import panel_plik  # noqa: E402
from services.engine import train as trener  # noqa: E402
from services.engine.horizons import HORIZONS, get as get_horizon  # noqa: E402
from services.engine.spread import zmierz_spread  # noqa: E402
from services.engine.uniwersum import odcisk  # noqa: E402

_SQL_KONFIGURACJE = """
SELECT DISTINCT v.params::text AS params
FROM learn.model_version v
WHERE v.model_id = %s AND v.status IN ('candidate','active','retired')
"""


def zbuduj(cur, engine: str, bar: str, dni: int, do: datetime) -> dict:
    """Komplet danych `train()` dla uniwersum `tradeable` toru `engine/bar`.

    Te same zapytania i funkcje, ktore `train()` wola w trybie z baza - po to,
    zeby przebieg z pliku byl rowny przebiegowi z bazy (test
    `tests/test_panel_z_pliku.py`).
    """
    h = get_horizon(bar)
    if h.source_timeframe != "1h":
        raise SystemExit(f"eksport obsługuje horyzonty ze świec źródłowych 1h, "
                         f"{bar} liczy się z {h.source_timeframe}")
    od = do - timedelta(days=dni)
    cur.execute("SELECT id, symbol, quote_ccy FROM ref.instrument WHERE engine=%s AND active"
                " AND tradeable AND kind <> 'perp' ORDER BY symbol", (engine,))
    instrumenty = [dict(i) for i in cur.fetchall()]
    if not instrumenty:
        raise SystemExit(f"puste uniwersum {engine} — nie ma czego eksportować")
    ids = [i["id"] for i in instrumenty]
    fp = odcisk([i["symbol"] for i in instrumenty])

    panele, swiece = {}, {}
    for i in instrumenty:
        panele[i["symbol"]] = trener.load_panel(cur, i["id"], h, od, do, fp=fp)
        swiece[i["symbol"]] = trener.load_swiece_wykonania(cur, i["id"], od, do)
        print(f"  {i['symbol']:10s} {len(panele[i['symbol']]):>7,} punktów panelu, "
              f"{len(swiece[i['symbol']]):>7,} świec 1h")

    pomiar = zmierz_spread(cur, engine, h.bar, h.source_timeframe, h.bar_minutes,
                           ids, od, do)
    cur.execute("SELECT symbol, lot_size FROM ref.instrument WHERE id = ANY(%s)", (ids,))
    kroki = {r["symbol"]: (float(r["lot_size"]) if r["lot_size"] is not None else None)
             for r in cur.fetchall()}
    nazwa_modelu = f"{engine}_signal_weighting_{h.bar}"
    prog_pylu = trener.prog_dla_pylu(cur, nazwa_modelu)

    # Ta sama definicja co w `train()`: instrumenty z panelem > 500 punktow.
    z_panelem = [i["id"] for i in instrumenty if len(panele[i["symbol"]]) > 500]
    cur.execute("SELECT max(event_time) AS ostatnia FROM market.candle"
                " WHERE instrument_id = ANY(%s) AND timeframe = %s",
                (z_panelem, h.source_timeframe))
    ostatnia = (cur.fetchone() or {}).get("ostatnia")

    cur.execute("SELECT count(*) AS n FROM feat.feature_def"
                " WHERE active AND params->>'bar' = %s AND params->>'uniwersum' = %s",
                (h.bar, fp))
    przekroj = bool(cur.fetchone()["n"])

    cur.execute("SELECT id FROM learn.model WHERE name = %s", (nazwa_modelu,))
    model = cur.fetchone()
    if model:
        cur.execute(_SQL_KONFIGURACJE, (model["id"],))
        konfiguracje = sorted({panel_plik.kanoniczne_params(r["params"])
                               for r in cur.fetchall()})
        cur.execute("SELECT count(*) AS n FROM learn.hypothesis_trial WHERE model_id=%s",
                    (model["id"],))
        trials = cur.fetchone()["n"]
        odchylenie = list(trener.odchylenie_prob_z_bazy(cur, model["id"]))
    else:
        konfiguracje, trials, odchylenie = [], 0, [None, 0]

    return {
        "engine": engine, "bar": h.bar, "dni": dni, "od": od, "do": do,
        "odcisk": fp, "instrumenty": instrumenty, "panele": panele,
        "swiece_1h": swiece, "spread": asdict(pomiar), "kroki": kroki,
        "prog_pylu": list(prog_pylu), "ostatnia_swieca": ostatnia,
        "przekroj_policzony": przekroj,
        "dsr": {"konfiguracje": konfiguracje, "trials": trials,
                "odchylenie": odchylenie},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--bar", default="4h", choices=sorted(HORIZONS))
    ap.add_argument("--days", type=int, default=1400)
    ap.add_argument("--do", default="",
                    help="koniec okna (ISO 8601 ze strefa); domyslnie teraz")
    ap.add_argument("--wyjscie", default="",
                    help="plik wyjsciowy; domyslnie panele_<engine>_<bar>_<data>.json.gz")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")
    do = (datetime.fromisoformat(args.do) if args.do
          else datetime.now(timezone.utc))
    if do.tzinfo is None:
        sys.exit("--do musi miec strefe czasowa (np. +00:00)")
    wyjscie = args.wyjscie or f"panele_{args.engine}_{args.bar}_{do:%Y%m%d}.json.gz"

    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(url, row_factory=dict_row) as conn:
        # Tylko odczyt - takze na poziomie sesji, zeby pomylka w zapytaniu nie
        # mogla niczego zapisac.
        conn.read_only = True
        dane = zbuduj(conn.cursor(), args.engine, args.bar, args.days, do)
    panel_plik.zapisz(wyjscie, dane)
    print(f"zapisano {wyjscie} ({Path(wyjscie).stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
