"""Jedno miejsce, w ktorym kod giełdy z `ref.venue` zamienia sie w adapter.

Do 2026-09-22 ten wybor siedzial w dwoch miejscach (test klucza w API
i `loop.zbuduj_adapter`) i oba znaly tylko Bybita. Dolozenie drugiej gieldy
to dopisanie JEDNEJ linii tutaj - a nie odnalezienie wszystkich `if venue ==
'bybit'` w projekcie. Host zawsze idzie z `ref.venue`, nigdy z tablicy
w adapterze (patrz komentarz w `credentials.py`).
"""
from __future__ import annotations


def zbuduj_adapter_gieldy(venue_code: str, api_key: str, api_secret: str, *,
                          mode: str, demo_host: str | None, live_host: str | None):
    host = demo_host if mode == "demo" else live_host
    if venue_code == "bybit":
        from services.exchanges.bybit import BybitAdapter
        return BybitAdapter(api_key, api_secret, mode=mode, host=host)
    if venue_code == "alpaca":
        from services.exchanges.alpaca import AlpacaAdapter
        return AlpacaAdapter(api_key, api_secret, mode=mode, host=host)
    raise KeyError(venue_code)
