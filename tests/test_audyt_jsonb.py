"""Czy `audit()` podaje kolumnom `jsonb` coś, co psycopg UMIE ZWIĄZAĆ.

DLACZEGO TO JEST OSOBNY TEST

`audit()` dostawała `before`/`after` jako goły Pythonowy `dict` i wkładała go
prosto w `%s`. psycopg 3 nie adaptuje `dict` — odmawia PRZY WIĄZANIU PARAMETRU:

    ProgrammingError: cannot adapt type 'dict' using placeholder '%s'

Ta klasa błędu jest gorsza, niż wygląda, z trzech powodów naraz:

  * wyjątek leci z biblioteki, nie z bazy — żaden test na atrapie kursora go
    nie zobaczy, bo atrapa po prostu zapamiętuje argumenty;
  * nic go nie łapie (w `main.py` nie ma `exception_handler`), więc FastAPI
    oddaje 500;
  * pula chodzi z `autocommit=True` (`db.open_pool`), a `audit()` woła się PO
    właściwym zapisie — zmiana JEST zatwierdzona, a użytkownik widzi błąd.

Stąd objawy na produkcji: kill-switch zwolniony, ale UI mówi „nie udało się"
i w audycie nie ma KTO wznowił handel; test klucza giełdy zawsze 500; ponowiony
przepływ kapitału dubluje wpłatę. Za każdym razem stan bazy rozjeżdża się
z tym, co zobaczył człowiek — a audyt, czyli jedyny zapis odpowiedzialności,
jest pusty dokładnie w chwilach, dla których go założono.

JAK TO TU SPRAWDZAMY

Bez bazy. Odrzucenie następuje w psycopg przy szukaniu dumpera, więc ten sam
krok da się wykonać wprost: `Transformer.get_dumper(wartość, PyFormat.AUTO)`.
Atrapa `execute` przepuszcza przez niego każdy parametr, którego `audit()`
użyła — czyli test pyta o dokładnie to, co pyta sterownik w czasie wykonania.
"""
from __future__ import annotations

import asyncio

import pytest
from psycopg.adapt import PyFormat, Transformer
from psycopg.types.json import Jsonb

from services.api import auth


class ZapamietanyZapis:
    """Atrapa `execute`, która ZWIĄZUJE parametry zamiast je przepisywać.

    Samo zapamiętanie krotki nic by nie udowodniło: goły `dict` wygląda w niej
    tak samo dobrze jak `Jsonb`. Dopiero przepuszczenie przez `Transformer`
    odtwarza chwilę, w której psycopg odmawia.
    """

    def __init__(self) -> None:
        self.sql: str | None = None
        self.parametry: tuple | None = None

    async def __call__(self, sql: str, parametry=None) -> None:
        self.sql = sql
        self.parametry = parametry
        transformer = Transformer()
        for wartosc in parametry or ():
            transformer.get_dumper(wartosc, PyFormat.AUTO)


def _wywolaj(**kwargi) -> ZapamietanyZapis:
    zapis = ZapamietanyZapis()
    pierwotny = auth.execute
    auth.execute = zapis
    try:
        asyncio.run(auth.audit("test.akcja", user_id=1, **kwargi))
    finally:
        auth.execute = pierwotny
    return zapis


def _kolumny_json(zapis: ZapamietanyZapis) -> tuple:
    """Parametry trafiające w `before` i `after` — po pozycji, jak w INSERCIE."""
    kolumny = [k.strip() for k in
               zapis.sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    return tuple(zapis.parametry[kolumny.index(nazwa)]
                 for nazwa in ("before", "after"))


def test_dict_w_kolumnie_jsonb_DA_SIE_zwiazac():
    """Twierdzenie główne. PADA na kodzie sprzed poprawki — tam `get_dumper`
    podnosi `ProgrammingError` na gołym `dict`, czyli dokładnie to, co leciało
    z produkcji jako 500 po dokonanej już zmianie."""
    zapis = _wywolaj(before={"reason": "limit dzienny", "released_at": None},
                     after={"release_note": "reka operatora"})

    before, after = _kolumny_json(zapis)
    assert isinstance(before, Jsonb), f"before poszlo jako {type(before).__name__}"
    assert isinstance(after, Jsonb), f"after poszlo jako {type(after).__name__}"


def test_lista_w_kolumnie_jsonb_TEZ_sie_wiaze():
    """`list` psycopg adaptuje sam, ale jako tablicę Postgresa, nie JSON —
    do `jsonb` i tak nie wejdzie. Opakowanie musi obejmować i ten przypadek."""
    zapis = _wywolaj(after={"permissions": ["read", "trade"]},
                     before=[{"stare": 1}])

    before, after = _kolumny_json(zapis)
    assert isinstance(before, Jsonb) and isinstance(after, Jsonb)


def test_brak_stanu_zapisuje_NULL_a_nie_jsonowy_null():
    """`None` ma zostać `None`.

    `Jsonb(None)` zapisałby JSON-owy `null`, czyli „stan istnieje i jest pusty".
    To co innego niż „stanu nie podano" i przy czytaniu audytu myli: `after IS
    NULL` przestaje odróżniać wpis bez części `after` od wpisu, w którym ktoś
    celowo zapisał pustkę.
    """
    zapis = _wywolaj(after={"ok": True})

    before, after = _kolumny_json(zapis)
    assert before is None, f"before poszlo jako {before!r}, a ma byc NULL"
    assert isinstance(after, Jsonb)


def test_atrapa_NAPRAWDE_odrzuca_golego_dicta():
    """Kanarek tego pliku.

    Testy wyżej są warte tyle, ile warta jest atrapa: gdyby `get_dumper`
    przestał cokolwiek odrzucać (inna wersja psycopg, zmiana API), przechodziły
    by przy każdej zawartości krotki. Tu podajemy gołego `dict` wprost.
    """
    zapis = ZapamietanyZapis()
    with pytest.raises(Exception, match="cannot adapt type 'dict'"):
        asyncio.run(zapis("INSERT INTO app.audit_log (before) VALUES (%s)",
                          ({"a": 1},)))
