"""Jedno miejsce, w ktorym kod giełdy z `ref.venue` zamienia sie w adapter.

Do 2026-09-22 ten wybor siedzial w dwoch miejscach (test klucza w API
i `loop.zbuduj_adapter`) i oba znaly tylko Bybita. Dolozenie drugiej gieldy
to dopisanie JEDNEJ linii tutaj - a nie odnalezienie wszystkich `if venue ==
'bybit'` w projekcie. Host zawsze idzie z `ref.venue`, nigdy z tablicy
w adapterze (patrz komentarz w `credentials.py`).
"""
from __future__ import annotations

# Gieldy, z ktorymi UMIEMY rozmawiac. UI pokazuje w formularzu kluczy tylko
# te - wpis w `ref.venue` bez adaptera (np. `ibkr`, placeholder z seeda 0008
# z hostem localhost) kusilby do wpisania klucza, ktorego nic nie uzyje.
OBSLUGIWANE = ("bybit", "alpaca")


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


def adapter_danych(conn, engine: str, mode: str = "demo"):
    """Adapter do ODCZYTU danych rynkowych dla silnika.

    Bybit wystawia swiece i arkusz publicznie - adapter bez klucza. Alpaca
    Data wymaga klucza takze do odczytu, wiec dla `equity` bierzemy NAJNOWSZY
    aktywny klucz z `app.api_credential` (ta sama regula co w
    `loop.zbuduj_adapter`) i odszyfrowujemy go `SECRETS_KEY` - workery maja
    go w srodowisku (charts/trader/templates/workers.yaml).

    Brak klucza dla silnika, ktory go wymaga, jest BLEDEM, nie pustym cyklem:
    worker equity bez klucza wygladalby jak worker, ktory nie ma nic do
    pobrania.
    """
    from psycopg.rows import dict_row

    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT v.code, v.demo_host, v.live_host FROM ref.venue v"
        " WHERE v.engine::text = %s AND v.code = ANY(%s) ORDER BY v.id LIMIT 1",
        (engine, list(OBSLUGIWANE)))
    venue = cur.fetchone()
    if not venue:
        raise RuntimeError(f"brak obslugiwanej gieldy dla silnika {engine} w ref.venue")
    host = venue["demo_host"] if mode == "demo" else venue["live_host"]
    if venue["code"] == "bybit":
        from services.exchanges.bybit import BybitAdapter
        return BybitAdapter(api_key="", api_secret="", mode=mode, host=host)

    cur.execute(
        "SELECT c.key_enc, c.secret_enc FROM app.api_credential c"
        " JOIN ref.venue v ON v.id = c.venue_id"
        " WHERE v.code = %s AND c.mode::text = %s AND c.active"
        " ORDER BY c.created_at DESC, c.id DESC LIMIT 1", (venue["code"], mode))
    cred = cur.fetchone()
    if not cred:
        raise RuntimeError(
            f"dane rynkowe {venue['code']} wymagaja klucza API, a w app.api_credential "
            f"nie ma aktywnego klucza dla trybu {mode}")
    from services.api.security import SecretBox
    box = SecretBox()
    return zbuduj_adapter_gieldy(venue["code"], box.decrypt(bytes(cred["key_enc"])),
                                 box.decrypt(bytes(cred["secret_enc"])), mode=mode,
                                 demo_host=venue["demo_host"], live_host=venue["live_host"])
