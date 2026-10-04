"""Typy parametrow wspolne dla routerow API.

Silnik i tryb to w bazie enumy (`ref.engine`, `ref.trade_mode`). psycopg
wysyla `str` jako `unknown`, wiec literal rzutuje dopiero Postgres - przy
nieznanej wartosci konczylo sie to InvalidTextRepresentation, czyli 500
("Internal Server Error") na zwykla literowke w adresie. Domkniecie listy
TUTAJ sprawia, ze FastAPI odmawia 422 ZANIM powstanie jakiekolwiek zapytanie.

JEDNA DEFINICJA (przeglad 2026-10-04, T-13). Do tej poprawki kazdy router mial
wlasna kopie, a `kapital.py` i `eksport.py` nie mialy zadnej - `engine=foo`
dawalo tam 500, a `mode` szedl nieescapowany do `Content-Disposition`.
"""
from __future__ import annotations

from typing import Literal

Silnik = Literal["crypto", "equity"]
Tryb = Literal["demo", "live"]
