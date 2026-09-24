"""Arkusz idzie z giełdy, NA KTÓREJ SKŁADAMY ZLECENIA — świece nadal z globalnej.

DLACZEGO TE DWA ŹRÓDŁA MUSZĄ BYĆ ROZDZIELONE

Do 2026-09-24 `BybitAdapter` brał WSZYSTKIE dane publiczne z `PUBLIC_HOST`
(`api.bybit.com`), także arkusz. Uzasadnienie brzmiało: „to on ma księgę, na
której model był trenowany" — i dla świec jest słuszne, bo przepięcie tamtego
źródła wstawiłoby w szereg uskok, którego kalibracja wag nie zakłada.

Dla arkusza było odwrotne. `fetch_order_book` zasila wyłącznie rachunek kosztów:
cechę `book_spread_bps` (którą `diagnose_signals.CECHY_BEZ_SYGNALU` wymienia
jako nieczytaną przez żaden sygnał) oraz składnik „spread" w `koszty.karta_rundy`.
Kosztu nie opisuje księga, na której model się uczył — opisuje go księga, na
którą idą zlecenia.

`docs/08` nazywał to najdroższym otwartym pytaniem projektu: spready mierzono
na księdze globalnej, a zlecenia idą na bybit.eu, podmiot o rzędu 0,1% tamtego
wolumenu. `karta_rundy` oznaczała tę liczbę jako INNY_RYNEK i traktowała jako
PODŁOGĘ kosztu, nie jego pomiar.

CZEGO TEN PLIK PILNUJE: żeby rozjazd nie wrócił — w żadną ze stron. Świece
przepięte na księgę lokalną zepsułyby szereg treningowy; arkusz przepięty
z powrotem na globalną zepsułby rachunek kosztów. Żadna z tych zmian nie
objawia się awarią: obie oddają liczby, które wyglądają poprawnie.
"""
from __future__ import annotations

import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO = (KORZEN / "services/exchanges/bybit.py").read_text(encoding="utf-8")


def _wywolanie(endpoint: str) -> str:
    """Fragment źródła od nazwy endpointu do końca wywołania `_get`."""
    i = ZRODLO.index(f'"{endpoint}"')
    return ZRODLO[i:ZRODLO.index(")", ZRODLO.index("host=", i))]


def test_arkusz_idzie_z_gieldy_na_ktorej_handlujemy():
    wyw = _wywolanie("/v5/market/orderbook")
    assert "host=self._host" in wyw, (
        "arkusz wrócił na host globalny — `book_spread_bps` i składnik `spread` "
        "w `karta_rundy` znowu opisują księgę, na której NIE składamy zleceń")
    assert "PUBLIC_HOST" not in wyw


def test_swiece_zostaja_na_ksiedze_globalnej():
    """Model był na niej trenowany; przepięcie wstawia uskok w szereg."""
    assert "host=PUBLIC_HOST" in _wywolanie("/v5/market/kline"), (
        "świece przepięte z hosta globalnego — szereg treningowy dostaje uskok, "
        "którego kalibracja wag nie zakłada")


def test_host_arkusza_idzie_za_trybem():
    """`self._host` pochodzi z `ref.venue`, więc w demo pytamy księgi demo —
    tej, która naprawdę wypełnia nasze zlecenia — a po przejściu na live: live.

    Gdyby host był zaszytą stałą, pomiar kosztu opisywałby inną księgę niż ta,
    z której przychodzą wypełnienia, i nikt by tego nie zauważył: liczba
    wyglądałaby poprawnie w obu przypadkach.
    """
    naglowek = ZRODLO[:ZRODLO.index("class BybitAdapter")]
    assert re.search(r"PUBLIC_HOST\s*=\s*\"https://api\.bybit\.com\"", naglowek)
    # Stałej dla księgi unijnej NIE MA i mieć nie powinno — host przychodzi
    # z bazy, żeby nie trzymać tej samej prawdy w dwóch miejscach.
    assert "api.bybit.eu\"" not in naglowek.replace("# ", ""), (
        "host unijny zaszyty jako stała — to druga kopia tego, co stoi "
        "w `ref.venue`, i rozjedzie się przy pierwszej zmianie")


def test_cecha_spreadu_nadal_nie_zasila_zadnego_sygnalu():
    """Warunek, na którym stoi cała ta zmiana.

    Gdyby któryś sygnał zaczął czytać `book_spread_bps`, przepięcie źródła
    arkusza stałoby się zmianą wejścia MODELU, a nie tylko rachunku kosztów —
    i wymagałoby ponownej kalibracji, a nie samego komentarza.
    """
    diag = (KORZEN / "services/engine/diagnose_signals.py").read_text(encoding="utf-8")
    assert "book_spread_bps" in diag.split("CECHY_BEZ_SYGNALU")[1].split(")")[0]
    sygnaly = (KORZEN / "services/engine/signals.py").read_text(encoding="utf-8")
    assert "book_spread_bps" not in sygnaly
