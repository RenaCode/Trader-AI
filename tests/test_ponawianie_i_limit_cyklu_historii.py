"""Proces, który STOI, ma się z tego stania wywrócić — bo inaczej nie widać go wcale.

DWIE DZIURY, JEDNA AWARIA

`backfill._fetch_range` ponawiał błąd giełdy tak:

    except ExchangeError as e:
        if not e.retryable:
            raise
        print(f"...  - ponawiam za 5 s")
        await asyncio.sleep(5)
        continue

Bez licznika prób, bez narastającego odstępu, bez wyjścia z pętli. `retryable=
True` ustawiają m.in. limit zapytań i KAŻDE 5xx (services/exchanges/alpaca.py),
więc kilkugodzinne okno serwisowe giełdy to pętla co pięć sekund w nieskończoność.

A `history_worker.main` — w odróżnieniu od `news_worker` — nie owijał cyklu
w `asyncio.timeout`.

To nie jest „wisi dłużej". Cykl `history` NIGDY się wtedy nie kończy, więc ani
`puls.bij`, ani `puls.zglos_awarie` nie zostaje wywołane, licznik `pod_rzad` nie
rośnie, proces nie pada i `POD_RZAD_MAX` nie ma czego liczyć. Pod stoi „Running",
świece 1h stoją, a świece 4h (agregat z 1h) i wszystkie cechy z nich razem z nimi.
Docstring `history_worker.py` nazywa ten stan wprost — kod go dopuszczał.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from services.exchanges.base import ExchangeError
from services.ingest import backfill

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO_HISTORII = (KORZEN / "services/ingest/history_worker.py").read_text(encoding="utf-8")


class _GieldaWOknieSerwisowym:
    """Adapter oddający 503 przy każdym zapytaniu — jak giełda w oknie serwisowym."""

    def __init__(self, retryable: bool = True):
        self.zapytan = 0
        self.retryable = retryable

    async def fetch_candles(self, symbol, timeframe, limit, start):
        self.zapytan += 1
        raise ExchangeError(f"HTTP 503 dla {symbol}", code="503",
                            retryable=self.retryable)


@pytest.fixture
def bez_czekania(monkeypatch):
    """Odstępy ponowień na zero — badamy LICZNIK PRÓB, nie zegar.

    Prawdziwe odstępy to 5+10+20+40+60+60 s na jedno okno. Podmieniamy
    `asyncio.sleep` wyłącznie na czas testu (monkeypatch przywraca oryginał)
    i zostawiamy w nim `await`, żeby pętla dalej oddawała sterowanie — bez tego
    `wait_for` niżej nie miałby kiedy zadziałać na kodzie SPRZED poprawki.
    """
    oryginalny = asyncio.sleep

    async def natychmiast(_ile, *a, **kw):
        return await oryginalny(0)

    monkeypatch.setattr(asyncio, "sleep", natychmiast)


def _pobierz(adapter, sekund: float = 5.0):
    """`_fetch_range` z TWARDYM limitem czasu w samym teście.

    Limit jest tu po to, żeby kod SPRZED poprawki dał się w ogóle zbadać:
    pętla bez wyjścia zawiesiłaby zestaw testów zamiast zgłosić porażkę.
    `TimeoutError` zamiast `ExchangeError` to właśnie ten stan, którego ta
    poprawka dotyczy.
    """
    from datetime import datetime, timedelta, timezone

    teraz = datetime.now(timezone.utc)
    start = teraz - timedelta(days=3)

    async def _biegnij():
        async with asyncio.timeout(sekund):
            return await backfill._fetch_range(
                adapter, conn=None, cur=None, instrument_id=1, symbol="BTCUSDC",
                timeframe="1h", step=timedelta(hours=1),
                start=start, stop=teraz, now=teraz)

    return asyncio.run(_biegnij())


def test_PONAWIANIE_MA_KONIEC_a_nie_krazy_w_kolko(bez_czekania):
    """Rdzeń znaleziska. Bez licznika ta pętla nie kończy się NIGDY."""
    gielda = _GieldaWOknieSerwisowym()

    with pytest.raises(ExchangeError) as e:
        _pobierz(gielda)

    assert "prób" in str(e.value)
    assert gielda.zapytan == backfill.PROBY_PONOWIEN + 1, (
        f"giełdę zapytano {gielda.zapytan} razy przy limicie "
        f"{backfill.PROBY_PONOWIEN} prób")


def test_WYJATEK_MOWI_DLACZEGO_PRZERYWAMY(bez_czekania):
    """Komunikat ma tłumaczyć, że przerwanie jest LEPSZE od dalszego czekania.

    Bez tego „przerwałem po 6 próbach" wygląda na nadgorliwość kodu, a jest
    jedynym sposobem, żeby postój giełdy stał się widoczny dla nadzoru.
    """
    gielda = _GieldaWOknieSerwisowym()
    with pytest.raises(ExchangeError) as e:
        _pobierz(gielda)

    tresc = str(e.value)
    assert "pulsu" in tresc or "puls" in tresc, (
        "komunikat nie mówi, dlaczego pętla bez końca jest gorsza niż przerwanie")


def test_BLAD_NIEPONAWIALNY_dalej_leci_od_razu(bez_czekania):
    """Granica: rozróżnienie `retryable` zostaje tam, gdzie było.

    Błędu walidacji nie ma sensu ponawiać ani razu — sześć prób na nim to sześć
    razy ten sam wynik i trzy minuty straty.
    """
    gielda = _GieldaWOknieSerwisowym(retryable=False)

    with pytest.raises(ExchangeError):
        _pobierz(gielda)

    assert gielda.zapytan == 1


def test_ODSTEP_MIEDZY_PROBAMI_NARASTA():
    """Sześć prób co pięć sekund to trzydzieści sekund — za mało na przerwę
    u giełdy, żeby się nią przespać, i za dużo, żeby jej nie zauważyć.
    Narastający odstęp daje ~3 minuty na tej samej liczbie zapytań."""
    zrodlo = (KORZEN / "services/ingest/backfill.py").read_text(encoding="utf-8")
    fragment = zrodlo[zrodlo.index("async def _fetch_range"):]
    assert "2 **" in fragment, (
        "odstęp między próbami jest stały — pętla ponawia sześć razy w pół "
        "minuty i kończy się, zanim jakakolwiek przerwa u giełdy minie")


# ============================================ limit czasu cyklu w history_worker

def _funkcja(nazwa: str) -> ast.AST:
    drzewo = ast.parse(ZRODLO_HISTORII)
    for w in ast.walk(drzewo):
        if isinstance(w, (ast.FunctionDef, ast.AsyncFunctionDef)) and w.name == nazwa:
            return w
    raise AssertionError(f"nie znaleziono funkcji `{nazwa}`")


def test_HISTORY_WORKER_MA_TWARDY_LIMIT_CZASU_CYKLU():
    """`news_worker` ma `--limit-cyklu` od dawna; `history_worker` nie miał.

    Test czyta źródło, bo `main` to pętla z połączeniem do bazy i wyjściami do
    giełdy — uruchomienie jej w teście wymagałoby atrapy całego świata,
    a sprawdzana własność jest własnością STRUKTURY, nie wyniku.
    """
    main = _funkcja("main")
    for w in ast.walk(main):
        if not isinstance(w, ast.AsyncWith):
            continue
        if "timeout" not in ast.dump(w.items[0].context_expr):
            continue
        if any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "cykl"
               for c in ast.walk(w)):
            return
    pytest.fail(
        "`history_worker.main` woła `cykl()` bez `asyncio.timeout` — proces "
        "zawieszony w cyklu nie bije pulsu, nie zgłasza awarii i nie podnosi "
        "licznika `pod_rzad`, więc stoi i wygląda na `Running`")


def test_LIMIT_CYKLU_JEST_ARGUMENTEM_a_nie_liczba_wpisana_w_petli():
    """Limit ma dać się podnieść w charcie bez przebudowy obrazu — pierwszy
    backfill po długim postoju bywa dłuższy niż cykl przy czystym starcie."""
    assert "--limit-cyklu" in ZRODLO_HISTORII


def test_DOMYSLNY_LIMIT_JEST_LICZONY_DLA_CYKLU_PO_AWARII():
    """Limit ustawiony pod zdrowy przebieg ucinałby dokładnie ten cykl, który
    nadrabia: po postoju dziura do domknięcia jest większa, więc cykl dłuższy.

    Zdrowy cykl `--days 3` na świecach 1h to sekundy. Godzina jest o dwa rzędy
    wielkości za duża na pracę i o rząd za mała na zawieszenie.
    """
    main = _funkcja("main")
    for w in ast.walk(main):
        if (isinstance(w, ast.Call)
                and getattr(w.func, "attr", None) == "add_argument"
                and w.args and getattr(w.args[0], "value", None) == "--limit-cyklu"):
            domyslny = next(k.value.value for k in w.keywords if k.arg == "default")
            assert domyslny >= 1800, (
                f"domyślny limit cyklu to {domyslny} s — cykl nadrabiający po "
                f"dłuższym postoju wpadnie w niego przy zdrowej pracy")
            return
    pytest.fail("nie znaleziono definicji `--limit-cyklu`")
