"""Testy odporności workera newsów na ZAWIESZENIE.

Awaria, od której te testy powstały, nie była awarią w żadnym sensie, który
kod potrafił zauważyć: pod stał `Running`, nie rzucił wyjątku, nie zapisał
błędu — po prostu przez 41 minut nie zrobił nic przy interwale cyklu 300 s.
Cała obsługa błędów w pętli była na to ślepa, bo zawieszenie nie jest wyjątkiem.
"""
import pytest

# ===================================================== zawieszenie workera

def test_klient_anthropica_ma_OGRANICZONY_czas_oczekiwania():
    """Worker zamilkł 2026-09-08 na 41 minut przy interwale cyklu 300 s.

    Klient był tworzony bez timeoutu, a SDK domyślnie czeka 600 s i ponawia
    dwa razy — jedno wywołanie mogło więc zablokować proces na pół godziny.
    Zawieszenie NIE JEST WYJĄTKIEM, więc licznik awarii nie rósł, puls nie był
    bity, a pod stał `Running` i nie robił nic. Klient Gemini obok miał 90 s od
    początku; ta rozbieżność była przeoczeniem, nie decyzją.

    Test czyta ŹRÓDŁO, a nie gotowy obiekt, bo pakiet `anthropic` jest tylko
    w obrazie — test wymagający go pomijałby się po cichu na każdej maszynie
    bez tej zależności, czyli nie pilnowałby niczego tam, gdzie się go
    uruchamia.
    """
    import ast
    import pathlib as _p

    drzewo = ast.parse(_p.Path("services/ingest/llm.py").read_text(encoding="utf-8"))
    wywolania = [w for w in ast.walk(drzewo)
                 if isinstance(w, ast.Call)
                 and getattr(w.func, "id", None) == "AsyncAnthropic"]
    assert wywolania, "nie znaleziono konstrukcji klienta Anthropica"

    for w in wywolania:
        nazwane = {k.arg: k.value for k in w.keywords}
        assert "timeout" in nazwane, (
            "klient bez `timeout` czeka domyślne 600 s na wywołanie")
        limit = ast.literal_eval(nazwane["timeout"])
        assert limit <= 300, f"timeout {limit}s jest dłuższy niż cykl workera"
        assert "max_retries" in nazwane, (
            "bez `max_retries` SDK ponawia dwa razy, mnożąc timeout przez trzy")
        assert ast.literal_eval(nazwane["max_retries"]) <= 1


def test_zawieszony_cykl_jest_PRZERYWANY_a_nie_czekany_w_nieskonczonosc():
    """Ogólna warstwa obrony, niezależna od tego, co dokładnie stanęło.

    Timeout na kliencie HTTP chroni przed jednym zawieszonym zapytaniem. Ten
    limit chroni przed wszystkim innym, co może stanąć — w tym przed kodem,
    którego jeszcze nie ma. Bez niego każda przyszła blokująca operacja
    odtworzy tę samą cichą awarię.
    """
    import asyncio

    async def zawieszony():
        await asyncio.sleep(3600)

    async def przebieg():
        async with asyncio.timeout(0.05):
            await zawieszony()

    with pytest.raises(TimeoutError):
        asyncio.run(przebieg())


def test_worker_STOSUJE_limit_cyklu():
    """Sam fakt istnienia opcji nic nie daje, jeśli pętla jej nie używa —
    nastawa udająca, że działa."""
    import ast
    import pathlib

    zrodlo = pathlib.Path("services/ingest/news_worker.py").read_text(encoding="utf-8")
    drzewo = ast.parse(zrodlo)
    limity = [
        w for w in ast.walk(drzewo)
        if isinstance(w, ast.Call)
        and isinstance(w.func, ast.Attribute)
        and w.func.attr == "timeout"
        and any(isinstance(a, ast.Attribute) and a.attr == "limit_cyklu" for a in w.args)
    ]
    assert limity, "run_cycle nie jest opakowany w asyncio.timeout(args.limit_cyklu)"
