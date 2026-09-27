"""`_klucz` nie moze zalezec od tego, JAK wolajacy otworzyl polaczenie.

DLACZEGO TO ISTNIEJE

`news_worker._klucz` czytalo szyfrogram przez `conn.execute(...).fetchone()`
i brala `row[0]`. `conn.execute` uzywa fabryki wierszy z POLACZENIA, a ta
zalezy od wolajacego:

  services/ingest/news_worker.py  - `dict_row` tylko na KURSORZE, wiec
                                    `conn.execute` oddawalo krotki i `row[0]`
                                    dzialalo;
  services/engine/postmortem.py   - `psycopg.connect(url, row_factory=dict_row)`,
                                    czyli `dict_row` na POLACZENIU, wiec
                                    `conn.execute` oddawalo slowniki, a `row[0]`
                                    na slowniku rzuca `KeyError: 0`.

Ta sama funkcja dzialala wiec u jednego wolajacego i padala u drugiego, a nic
w jej sygnaturze tego nie zdradzalo. Zmierzone na produkcji: nocny przebieg
`trader-postmortem` z 2026-09-27 05:50 UTC padl dokladnie tu, z sladem
`KeyError: 0` w `_klucz`. Dziennik jakosciowy - jedyne miejsce, w ktorym
czlowiek czyta wnioski z zamknietych pozycji - nie powstal.

CZEGO TEN TEST NIE ROBI: nie podstawia atrapy polaczenia. Atrapa honorujaca
`row_factory` dowodzilaby tylko, ze atrapa ja honoruje. Test pyta PRAWDZIWEGO
Postgresa, dwa razy, raz na kazdej fabryce - i przed poprawka drugi przebieg
padal.
"""
from __future__ import annotations

import base64
import os

import pytest

TEST_DB_URL = os.environ.get("TEST_DB_URL")
wymaga_bazy = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="ustaw TEST_DB_URL na jednorazową bazę z zastosowanymi migracjami")

KLUCZ_TESTOWY = base64.b64encode(b"0" * 32).decode()
TAJNE = "sk-ant-testowy-klucz-ktory-ma-wrocic-w-calosci"


@wymaga_bazy
@pytest.mark.parametrize("slownikowe_polaczenie", [False, True],
                         ids=["polaczenie_krotkowe", "polaczenie_slownikowe"])
def test_PRAWDZIWA_BAZA_klucz_wraca_przy_OBU_fabrykach_wierszy(
        monkeypatch, slownikowe_polaczenie):
    """Ten sam klucz, dwa sposoby otwarcia polaczenia, ten sam wynik.

    `polaczenie_slownikowe=True` odtwarza dokladnie to, co robi `postmortem.py`
    i co wywracalo nocny przebieg.
    """
    import psycopg
    from psycopg.rows import dict_row

    monkeypatch.setenv("SECRETS_KEY", KLUCZ_TESTOWY)
    from services.api.security import SecretBox
    from services.ingest import news_worker

    szyfrogram = SecretBox(KLUCZ_TESTOWY).encrypt(TAJNE)

    kwargs = {"row_factory": dict_row} if slownikowe_polaczenie else {}
    with psycopg.connect(TEST_DB_URL, **kwargs) as conn:
        conn.execute(
            "INSERT INTO app.secret (name, value_enc, hint) VALUES (%s, %s, %s)"
            " ON CONFLICT (name) DO UPDATE SET value_enc = EXCLUDED.value_enc",
            ("anthropic_api_key", szyfrogram, "klucz testowy"))
        # Bez commitu: caly test zyje w jednej transakcji i konczy sie wycofaniem.
        wynik = news_worker._klucz(conn, "anthropic_api_key", "ANTHROPIC_API_KEY")
        conn.rollback()

    assert wynik == TAJNE, (
        f"`_klucz` oddalo {wynik!r} przy polaczeniu "
        f"{'slownikowym' if slownikowe_polaczenie else 'krotkowym'}. Przed "
        f"poprawka wariant slownikowy rzucal `KeyError: 0`, bo `conn.execute` "
        f"bierze fabryke wierszy z POLACZENIA, a `row[0]` na slowniku nie istnieje")


@wymaga_bazy
def test_PRAWDZIWA_BAZA_brak_wiersza_to_sciezka_ze_srodowiska(monkeypatch):
    """Odwrotna strona: gdy szyfrogramu NIE MA, wraca zmienna srodowiskowa.

    Bez tego testu poprawka mogla zamienic "brak klucza" w wyjatek, a wtedy
    worker padalby tam, gdzie dotad poprawnie schodzil na sciezke zapasowa.
    """
    import psycopg
    from psycopg.rows import dict_row

    monkeypatch.setenv("SECRETS_KEY", KLUCZ_TESTOWY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ze-srodowiska")
    from services.ingest import news_worker

    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        conn.execute("DELETE FROM app.secret WHERE name = %s",
                     ("anthropic_api_key",))
        wynik = news_worker._klucz(conn, "anthropic_api_key", "ANTHROPIC_API_KEY")
        conn.rollback()

    assert wynik == "ze-srodowiska"


def test_funkcja_NIE_uzywa_conn_execute_bez_jawnej_fabryki():
    """Strukturalnie: `row[0]` wolno tylko na kursorze o JAWNEJ fabryce.

    Sprawdzane na tresci funkcji, nie na calym pliku - inaczej test spelnialby
    sie przez dowolne inne wystapienie `tuple_row` gdzie indziej.
    """
    from pathlib import Path
    zrodlo = (Path(__file__).resolve().parents[1]
              / "services" / "ingest" / "news_worker.py").read_text(encoding="utf-8")
    i = zrodlo.index("def _klucz(")
    j = zrodlo.index("\ndef ", i + 1)
    cialo = "\n".join(w.split("#")[0] for w in zrodlo[i:j].splitlines())
    assert "tuple_row" in cialo, (
        "`_klucz` nie zada jawnej fabryki wierszy - wraca zaleznosc od tego, "
        "jak wolajacy otworzyl polaczenie")
    assert "conn.execute(" not in cialo, (
        "`_klucz` znowu woła `conn.execute`, ktore bierze fabryke z polaczenia")
