"""Narzędzia diagnostyczne muszą mierzyć TO SAMO uniwersum, którym się handluje.

DLACZEGO TO JEST OSOBNY PLIK

Cecha przekrojowa `rel_strength` jest własnością PARY (instrument, uniwersum),
więc jej definicja nosi odcisk zbioru w `params->>'uniwersum'` (patrz
services/engine/uniwersum.py). `train.load_panel` przepuszcza taką cechę
WYŁĄCZNIE wtedy, gdy podany odcisk zgadza się co do znaku:

    AND (fd.params->>'uniwersum' IS NULL OR fd.params->>'uniwersum' = %(fp)s)

Z tego wynikają dwa sposoby, żeby CICHO zgubić ósmy sygnał:

  1. nie podać `fp` w ogóle — wtedy warunek jest `= NULL`, czyli NULL, czyli
     fałsz, i żaden wiersz cechy przekrojowej nie przechodzi;
  2. podać odcisk POLICZONY Z INNEGO ZBIORU — `feat.feature_def` nie ma wtedy
     pasującej definicji i dzieje się dokładnie to samo.

Żaden z tych przypadków nie kończy się błędem. Panel jest, świece są, tabela
wyników się drukuje — brakuje w niej jednego wiersza, a `cross_section` zwraca
None i `combine()` renormalizuje wynik po pozostałych siedmiu sygnałach. Czyli
narzędzie mierzy INNĄ strategię niż ta, którą prowadzi produkcja, i nie ma jak
tego powiedzieć.
"""
import ast
from pathlib import Path

import pytest

from services.engine.uniwersum import odcisk

KORZEN = Path(__file__).resolve().parents[1]
MODULY = sorted((KORZEN / "services").rglob("*.py"))


def _wywolania_load_panel(sciezka: Path) -> list[ast.Call]:
    drzewo = ast.parse(sciezka.read_text())
    return [w for w in ast.walk(drzewo)
            if isinstance(w, ast.Call)
            and isinstance(w.func, ast.Name) and w.func.id == "load_panel"]


def test_kazde_wywolanie_load_panel_podaje_odcisk():
    """Bez `fp` cecha przekrojowa wypada z panelu — cicho i bez błędu."""
    braki = []
    for sciezka in MODULY:
        for wywolanie in _wywolania_load_panel(sciezka):
            klucze = {k.arg for k in wywolanie.keywords}
            # `fp` jest piątym parametrem pozycyjnym (cur, iid, h, since, until, fp)
            pozycyjnie = len(wywolanie.args) >= 6
            if "fp" not in klucze and not pozycyjnie:
                braki.append(f"{sciezka.relative_to(KORZEN)}:{wywolanie.lineno}")
    assert not braki, (
        "wywołania load_panel bez odcisku uniwersum — `params->>'uniwersum' = NULL` "
        "wycina rel_strength z panelu bez żadnego komunikatu: " + ", ".join(braki))


def test_odcisk_uniwersum_decyzyjnego_nie_zalezy_od_narzedzia():
    """Odcisk liczony przez diagnostykę musi być tym samym, co liczy trener.

    KANAREK NA PRAWDZIWY STAN BAZY. Migracja 0023 zostawiła pary USDT jako
    `active` i zdjęła im `tradeable` — więc zbiór „aktywne" i zbiór decyzyjny
    RÓŻNIĄ SIĘ, a ten sam aktyw występuje w tym pierwszym dwa razy.
    """
    baza = [
        {"symbol": "BTCUSDT", "active": True, "tradeable": False, "kind": "spot"},
        {"symbol": "BTCUSDC", "active": True, "tradeable": True,  "kind": "spot"},
        {"symbol": "ETHUSDT", "active": True, "tradeable": False, "kind": "spot"},
        {"symbol": "ETHUSDC", "active": True, "tradeable": True,  "kind": "spot"},
        {"symbol": "BTCUSDT-PERP", "active": True, "tradeable": False, "kind": "perp"},
    ]
    decyzyjne = [r["symbol"] for r in baza
                 if r["active"] and r["tradeable"] and r["kind"] != "perp"]
    wszystkie_aktywne = [r["symbol"] for r in baza
                         if r["active"] and r["kind"] != "perp"]

    assert odcisk(decyzyjne) != odcisk(wszystkie_aktywne), (
        "test nie odtwarza sytuacji z bazy — oba zbiory muszą się różnić")
    # To jest treść błędu: narzędzie liczące odcisk po „wszystkich aktywnych"
    # szuka definicji cechy, której nikt nigdy nie zapisał.
    assert len(wszystkie_aktywne) == 2 * len(decyzyjne), (
        "zbiór 'aktywne' liczy każdy aktyw dwa razy — USDT i USDC")


def test_diagnostyka_domyslnie_bierze_uniwersum_decyzyjne():
    """Domyślny przebieg diagnostyki nie może brać szerszego zbioru niż produkcja.

    Sprawdzamy ZAPYTANIE, nie komentarz: `tradeable` jest jedyną flagą, która
    oddziela historię zbieraną szerzej od zbioru, którym się handluje (0020).
    """
    from services.engine.uniwersum import SQL_UNIWERSUM_DECYZYJNE
    sql = " ".join(SQL_UNIWERSUM_DECYZYJNE.split())
    assert "active" in sql and "tradeable" in sql and "kind <> 'perp'" in sql

    for nazwa in ("diagnose_signals.py", "diagnose_timing.py"):
        zrodlo = (KORZEN / "services" / "engine" / nazwa).read_text()
        assert "SQL_UNIWERSUM_DECYZYJNE" in zrodlo or "uniwersum_decyzyjne" in zrodlo, (
            f"{nazwa} buduje uniwersum własnym zapytaniem — rozjazd z produkcją "
            f"nie da się wtedy zauważyć inaczej niż przez brakujący wiersz w tabeli")
