"""Panel treningowy w PLIKU - harness offline (audyt Fable F-6.2 / T3).

PO CO. Kazdy `train.py --sucho` czytal panele z bazy produkcyjnej: kilkanascie
minut zapytan na przebieg, pelny `learn.model_version` sierot (`--sucho` tez
zaklada wiersz) i eksperymenty puszczane tam, gdzie handluje silnik. Plik
zrobiony RAZ (`scripts/eksport_paneli.py`) zawiera wszystko, czego `train()`
potrzebuje z bazy, i nic poza tym - przebieg z `--panel` nie otwiera zadnego
polaczenia.

FORMAT: JSON skompresowany gzipem (`.json.gz`), nie parquet. Audyt proponowal
parquet, ale obraz i srodowisko testowe nie maja `pyarrow` ani `pandas`, a ten
plik czyta sie raz na przebieg i w calosci - kolumnowy format nie daje tu nic,
za co warto placic nowa zaleznoscia produkcyjna. Daty jako ISO 8601 z UTC,
liczby jako float (tak jak oddaje je `train.load_panel`), wiec panel
odczytany z pliku jest rowny panelowi z bazy co do wartosci (test
`tests/test_panel_z_pliku.py`).

ZAWARTOSC (klucz -> skad w bazie):
    instrumenty         ref.instrument (uniwersum przebiegu)
    panele              train.load_panel
    swiece_1h           market.candle 1h (model wypelnien z opoznieniem)
    spread              spread.zmierz_spread (Pomiar, takze per instrument)
    kroki, prog_pylu    ref.instrument.lot_size, train.prog_dla_pylu
    ostatnia_swieca     max(market.candle.event_time) uniwersum
    dsr                 rozne `params` zakonczonych wersji, licznik prob,
                        rozrzut konfiguracji - mianowniki DSR w chwili eksportu
    przekroj_policzony  czy sa cechy przekrojowe dla odcisku uniwersum
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

WERSJA = 1


def _do_json(o):
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"nie umiem zapisać {type(o).__name__}")


def zapisz(sciezka: str | Path, dane: dict) -> None:
    dane = {"wersja": WERSJA} | dane
    with gzip.open(sciezka, "wt", encoding="utf-8") as f:
        json.dump(dane, f, default=_do_json, separators=(",", ":"))


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def wczytaj(sciezka: str | Path) -> dict:
    """Plik -> slownik z datami jako `datetime` (strefa UTC zachowana)."""
    with gzip.open(sciezka, "rt", encoding="utf-8") as f:
        d = json.load(f)
    if d.get("wersja") != WERSJA:
        raise ValueError(f"panel w wersji {d.get('wersja')}, ten kod czyta {WERSJA}")
    for rows in d["panele"].values():
        for r in rows:
            r["t"] = _t(r["t"])
    for rows in d.get("swiece_1h", {}).values():
        for r in rows:
            r["t"] = _t(r["t"])
    for klucz in ("od", "do", "ostatnia_swieca"):
        if d.get(klucz):
            d[klucz] = _t(d[klucz])
    sp = d.get("spread") or {}
    for klucz in ("od", "do"):
        if sp.get(klucz):
            sp[klucz] = _t(sp[klucz])
    if "per_instrument" in sp:
        sp["per_instrument"] = tuple(tuple(x) for x in sp["per_instrument"])
    return d


def kanoniczne_params(params: dict | str) -> str:
    """`params` jako napis porownywalny niezaleznie od kolejnosci kluczy -
    tak jak porownuje je `count(DISTINCT params)` na jsonb."""
    if isinstance(params, str):
        params = json.loads(params)
    return json.dumps(params, sort_keys=True, separators=(",", ":"))
