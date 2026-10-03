"""Eksport transakcji — wyjście dla księgowości i dla własnej analizy.

DLACZEGO TO ISTNIEJE

Dane są w bazie i są kompletne: dziennik decyzji jest append-only, wypełnienia
mają prowizję i znacznik czasu, pozycje mają zrealizowany wynik. Brakowało
wyłącznie wyjścia — a przy handlu automatycznym liczba transakcji rośnie szybko
i każda jest zdarzeniem podatkowym.

DWA WIDOKI, BO ODPOWIADAJĄ NA DWA RÓŻNE PYTANIA

  wypelnienia  — pojedyncze zdarzenia: kupno albo sprzedaż, ilość, cena,
                 prowizja. To jest surowy materiał i to na nim opierają się
                 rozliczenia wymagające każdej transakcji z osobna.
  pozycje      — zamknięte przejścia tam i z powrotem, z ceną wejścia, wyjścia
                 i wynikiem po kosztach. To jest postać, w której wynik da się
                 przeczytać bez arkusza.

TRYB JEST W KAŻDYM WIERSZU, NIE TYLKO W NAZWIE PLIKU

Rachunek jest demonstracyjny. Plik, który tego nie mówi w każdym wierszu, po
przesłaniu dalej wygląda dokładnie jak wyciąg z prawdziwego konta — a to jest
pomyłka, której nie da się cofnąć po fakcie. Kolumna `tryb` jest pierwsza
i zawsze obecna.

FORMAT LICZB I DAT

Liczby idą z kropką dziesiętną i pełną precyzją, daty w ISO 8601 z jawną strefą
UTC. Bez zaokrągleń: plik ma być źródłem, a nie prezentacją. Wariant `excel`
dokłada BOM i średnik jako separator, bo polski Excel inaczej wsadza cały wiersz
do jednej komórki — dwie jawne opcje zamiast jednej, która jest zła dla połowy
odbiorców.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ..auth import audit, wymagaj_wlasciciela
from ..db import fetch_all

router = APIRouter(prefix="/api/engines/{engine}/eksport", tags=["eksport"])

# Nagłówek pliku. Kolejność jest częścią umowy z odbiorcą — arkusze i skrypty
# bywają pisane pod pozycje kolumn, nie pod ich nazwy.
KOLUMNY_WYPELNIEN = ("tryb", "czas_utc", "instrument", "strona", "ilosc", "cena",
                     "wartosc", "prowizja", "waluta_prowizji", "typ_zlecenia",
                     "id_zlecenia_gieldy", "id_wlasne", "id_decyzji")
KOLUMNY_POZYCJI = ("tryb", "instrument", "kierunek", "ilosc", "cena_wejscia",
                   "otwarta_utc", "zamknieta_utc", "wynik_zrealizowany",
                   "prowizje", "wynik_po_kosztach", "id_decyzji_otwarcia",
                   "id_decyzji_zamkniecia")


def zloz_csv(wiersze, kolumny, wariant: str) -> str:
    """Sama treść pliku. CZYSTA FUNKCJA, i to jest powód, dla którego istnieje.

    Wersja wpisana wprost w odpowiedź HTTP nie dawała się sprawdzić: ciało
    `StreamingResponse` jest generatorem asynchronicznym, więc test albo
    uruchamiałby serwer, albo grzebałby w polach prywatnych frameworka.
    Format pliku jest umową z odbiorcą spoza aplikacji — musi dać się sprawdzić
    wprost.
    """
    bufor = io.StringIO()
    # Średnik dla Excela, przecinek dla wszystkiego innego.
    pisarz = csv.writer(bufor, delimiter=";" if wariant == "excel" else ",",
                        lineterminator="\r\n")
    pisarz.writerow(kolumny)
    pisarz.writerows(wiersze)
    if wariant == "excel":
        # BOM: bez niego Excel czyta UTF-8 jako windows-1250 i psuje polskie znaki.
        return "﻿" + bufor.getvalue()
    return bufor.getvalue()


def _csv(wiersze, kolumny, nazwa: str, wariant: str) -> StreamingResponse:
    """Pakuje treść w pobranie.

    Bez strumieniowania z bazy: przy dzisiejszych wolumenach cały plik to
    kilkadziesiąt kilobajtów, a strumieniowanie wymagałoby trzymania kursora
    przez czas pobierania. Gdy pliki urosną do megabajtów, to jest miejsce
    do zmiany — i jedyne.
    """
    return StreamingResponse(
        io.BytesIO(zloz_csv(wiersze, kolumny, wariant).encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nazwa}"'},
    )


async def _odnotuj(widok: str, user: dict, request: Request | None, engine: str,
                   mode: str, od, do, wierszy: int) -> None:
    """Slad eksportu w `app.audit_log` (audyt Fable U-11).

    Eksport to cala historia transakcji w jednym pliku - jedyny odczyt w tym API,
    ktory wynosi dane poza aplikacje. Do 2026-10-03 nie zostawial sladu i byl
    dostepny dla kazdej waznej sesji, nie tylko wlasciciela.
    """
    await audit(f"eksport.{widok}", user_id=user["id"], entity="eksport",
                after={"engine": engine, "mode": mode,
                       "od": od.isoformat() if od else None,
                       "do": do.isoformat() if do else None, "wierszy": wierszy},
                request=request)


def _czas(v: datetime | None) -> str:
    return v.isoformat() if v else ""


@router.get("/wypelnienia")
async def wypelnienia(engine: str, request: Request,
                  user: dict = Depends(wymagaj_wlasciciela),
                  mode: str = Query("demo"),
                      od: datetime | None = None, do: datetime | None = None,
                      wariant: str = Query("csv", pattern="^(csv|excel)$")):
    """Pojedyncze wypełnienia — surowy materiał do rozliczeń."""
    rows = await fetch_all(
        """
        SELECT o.mode::text AS tryb, f.event_time, i.symbol, o.side, f.qty, f.price,
               f.fee, f.fee_ccy, o.order_type, o.venue_order_id, o.client_order_id,
               o.decision_id
        FROM exec.fill f
        JOIN exec.order o ON o.id = f.order_id
        JOIN ref.instrument i ON i.id = o.instrument_id
        WHERE o.engine::text = %s AND o.mode::text = %s
          AND (%s::timestamptz IS NULL OR f.event_time >= %s)
          AND (%s::timestamptz IS NULL OR f.event_time < %s)
        ORDER BY f.event_time, f.id
        """,
        (engine, mode, od, od, do, do))
    wiersze = [
        (r["tryb"], _czas(r["event_time"]), r["symbol"], r["side"],
         r["qty"], r["price"], r["qty"] * r["price"], r["fee"], r["fee_ccy"] or "",
         r["order_type"], r["venue_order_id"] or "", r["client_order_id"],
         r["decision_id"] or "")
        for r in rows
    ]
    nazwa = f"trader-{engine}-{mode}-wypelnienia.csv"
    await _odnotuj("wypelnienia", user, request, engine, mode, od, do, len(wiersze))
    return _csv(wiersze, KOLUMNY_WYPELNIEN, nazwa, wariant)


@router.get("/pozycje")
async def pozycje(engine: str, request: Request,
                  user: dict = Depends(wymagaj_wlasciciela),
                  mode: str = Query("demo"),
                  od: datetime | None = None, do: datetime | None = None,
                  wariant: str = Query("csv", pattern="^(csv|excel)$")):
    """Zamknięte pozycje — wynik po kosztach, gotowy do przeczytania."""
    rows = await fetch_all(
        """
        SELECT p.mode::text AS tryb, i.symbol, p.side, p.qty, p.avg_price,
               p.opened_at, p.closed_at, p.realized_pnl, p.fees_paid,
               p.open_decision_id, p.close_decision_id
        FROM exec.position p
        JOIN ref.instrument i ON i.id = p.instrument_id
        WHERE p.engine::text = %s AND p.mode::text = %s AND p.closed_at IS NOT NULL
          AND (%s::timestamptz IS NULL OR p.closed_at >= %s)
          AND (%s::timestamptz IS NULL OR p.closed_at < %s)
        ORDER BY p.closed_at, p.id
        """,
        (engine, mode, od, od, do, do))
    wiersze = [
        (r["tryb"], r["symbol"], r["side"], r["qty"], r["avg_price"],
         _czas(r["opened_at"]), _czas(r["closed_at"]),
         r["realized_pnl"], r["fees_paid"],
         # Wynik PO KOSZTACH liczony tutaj, a nie zostawiony odbiorcy.
         # Kolumna `realized_pnl` jest przed prowizją i pomylenie ich zawyża
         # wynik dokładnie o koszt handlu - ten sam blad, ktory projekt mial juz
         # raz w mierze walk-forward.
         r["realized_pnl"] - r["fees_paid"],
         r["open_decision_id"] or "", r["close_decision_id"] or "")
        for r in rows
    ]
    nazwa = f"trader-{engine}-{mode}-pozycje.csv"
    await _odnotuj("pozycje", user, request, engine, mode, od, do, len(wiersze))
    return _csv(wiersze, KOLUMNY_POZYCJI, nazwa, wariant)
