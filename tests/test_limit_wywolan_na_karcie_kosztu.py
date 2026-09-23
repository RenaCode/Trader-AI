"""KARTA KOSZTU AI MA POKAZYWAĆ TEN LIMIT, KTÓRY NAPRAWDĘ ZATRZYMUJE ANALIZĘ.

CO BYŁO ZŁE

Worker newsów ma od poprawki DWIE bramki — `news_worker.py:412-417` przerywa
przy `usd >= DAILY_LIMIT_USD` **albo** `wywolania >= DZIENNY_LIMIT_WYWOLAN`.
Poprawka weszła do workera i nie weszła ani do API, ani do interfejsu:
`/api/api-spend` zwracał licznik wywołań, ale NIE zwracał mianownika, a karta
wypisywała jeden KPI „Limit dobowy … twardy stop po przekroczeniu".

Komentarz przy drugiej bramce (`news_worker.py:366-379`) mówi, ile to
kosztowało:

    „Bramka kwotowa nie zadziałała ANI RAZU. 11.09.2026 potok stanął
     całkowicie przy $0,114 z $5 — zatrzymał go DARMOWY próg Gemini, liczony
     w zapytaniach (…) i pokazywał przy tym »wykorzystanie 2%«".

Czyli: limit, który faktycznie zatrzymał analizę, nie był na ekranie w ogóle,
a ten, który na ekranie był, świecił „w normie" w chwili, gdy potok stał.

Wartość jest dostępna w procesie API — `LLM_DAILY_CALLS` siedzi
w `charts/trader/templates/_helpers.tpl:113`, a helper `trader.llmEnv` jest
włączony do poda API (`api.yaml:77`) właśnie po to, „żeby interfejs nie
pokazywał limitu z domyślu kodu". Brakowało wyłącznie odczytu.
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from services.api.routers import sources as modul
from services.ingest.llm import limit_wywolan_dobowy
from tests.test_dane_bez_zer import Dyspozytor, podlacz

# Fragmenty rozróżniające trzy zapytania `/api-spend` do tej samej tabeli.
# Reguła bardziej szczegółowa idzie pierwsza — `dobowe` też ma `make_interval`.
_DOBOWE = ("news.api_spend", "GROUP BY 1, 2")
_DZIS = ("news.api_spend", "date_trunc('day', now())")
_OKRES = ("news.api_spend", "make_interval")


def dyspozytor(*, koszt: str, wywolania: int) -> Dyspozytor:
    return Dyspozytor(
        (_DOBOWE, [{"dzien": date(2026, 9, 11), "purpose": "triaz",
                    "wywolania": wywolania, "tokeny_wej": 120000,
                    "tokeny_wyj": 9000, "koszt": Decimal(koszt)}]),
        (_DZIS, [{"koszt": Decimal(koszt), "wywolania": wywolania}]),
        (_OKRES, [{"koszt": Decimal(koszt)}]),
    )


def spend(monkeypatch, *, koszt: str, wywolania: int, limit_wywolan: str | None = None,
          limit_usd: str = "5") -> dict:
    monkeypatch.setenv("LLM_DAILY_USD", limit_usd)
    if limit_wywolan is None:
        monkeypatch.delenv("LLM_DAILY_CALLS", raising=False)
    else:
        monkeypatch.setenv("LLM_DAILY_CALLS", limit_wywolan)
    podlacz(monkeypatch, modul, dyspozytor(koszt=koszt, wywolania=wywolania))
    return asyncio.run(modul.api_spend(30))


# ===================================== API oddaje MIANOWNIK, nie sam licznik

def test_api_spend_oddaje_limit_LICZBY_wywolan(monkeypatch):
    """Licznik bez mianownika nie mówi nic o tym, jak blisko progu jest potok."""
    wynik = spend(monkeypatch, koszt="0.1140", wywolania=512, limit_wywolan="512")

    assert wynik["limit_dobowy_wywolan"] == 512, (
        "odpowiedź nie zawiera limitu liczby wywołań — interfejs nie ma z czego "
        "policzyć wykorzystania bramki, która naprawdę zatrzymuje analizę")


def test_limit_wywolan_pochodzi_z_TEGO_SAMEGO_odczytu_co_worker(monkeypatch):
    """Własna kopia `os.environ.get` rozjechałaby się z workerem przy pierwszej
    zmianie nazwy zmiennej — i rozjazd objawiłby się tym, że UI pokazuje inny
    limit niż ten, który faktycznie zatrzymuje potok."""
    monkeypatch.setenv("LLM_DAILY_CALLS", "1234")
    wynik = spend(monkeypatch, koszt="0", wywolania=0, limit_wywolan="1234")

    assert wynik["limit_dobowy_wywolan"] == limit_wywolan_dobowy() == 1234


def test_11_09_2026_karta_ma_pokazac_WYCZERPANY_limit_wywolan(monkeypatch):
    """Stan produkcji z dnia, w którym potok stanął: $0,114 z $5 i 512 z 512
    wywołań. Kwotowe wykorzystanie to 2%, wywołaniowe — 100%."""
    wynik = spend(monkeypatch, koszt="0.1140", wywolania=512, limit_wywolan="512")

    assert wynik["dzis"]["wykorzystanie"] == pytest.approx(0.0228, abs=1e-4)
    assert wynik["dzis"]["wykorzystanie_wywolan"] == pytest.approx(1.0), (
        "wykorzystanie limitu wywołań nie jest liczone, więc karta dalej "
        "opisuje stojący potok jako w normie")


def test_wykorzystanie_kwotowe_zostaje_bez_zmian(monkeypatch):
    """Kanarek: drugi limit dochodzi OBOK pierwszego, nie zamiast niego."""
    wynik = spend(monkeypatch, koszt="2.5000", wywolania=100, limit_wywolan="1000")

    assert wynik["limit_dobowy_usd"] == 5.0
    assert wynik["dzis"]["wykorzystanie"] == pytest.approx(0.5)
    assert wynik["dzis"]["koszt_usd"] == pytest.approx(2.5)
    assert wynik["dzis"]["wywolania"] == 100


def test_dzien_bez_ani_jednego_wywolania_daje_zero_a_nie_brak(monkeypatch):
    """Zero wywołań przy znanym limicie JEST pomiarem równym zeru — w drugą
    stronę niż zwykle w tym projekcie, ale tu licznik naprawdę wynosi zero."""
    wynik = spend(monkeypatch, koszt="0", wywolania=0, limit_wywolan="1500")

    assert wynik["dzis"]["wykorzystanie_wywolan"] == 0.0


# ===================================== interfejs czyta OBA limity

def _sources_jsx() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1]
            / "web/src/components/Sources.jsx").read_text(encoding="utf-8")


def test_karta_kosztu_wypisuje_limit_wywolan(monkeypatch):
    """Limit, którego nie ma na ekranie, nie tłumaczy niczego właścicielowi
    patrzącemu na stojący potok."""
    ui = _sources_jsx()

    assert "limit_dobowy_wywolan" in ui, (
        "karta nie pokazuje limitu liczby wywołań — czyli dalej nie pokazuje "
        "tego progu, który 11.09 zatrzymał analizę")
    assert "wykorzystanie_wywolan" in ui, (
        "karta nie czyta wykorzystania limitu wywołań")


def test_w_normie_liczy_sie_z_OBU_bramek(monkeypatch):
    """Wyczerpanie jednej bramki zatrzymuje analizę niezależnie od stanu
    drugiej, więc „w normie" nie może stać na samym wykorzystaniu kwotowym."""
    ui = _sources_jsx()

    assert "Math.max(" in ui, (
        "werdykt nadal stoi na jednej bramce — karta powie w normie przy "
        "2% kwoty i 100% wywołań, dokładnie jak 11.09.2026")
    assert "wykorzystanieWywolan" in ui and "wykorzystanieUsd" in ui, (
        "obie bramki mają być policzone osobno, żeby dało się powiedzieć, "
        "KTÓRA z nich zatrzymała analizę")
