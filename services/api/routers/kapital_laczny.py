"""Kapitał ŁĄCZNY obu platform na jednej siatce czasu.

Krzywa per silnik żyje w `exec.equity_point` w rytmie własnego silnika
(krypto: co cykl, 24/7; equity: w sesji). Sumowanie „po wierszach" dałoby
sumę dwóch różnych chwil. Dlatego siatka jest wspólna (godzinowa), a wartość
silnika w punkcie siatki to jego OSTATNI znany kapitał nie później niż ten
punkt — czyli to, co naprawdę było wtedy na rachunku, także w nocy i w weekend,
gdy giełda tradycyjna stoi.

Silnik bez punktu w danej chwili wchodzi jako brak — nie jako zero — a suma
istnieje tylko tam, gdzie znane są WSZYSTKIE silniki. Inaczej „Razem"
kłamałoby dwa razy: o spadku (zero) albo o zysku (skok o kapitał drugiej
platformy w chwili jej pierwszego punktu).

SUMA ISTNIEJE TYLKO W JEDNEJ WALUCIE

`exec.equity_point` NIE MA kolumny waluty (migracja 0005): jednostka wynika
z tego, który proces pisze wiersz. Krypto liczy w USDC (uniwersum decyzyjne to
pary USDC, migracja 0023; `app.waluta_odniesienia.kod_mierzony` z 0036 mówi to
wprost), a equity w USD — bo `kapital_gieldy.py` przepisuje stan konta paper
Alpaki, a tamte instrumenty mają `quote_ccy = 'USD'` (migracja 0047).

`p["razem"] = sum(wart)` dodawało więc dolary do USDC i podawało wynik jako
jedną liczbę, na dodatek grubą linią. Przeliczenia NIE ROBIMY, i to jest
decyzja, a nie zaniechanie: `market.fx_rate` (0036) trzyma wyłącznie parę
waluty POMIARU do waluty ODNIESIENIA właściciela — `waluta.py` zna dokładnie
dwa źródła, USDCEUR i USDCPLN. Kursu USD/USDC nikt nie zbiera i nie ma go skąd
wziąć, a podstawienie 1:1 („stablecoin, więc mniej więcej dolar") byłoby
dokładnie tym samym błędem co zero za brak pomiaru: liczbą wyglądającą na
zmierzoną. Ten sam wybór zapadł już raz, w `waluta.py`: „BRAK KURSU TO LUKA,
NIE ZERO".

Dlatego przy różnych walutach „Razem" jest `null` z podanym powodem, a każda
seria niesie swoją walutę. Prawdziwym domknięciem tego tematu jest kolumna
waluty w `exec.equity_point` plus zbieranie kursu USD — jedno i drugie to
migracja, czyli osobna, świadoma zmiana.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query

from ..db import fetch_all

router = APIRouter(prefix="/api/kapital", tags=["kapital"])

# Odstep, ktory kafel "24 h" ma naprawde opisywac.
DOBA = timedelta(hours=24)


@router.get("/laczny")
async def laczny(days: int = Query(45, ge=1, le=365), mode: str = Query("demo")):
    rows = await fetch_all(
        """
        WITH siatka AS (
            SELECT generate_series(
                date_trunc('hour', now() - make_interval(days => %s)),
                date_trunc('hour', now()), interval '1 hour') AS t
            UNION SELECT now()   -- ostatni punkt to TERAZ, nie pelna godzina
        ),
        silniki AS (
            SELECT DISTINCT engine::text AS engine FROM exec.equity_point
            WHERE mode::text = %s
        )
        SELECT s.t, e.engine, p.equity
        FROM siatka s
        CROSS JOIN silniki e
        LEFT JOIN LATERAL (
            SELECT equity FROM exec.equity_point q
            WHERE q.engine::text = e.engine AND q.mode::text = %s AND q.event_time <= s.t
            ORDER BY q.event_time DESC LIMIT 1
        ) p ON true
        ORDER BY s.t, e.engine
        """,
        (days, mode, mode),
    )
    # WALUTA SILNIKA - z bazy, bo `exec.equity_point` jej nie zapisuje.
    #
    # Bierzemy uniwersum DECYZYJNE (`tradeable`), bo to w tej walucie silnik
    # zawiera transakcje. Silnik, ktory nie ma jeszcze ani jednego instrumentu
    # dopuszczonego do handlu - a taki jest dzis equity, migracja 0047 ustawia
    # `tradeable = false` wszystkim - opisuje waluta jego instrumentow
    # AKTYWNYCH, czyli tych, z ktorych gielda liczy jego rachunek.
    #
    # Niejednoznaczny wynik (kilka walut) zostaje `None`. Wybranie "pierwszej"
    # byloby zgadywaniem jednostki, a jednostka zgadnieta jest gorsza niz jej
    # brak: liczba wyglada wtedy na porownywalna.
    waluty_rows = await fetch_all(
        """
        SELECT engine::text AS engine,
               array_agg(DISTINCT quote_ccy) FILTER (WHERE tradeable) AS handlowe,
               array_agg(DISTINCT quote_ccy)                          AS wszystkie
        FROM ref.instrument
        WHERE active
        GROUP BY 1
        """)
    waluty: dict[str, str | None] = {}
    for r in waluty_rows:
        kandydaci = r["handlowe"] or r["wszystkie"] or []
        waluty[r["engine"]] = kandydaci[0] if len(kandydaci) == 1 else None

    silniki = sorted({r["engine"] for r in rows})
    waluty = {e: waluty.get(e) for e in silniki}

    # CZY SUMA W OGOLE MA JEDNOSTKE. Patrz naglowek modulu.
    nieznane = [e for e in silniki if waluty[e] is None]
    rozne = sorted({w for w in waluty.values() if w})
    if not silniki:
        razem_powod = "Żaden silnik nie zapisał jeszcze punktu kapitału."
    elif nieznane:
        razem_powod = (
            "Nie da się ustalić waluty, w której mierzy: "
            + ", ".join(nieznane)
            + " — suma dwóch nieznanych jednostek nie jest liczbą.")
    elif len(rozne) > 1:
        razem_powod = (
            "Silniki mierzą w różnych walutach ("
            + ", ".join(f"{e}: {waluty[e]}" for e in silniki)
            + "). Kursu między nimi nie ma w market.fx_rate — tabela trzyma "
              "wyłącznie kurs waluty pomiaru do waluty odniesienia właściciela "
              "(USDC/EUR, USDC/PLN). Podstawienie 1:1 dałoby liczbę wyglądającą "
              "na przeliczoną, więc suma nie jest podawana.")
    else:
        razem_powod = None

    punkty: dict = {}
    for r in rows:
        p = punkty.setdefault(r["t"], {"t": r["t"].isoformat()})
        p[r["engine"]] = float(r["equity"]) if r["equity"] is not None else None
    seria, czasy = [], []
    for t, p in punkty.items():
        # SUMA TYLKO Z KOMPLETU I TYLKO W JEDNEJ WALUCIE. Suma z jednego
        # silnika, gdy drugi jeszcze nie ma danych, dalaby "Razem" rowne krypto,
        # a potem skok o caly kapital gieldy w chwili jej pierwszego punktu -
        # wygladaloby to jak zysk.
        wart = [p.get(e) for e in silniki]
        p["razem"] = (sum(wart) if razem_powod is None
                      and all(v is not None for v in wart) else None)
        seria.append(p)
        czasy.append(t)

    def zmiana(klucz: str) -> dict:
        znane = [(t, p[klucz]) for t, p in zip(czasy, seria) if p.get(klucz) is not None]
        if not znane:
            return {"teraz": None, "od_startu": None, "od_startu_pct": None,
                    "doba": None, "doba_pct": None}
        teraz_t, teraz = znane[-1]
        start = znane[0][1]
        # ODNIESIENIE "24 H" WYBIERANE PO CZASIE, NIE PO LICZBIE KROKOW SIATKI.
        #
        # `wart[-25]` cofalo o 24 kroki, ale ostatnim punktem siatki jest
        # `now()`, a nie pelna godzina (UNION w zapytaniu wyzej) - realny odstep
        # wynosil wiec 23 h plus to, co uplynelo z biezacej godziny. Drugi blad
        # byl grozniejszy: przy krotszej serii odniesieniem stawal sie PIERWSZY
        # punkt, czyli kafel "24 h" pokazywal zmiane od startu - te sama liczbe
        # co kafel "okno", tuz obok, bez zadnej adnotacji. Brak punktu sprzed
        # doby jest brakiem pomiaru i tak ma byc podany.
        granica = teraz_t - DOBA
        starsze = [v for t, v in znane if t <= granica]
        dobe_temu = starsze[-1] if starsze else None
        return {
            "teraz": teraz,
            "od_startu": teraz - start,
            "od_startu_pct": (teraz / start - 1) * 100 if start else None,
            "doba": (teraz - dobe_temu) if dobe_temu is not None else None,
            "doba_pct": (teraz / dobe_temu - 1) * 100 if dobe_temu else None,
        }

    return {
        "silniki": silniki,
        # Waluta PRZY KAZDEJ SERII, nie jedna na wykres. Bez niej dwie linie
        # w roznych jednostkach wygladaja na porownywalne.
        "waluty": waluty,
        "punkty": seria,
        "zmiany": {**{e: zmiana(e) for e in silniki}, "razem": zmiana("razem")},
        # `null` znaczy "suma jest policzona". Tekst znaczy "nie jest, i oto
        # dlaczego" - powod idzie na ekran, a nie do logu.
        "razem_powod": razem_powod,
    }
