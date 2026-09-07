"""Eksport transakcji — plik, który wychodzi poza aplikację.

Dane były w bazie i były kompletne. Brakowało wyłącznie wyjścia, a przy handlu
automatycznym liczba transakcji rośnie szybko i każda jest zdarzeniem
podatkowym.

Plik eksportu jest jedynym artefaktem tego projektu, który trafia do kogoś
z zewnątrz — księgowej, urzędu, arkusza. Testy pilnują tego, co przy takim
pliku kosztuje najwięcej: pomyłki co do tego, czy konto jest prawdziwe,
i liczb, które znaczą co innego, niż się wydaje.
"""
import csv
import io
from pathlib import Path

import pytest

from services.api.routers.eksport import (
    KOLUMNY_POZYCJI, KOLUMNY_WYPELNIEN, _czas, zloz_csv,
)


def zbuduj(wiersze, kolumny, wariant="csv"):
    return zloz_csv(wiersze, kolumny, wariant)


def test_tryb_jest_PIERWSZA_kolumna_w_obu_widokach():
    """Rachunek jest demonstracyjny. Plik, który tego nie mówi w KAŻDYM wierszu,
    po przesłaniu dalej wygląda dokładnie jak wyciąg z prawdziwego konta —
    a to jest pomyłka, której nie da się cofnąć po fakcie."""
    assert KOLUMNY_WYPELNIEN[0] == "tryb"
    assert KOLUMNY_POZYCJI[0] == "tryb"


def test_wariant_excel_dokłada_BOM_i_srednik():
    """Polski Excel bez BOM czyta UTF-8 jako windows-1250 i psuje znaki,
    a bez średnika wsadza cały wiersz do jednej komórki. Dwie jawne opcje
    zamiast jednej, która jest zła dla połowy odbiorców."""
    tresc = zbuduj([("demo", "BTCUSDC")], ("tryb", "instrument"), wariant="excel")
    assert tresc.startswith("﻿"), "brak BOM — Excel zepsuje polskie znaki"
    assert ";" in tresc.splitlines()[0]


def test_wariant_csv_jest_CZYSTY():
    """Kanarek do poprzedniego. Wariant maszynowy nie może nieść BOM ani
    średnika — inaczej każdy parser dostaje śmieć w pierwszej kolumnie."""
    tresc = zbuduj([("demo", "BTCUSDC")], ("tryb", "instrument"), wariant="csv")
    assert not tresc.startswith("﻿")
    assert tresc.splitlines()[0] == "tryb,instrument"


def test_wynik_po_kosztach_jest_OSOBNA_kolumna():
    """`realized_pnl` w bazie jest PRZED prowizją. Zostawienie odbiorcy
    odejmowania to zaproszenie do tego samego błędu, który projekt miał już raz
    w mierze walk-forward: wynik zawyżony dokładnie o koszt handlu."""
    assert "wynik_zrealizowany" in KOLUMNY_POZYCJI
    assert "prowizje" in KOLUMNY_POZYCJI
    assert "wynik_po_kosztach" in KOLUMNY_POZYCJI

    zrodlo = Path("services/api/routers/eksport.py").read_text(encoding="utf-8")
    assert 'r["realized_pnl"] - r["fees_paid"]' in zrodlo


def test_czas_ma_jawna_strefe():
    """Znacznik bez strefy jest w pliku dla księgowości bezużyteczny: nie da się
    powiedzieć, do którego dnia należy transakcja z okolic północy."""
    from datetime import datetime, timezone

    t = _czas(datetime(2026, 9, 7, 23, 45, tzinfo=timezone.utc))
    assert t.endswith("+00:00"), f"brak strefy w {t}"
    assert _czas(None) == "", "brak daty ma dawać pustą komórkę, nie 'None'"


def test_liczby_ida_z_KROPKA_i_bez_zaokraglen():
    """Plik ma być źródłem, a nie prezentacją. Przecinek dziesiętny rozjechałby
    się z separatorem kolumn, a zaokrąglenie ukryłoby grosze, których suma po
    tysiącu transakcji przestaje być groszami."""
    from decimal import Decimal

    tresc = zbuduj([("demo", Decimal("0.123456789012"), Decimal("64000.5"))],
                   ("tryb", "ilosc", "cena"))
    # Parsujemy jak odbiorca, a nie szukamy znaku w całym wierszu: przecinek
    # JEST separatorem kolumn, więc pierwsza wersja tego testu sprawdzała coś
    # innego, niż deklarowała.
    wiersz = next(csv.reader(io.StringIO(tresc.splitlines()[1])))
    assert wiersz[1] == "0.123456789012", f"precyzja obcięta: {wiersz[1]}"
    for wartosc in wiersz[1:]:
        assert "," not in wartosc, f"przecinek dziesiętny w wartości: {wartosc}"


def test_endpointy_eksportu_SA_OSIAGALNE_i_chronione():
    from services.api.main import app

    sciezki = set(app.openapi()["paths"])
    assert "/api/engines/{engine}/eksport/wypelnienia" in sciezki
    assert "/api/engines/{engine}/eksport/pozycje" in sciezki

    zrodlo = Path("services/api/main.py").read_text(encoding="utf-8")
    chroniony = zrodlo.split("_protected = [")[1].split("]")[0]
    assert "eksport.router" in chroniony, "eksport poza listą chronionych sesją"
