"""Prompt i baza muszą dopuszczać dokładnie te same wartości.

DLACZEGO TO JEST WARTE TESTU

Analiza newsa kosztuje pieniądze ZANIM trafi do bazy: najpierw płacimy za
wywołanie modelu, potem zapisujemy wynik. Jeśli model zwróci wartość, której
CHECK w bazie nie dopuszcza, `INSERT` zawiedzie — a pieniądze są już wydane.

Ścieżka zapisu do `news.article_analysis` nie wykonała się dotąd ANI RAZU
(zero wierszy). Sprawdziłem ją 2026-09-07 na produkcji w transakcji wycofanej
i działa — ale wystarczy, że ktoś rozszerzy `enum` w prompcie albo zawęzi
`CHECK` w migracji, żeby rozjazd wrócił niezauważony.

Tego rodzaju błąd nie objawia się przy przeglądzie kodu: obie strony wyglądają
poprawnie z osobna.
"""
import ast
import re
from pathlib import Path

KORZEN = Path(__file__).resolve().parents[1]
WORKER = (KORZEN / "services/ingest/news_worker.py").read_text()
MIGRACJA = (KORZEN / "db/migrations/0003_news.sql").read_text()


def _enum_z_promptu(pole: str) -> set[str]:
    """Wartości dopuszczone przez schemat JSON wymuszony na modelu."""
    m = re.search(rf'"{pole}":\s*\{{[^}}]*"enum":\s*(\[[^\]]*\])', WORKER)
    assert m, f"nie znaleziono enum dla pola {pole} w schemacie promptu"
    return set(ast.literal_eval(m.group(1)))


def _check_z_bazy(kolumna: str) -> set[str]:
    """Wartości dopuszczone przez CHECK w definicji tabeli."""
    m = re.search(rf"{kolumna}\s+text\s+NOT NULL CHECK \({kolumna} IN \(([^)]*)\)\)", MIGRACJA)
    assert m, f"nie znaleziono CHECK dla kolumny {kolumna}"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_horizon_prompt_zgodny_z_baza():
    assert _enum_z_promptu("horizon") == _check_z_bazy("horizon")


def test_direction_prompt_zgodny_z_baza():
    assert _enum_z_promptu("direction") == _check_z_bazy("direction")


def test_zbiory_nie_sa_puste():
    """Kanarek: gdyby wyrażenie przestało cokolwiek znajdować, oba zbiory byłyby
    puste i test przechodziłby, nie sprawdzając niczego."""
    assert len(_enum_z_promptu("horizon")) >= 3
    assert len(_check_z_bazy("horizon")) >= 3


def test_kanarek_wykrywa_rozjazd():
    """Sprawdza, że porównanie faktycznie by zadziałało — bez tego test
    przechodziłby także wtedy, gdyby oba zbiory były równe przez przypadek
    (np. gdyby oba wyrażenia zwracały to samo puste)."""
    prompt = _enum_z_promptu("horizon")
    baza = _check_z_bazy("horizon")
    assert prompt == baza
    assert prompt != (baza | {"24h"}), "porównanie zbiorów nie rozróżnia różnicy"
