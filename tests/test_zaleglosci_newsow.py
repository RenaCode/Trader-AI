"""Nadrabianie zaległości w analizie newsów — i jego twarda granica.

DLACZEGO GRANICA JEST WYLICZONA, A NIE DOBRANA

`compute_news` liczy sentyment świecy `t` z artykułów spełniających
`a.event_time > t - 24 godziny` oraz analiz spełniających `an.ingested_at <= t`.

Artykuł opublikowany o P i przeanalizowany o A wchodzi więc do świecy `t`
wyłącznie wtedy, gdy jednocześnie A ≤ t oraz t < P + 24 h. Takie `t` istnieje
tylko przy A < P + 24 h — czyli gdy analiza nastąpiła w ciągu doby od publikacji.

Starszy artykuł nie zasili już żadnej świecy, niezależnie od tego, ile za jego
analizę zapłacimy. Zmierzone 2026-09-06: z 496 zapisanych artykułów w oknie
mieściły się 23.
"""
import re
from pathlib import Path

ZRODLO = Path("services/ingest/news_worker.py").read_text()
CECHY = Path("services/ingest/features.py").read_text()


def test_okno_zaleglosci_zgadza_sie_z_oknem_cechy():
    """Kanarek: gdyby ktoś zmienił okno w compute_news, nadrabianie płaciłoby
    za artykuły, które i tak nie mogą wejść do żadnej świecy."""
    from services.ingest.news_worker import OKNO_ZALEGLOSCI_H

    assert re.search(r"a\.event_time >\s+b\.t - interval '24 hours'", CECHY), \
        "okno cechy newsowej nie wynosi już 24 h — popraw OKNO_ZALEGLOSCI_H"
    assert OKNO_ZALEGLOSCI_H == 24


def test_zaleglosci_ida_od_najnowszych():
    """Przy wyczerpaniu budżetu tracimy te, którym do wypadnięcia z okna zostało
    najmniej czasu — czyli najstarsze."""
    assert "ORDER BY a.event_time DESC" in ZRODLO


def test_zaleglosci_biora_tylko_artykuly_bez_analizy():
    assert "LEFT JOIN news.article_analysis an ON an.article_id = a.id" in ZRODLO
    assert "WHERE an.id IS NULL" in ZRODLO


def test_zaleglosci_respektuja_limit_dobowy():
    zaleglosci = ZRODLO[ZRODLO.index("async def nadrob_zaleglosci"):
                        ZRODLO.index("async def run_cycle")]
    assert "if wydano >= DAILY_LIMIT_USD:" in zaleglosci, \
        "nadrabianie zaległości pomija limit wydatków"


def test_zapis_analizy_ma_jedna_implementacje():
    """Dwie kopie zapisu rozjechałyby się przy pierwszej zmianie schematu."""
    assert len(re.findall(r"INSERT INTO news\.article_analysis", ZRODLO)) == 1


def test_zaleglosci_nie_ruszaja_bez_klucza():
    assert "if analizuj and ai is not None:" in ZRODLO, \
        "nadrabianie zaległości musi być pominięte w trybie bez klucza"
