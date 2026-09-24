#!/usr/bin/env python3
"""Dziennik jakosciowy z zamknietych transakcji - warstwa "uczenia sie na bledach".

DLACZEGO TO POWSTAJE DOPIERO TERAZ

`learn.postmortem` istnieje od migracji 0006, typ zdarzenia `postmortem.ready`
od 0011, a endpoint `/api/engines/{e}/learning/postmortems` i zakladka w panelu
od poczatku. Przez ten czas NIC nie zapisalo do tej tabeli ani jednego wiersza:
czytal ja panel, kasowal `purge_demo`, a powiadomienie o gotowej analizie nie
zostalo wyslane nigdy. Mechanizm zbudowany i niewpiety - ten sam ksztalt, co
`ref.session_calendar` i `kind='news'` w dzienniku wkladow.

CO TEN PROCES LICZY SAM, A CO ODDAJE MODELOWI - I DLACZEGO TA GRANICA

Wszystkie LICZBY pochodzza z SQL-a w tym pliku: liczba pozycji, trafnosc, wynik,
prowizje, czas trzymania, rozklad po sygnalach. Model jezykowy dostaje je
GOTOWE i ma je zinterpretowac, a nie policzyc.

Granica nie jest ostroznoscia na wszelki wypadek. Model, ktory liczy, myli sie
w sposob NIEODROZNIALNY od wyniku: "trafnosc 58%" wygenerowane z rozpedu wyglada
dokladnie tak samo jak trafnosc policzona, a trafia do tabeli, ktora z zalozenia
jest pamiecia projektu. Zla liczba w dzienniku jakosciowym jest gorsza od braku
dziennika, bo nastepna decyzja bedzie sie na nia powolywac.

Stad tez `findings[].rodzaj`: kazdy wniosek modelu jest oznaczony jako HIPOTEZA
i niesie `oparte_na` z identyfikatorami pozycji, z ktorych powstal. Hipoteza,
ktorej nie da sie odniesc do konkretnych transakcji, nie nadaje sie do sprawdzenia
- a wniosek niesprawdzalny jest opowiescia, nie nauka.

CZEGO TEN PROCES NIE ROBI

Nie zmienia ani jednego parametru strategii. Wnioski trafiaja do dziennika dla
czlowieka i dla trenera jako material, a nie do `learn.model_version`. Petla
"model czyta wlasna ocene i sam sie poprawia" zamykalaby obieg bez zadnego
pomiaru z zewnatrz - czyli budowala przewage z wlasnego przekonania o niej.

Uzycie:
    DATABASE_URL=... python services/engine/postmortem.py --engine crypto --dni 7
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from services.ingest.llm import (BladOdpowiedzi, BrakDostawcy, Odmowa,  # noqa: E402
                                 Przeciazony, limit_dobowy_usd,
                                 limit_wywolan_dobowy, zbuduj_dostawce)
from services.ingest.news_worker import (odczytaj_klucze, record_spend,  # noqa: E402
                                         spent_today, wywolania_dzis,
                                         zamknij_dostawce)

# Ile pozycji trafia do promptu. Nie jest to oszczednosc na tokenach: dziennik
# z dwustu transakcji przestaje byc analiza, a staje sie streszczeniem, w ktorym
# kazdy wniosek pasuje do wszystkiego. Przy wiekszej liczbie bierzemy SKRAJNE
# (najlepsze i najgorsze) - bo to one niosa informacje o bledzie.
POZYCJI_MAX = 40

# Minimum, ponizej ktorego nie pytamy modelu w ogole. Wniosek z dwoch transakcji
# jest szumem opisanym zdaniem twierdzacym, a zapisany w dzienniku zyje dalej
# jak ustalenie.
POZYCJI_MIN = 5

SYSTEM = """Jesteś analitykiem ryzyka oceniającym zamknięte transakcje systemu \
tradingowego. Dostajesz POLICZONE statystyki okresu i listę pojedynczych pozycji \
wraz z uzasadnieniem, z jakim system je otwierał.

Twoje zadanie: wskazać POWTARZALNE wzorce błędu i zaproponować sprawdzalne \
działanie.

Zasady bezwzględne:
1. NIE LICZ NICZEGO. Wszystkie liczby masz podane. Nie podawaj własnych wartości \
   procentowych, średnich ani sum — jeśli chcesz się odwołać do liczby, zacytuj \
   tę, którą dostałeś.
2. Każdy wniosek musi wskazywać konkretne pozycje (pole `oparte_na`).
3. Jeśli dane nie wystarczają na wniosek, powiedz to wprost zamiast go tworzyć. \
   Pusta lista wniosków jest poprawną odpowiedzią.
4. `action` ma być czymś, co da się sprawdzić pomiarem, a nie ogólną radą \
   („ograniczyć ryzyko" nie jest działaniem; „sprawdzić, czy wyjście po 2 \
   świecach zamiast 6 poprawia wynik na tym sygnale" jest).
5. Nie zakładaj, że przewaga istnieje. Wynik ujemny na małej próbce może być \
   szumem i wolno Ci tak napisać."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-4 zdania o tym, co ten okres pokazuje. Bez własnych liczb.",
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string", "description": "Zaobserwowany wzorzec."},
                    "action": {"type": "string", "description": "Sprawdzalne działanie."},
                    "oparte_na": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "id pozycji, z których wniosek powstał.",
                    },
                },
                "required": ["finding", "action", "oparte_na"],
            },
        },
    },
    "required": ["summary", "findings"],
}


# ----------------------------------------------------------------- pomiar
SQL_POZYCJE = """
SELECT p.id, i.symbol, p.side, p.qty, p.avg_price,
       p.realized_pnl, p.fees_paid, p.opened_at, p.closed_at,
       round(extract(epoch from (p.closed_at - p.opened_at)) / 3600.0, 1) AS godzin,
       d.regime, d.confidence, d.rationale,
       (SELECT string_agg(dc.label || ' ' || round(dc.contribution::numeric, 3), '; '
                          ORDER BY abs(dc.contribution) DESC)
          FROM journal.decision_contribution dc
         WHERE dc.decision_id = d.id AND dc.kind = 'signal'
           AND abs(dc.contribution) > 0) AS sygnaly,
       (SELECT string_agg(dc.label, ' | ' ORDER BY abs(dc.value) DESC)
          FROM journal.decision_contribution dc
         WHERE dc.decision_id = d.id AND dc.kind = 'news') AS newsy
  FROM exec.position p
  JOIN ref.instrument i ON i.id = p.instrument_id
  LEFT JOIN journal.decision d ON d.id = p.open_decision_id
 WHERE p.engine::text = %s AND p.mode::text = %s
   AND p.closed_at IS NOT NULL
   AND p.closed_at > %s AND p.closed_at <= %s
 ORDER BY p.closed_at
"""


def statystyki(pozycje: list[dict]) -> dict:
    """Liczby okresu - POLICZONE TUTAJ, nie przez model. Patrz naglowek modulu.

    BEZ ROZROZNIENIA "rozliczone / nierozliczone", i to jest decyzja o schemacie,
    a nie przeoczenie. `exec.position.realized_pnl` jest NOT NULL DEFAULT 0,
    a bierzemy wylacznie wiersze z `closed_at IS NOT NULL` - wiec kazda pozycja
    w tej probce MA wynik. Licznik "bez wyniku" bylby tu martwy: pokazywalby
    zawsze zero i sugerowal, ze cos jest pilnowane.
    (Inaczej niz w `learning.signal_performance`, gdzie NULL bierze sie
    z LEFT JOIN-a do pozycji, ktora jeszcze nie powstala - tam rozroznienie
    jest realne i zostaje.)

    ZERO NIE JEST WYGRANA. Warunek jest ostry (`> 0`), bo pozycja zamknieta
    dokladnie na wejsciu oddala prowizje i nie zarobila nic.
    """
    wygrane = [p for p in pozycje if p["realized_pnl"] > 0]
    suma = sum(float(p["realized_pnl"]) for p in pozycje)
    prowizje = sum(float(p["fees_paid"] or 0) for p in pozycje)
    godziny = [float(p["godzin"]) for p in pozycje if p["godzin"] is not None]
    return {
        "pozycji": len(pozycje),
        "wygranych": len(wygrane),
        "trafnosc": round(len(wygrane) / len(pozycje), 4) if pozycje else None,
        "wynik_netto": round(suma, 4),
        "prowizje": round(prowizje, 4),
        # Prowizje wobec wyniku BRUTTO - liczba, ktora w tym projekcie decyduje
        # o wszystkim (docs/08, brama 0). Bez niej "wynik -2,3%" nie mowi, czy
        # strategia przegrala z rynkiem, czy z wlasnym kosztem.
        "wynik_brutto": round(suma + prowizje, 4),
        "godzin_mediana": round(sorted(godziny)[len(godziny) // 2], 1) if godziny else None,
    }


def opis_pomiaru(s: dict) -> str:
    """Liczby zdaniem - trafia na POCZATEK `summary`, przed narracja modelu.

    Kolejnosc jest tu trescia: czytelnik panelu widzi najpierw to, co zmierzone,
    a dopiero potem to, co zinterpretowane. Odwrotna kolejnosc uczy czytac
    interpretacje jako wynik.
    """
    traf = "—" if s["trafnosc"] is None else f"{s['trafnosc']:.1%}"
    return (f"POMIAR: {s['pozycji']} zamkniętych pozycji, trafność {traf} "
            f"({s['wygranych']}/{s['pozycji']}), wynik netto {s['wynik_netto']:+.4f}, "
            f"prowizje {s['prowizje']:.4f} (brutto {s['wynik_brutto']:+.4f}), "
            f"mediana trzymania {s['godzin_mediana']} h.")


def probka_do_promptu(pozycje: list[dict]) -> list[dict]:
    """Ktore pozycje pokazujemy modelowi, gdy jest ich za duzo.

    SKRAJNE, nie pierwsze z brzegu. Przy obcieciu `ORDER BY closed_at LIMIT n`
    model ogladalby poczatek okresu i nazwal go calym okresem. Blad mieszka
    w ogonach rozkladu, wiec to one maja byc widoczne - a fakt obciecia jest
    powiedziany w promptcie wprost, zeby model nie wzial probki za populacje.
    """
    if len(pozycje) <= POZYCJI_MAX:
        return pozycje
    wg_wyniku = sorted(pozycje, key=lambda p: float(p["realized_pnl"]))
    polowa = POZYCJI_MAX // 2
    return wg_wyniku[:polowa] + wg_wyniku[-polowa:]


def wiersz_promptu(p: dict) -> str:
    wynik = f"{float(p['realized_pnl']):+.4f}"
    czesci = [
        f"#{p['id']} {p['symbol']} {p['side']} wynik {wynik}",
        f"trzymane {p['godzin']} h" if p["godzin"] is not None else "",
        f"reżim {p['regime']}" if p["regime"] else "",
        f"pewność {p['confidence']:.0%}" if p["confidence"] is not None else "",
        f"sygnały: {p['sygnaly']}" if p["sygnaly"] else "sygnały: brak zapisu",
        f"newsy: {p['newsy']}" if p["newsy"] else "",
        f"uzasadnienie: {p['rationale']}" if p["rationale"] else "",
    ]
    return "  " + " | ".join(c for c in czesci if c)


def zbuduj_prompt(engine: str, od: datetime, do: datetime,
                  s: dict, pozycje: list[dict], probka: list[dict]) -> str:
    naglowek = [
        f"Silnik: {engine}. Okres: {od:%Y-%m-%d %H:%M} - {do:%Y-%m-%d %H:%M} UTC.",
        "",
        "STATYSTYKI OKRESU (policzone, nie licz ich ponownie):",
        f"  pozycji zamkniętych: {s['pozycji']}",
        f"  wygranych: {s['wygranych']}",
        f"  trafność: {s['trafnosc']}",
        f"  wynik netto: {s['wynik_netto']}",
        f"  prowizje: {s['prowizje']}",
        f"  wynik brutto (przed prowizjami): {s['wynik_brutto']}",
        f"  mediana czasu trzymania (h): {s['godzin_mediana']}",
        "",
    ]
    if len(probka) < len(pozycje):
        naglowek.append(
            f"POZYCJE: pokazano {len(probka)} z {len(pozycje)} — skrajne wyniki "
            f"(najgorsze i najlepsze). To NIE jest cały okres; statystyki wyżej "
            f"dotyczą wszystkich {len(pozycje)} pozycji.")
    else:
        naglowek.append(f"POZYCJE (wszystkie {len(pozycje)}):")
    return "\n".join(naglowek + [wiersz_promptu(p) for p in probka])


# ----------------------------------------------------------------- przebieg
def okres(cur, engine: str, mode: str, dni: int,
          teraz: datetime) -> tuple[datetime, datetime]:
    """Od konca poprzedniego dziennika, albo `dni` wstecz przy pierwszym razie.

    Dzieki temu okresy nie zachodza na siebie i ta sama transakcja nie jest
    oceniana dwa razy - a przebieg opuszczony (padniety Job, przestoj klastra)
    nadrabia sie sam przy nastepnym uruchomieniu, zamiast zostawiac dziure.
    """
    cur.execute("SELECT max(period_to) AS do FROM learn.postmortem"
                " WHERE engine::text = %s", (engine,))
    row = cur.fetchone()
    poprzedni = row["do"] if row else None
    return (poprzedni or teraz - timedelta(days=dni)), teraz


async def zbuduj(conn, engine: str, mode: str, dni: int, sucho: bool,
                 teraz: datetime | None = None) -> int:
    cur = conn.cursor()
    teraz = teraz or datetime.now(timezone.utc)
    od, do = okres(cur, engine, mode, dni, teraz)
    print(f"Post-mortem {engine}/{mode}: okres {od:%Y-%m-%d %H:%M} - {do:%Y-%m-%d %H:%M} UTC")

    cur.execute(SQL_POZYCJE, (engine, mode, od, do))
    pozycje = cur.fetchall()
    if len(pozycje) < POZYCJI_MIN:
        # CISZA JEST WYNIKIEM, wiec musi byc widoczna - i musi powiedziec, ile
        # brakuje. "Brak analizy" bez liczby nie odroznia pustego okresu od
        # zepsutego zapytania.
        print(f"  {len(pozycje)} zamkniętych pozycji, próg to {POZYCJI_MIN} — "
              f"nie pytam modelu. Wniosek z takiej próbki byłby szumem "
              f"zapisanym jak ustalenie.")
        return 0

    s = statystyki(pozycje)
    print(f"  {opis_pomiaru(s)}")

    # BRAMKI BUDZETU - OBIE, i sprawdzane PRZED wywolaniem.
    #
    # To ten sam dziennik (`news.api_spend`) i te same dwa progi, ktorymi chodzi
    # skaner wiadomosci. Osobny licznik dla tego procesu znaczylby, ze dwa
    # procesy wydaja z jednego konta, pilnujac dwoch roznych sum - czyli ze
    # zaden z nich nie pilnuje niczego. `purpose='postmortem'` rozroznia je
    # w rozliczeniu, nie w limicie.
    wydano, wywolan = spent_today(cur), wywolania_dzis(cur)
    if wydano >= limit_dobowy_usd():
        print(f"  limit dobowy ${limit_dobowy_usd():.2f} osiągnięty "
              f"(wydano ${wydano:.4f}) — nie pytam modelu")
        return 0
    if wywolan >= limit_wywolan_dobowy():
        print(f"  limit {limit_wywolan_dobowy()} wywołań na dobę osiągnięty "
              f"({wywolan}) — nie pytam modelu")
        return 0

    probka = probka_do_promptu(pozycje)
    prompt = zbuduj_prompt(engine, od, do, s, pozycje, probka)
    if sucho:
        print("\n--- tryb suchy, prompt poniżej, nic nie wysyłam ---")
        print(prompt)
        return 0

    ai = zbuduj_dostawce(odczytaj_klucze(conn))
    if ai is None:
        # BRAK DOSTAWCY JEST AWARIA TEGO ZADANIA, nie jego pustym przebiegiem.
        # Job na czerwono; cichy `return 0` znaczylby "przeanalizowano" przy
        # koncie bez klucza.
        print("  BRAK KLUCZA dostawcy analizy — nie ma czym zbudować dziennika")
        return 1
    try:
        dane, usage = await ai.zapytaj(
            model=ai.model_analizy, system=SYSTEM, user=prompt,
            schema=SCHEMA, max_tokens=4000)
    except (Odmowa, BrakDostawcy, Przeciazony, BladOdpowiedzi) as e:
        print(f"  dostawca nie odpowiedział: {type(e).__name__}: {e}")
        record_spend(cur, ai.model_analizy, "postmortem",
                     getattr(e, "usage", {}) or {}, 0.0, ok=False)
        conn.commit()
        return 1
    finally:
        await zamknij_dostawce(ai)

    # MODEL I KOSZT Z DOSTAWCY, NIE Z NASZEGO ZADANIA.
    #
    # Mieszanka dostawcow schodzi przy limicie na model zapasowy, wiec
    # `ai.model_analizy` jest tym, o co PROSILISMY, a `uzyty_model` tym, ktory
    # naprawde pracowal. Dziennik wydatkow zapisujacy pierwszy pokazywalby
    # tansza stawke za prace, ktorej ten model nie wykonal - i ta sama nazwa
    # trafilaby do `learn.postmortem.model`, czyli do pamieci projektu.
    model_uzyty = ai.uzyty_model(ai.model_analizy, usage)
    koszt = ai.koszt(model_uzyty, usage)
    record_spend(cur, model_uzyty, "postmortem", usage, koszt, ok=True)

    # KAZDY WNIOSEK DOSTAJE ETYKIETE I OPARCIE. Panel czyta `finding`/`action`
    # i pozostale pola ignoruje, wiec dopisanie ich nic nie psuje - a bez nich
    # zdanie modelu lezaloby w bazie nieodroznialne od ustalenia.
    findings = [
        {"finding": f.get("finding", ""), "action": f.get("action", ""),
         "rodzaj": "hipoteza", "oparte_na": f.get("oparte_na", [])}
        for f in (dane.get("findings") or [])
    ]
    summary = f"{opis_pomiaru(s)}\n\n{dane.get('summary', '').strip()}"

    cur.execute(
        "INSERT INTO learn.postmortem (engine, period_from, period_to, model,"
        " summary, findings, position_ids) VALUES (%s,%s,%s,%s,%s,%s,%s)"
        " RETURNING id",
        (engine, od, do, model_uzyty, summary, Jsonb(findings),
         [p["id"] for p in pozycje]))
    pid = cur.fetchone()["id"]
    conn.commit()
    print(f"  zapisano post-mortem #{pid}: {len(findings)} hipotez, "
          f"koszt ${koszt:.4f}")

    await powiadom(engine, pid, s, len(findings))
    return 0


async def powiadom(engine: str, pid: int, s: dict, hipotez: int) -> None:
    """`postmortem.ready` - typ istniejacy od migracji 0011 i NIGDY nie wyslany.

    Blad tutaj NIE przewraca zadania: dziennik jest juz w bazie i widoczny
    w panelu, wiec nieudane powiadomienie nie moze zamienic udanej analizy
    w czerwonego Joba. Idzie do logu i tyle - odwrotnie niz przy czujkach, gdzie
    powiadomienie JEST produktem.
    """
    try:
        from services.api.db import close_pool, fetch_all, open_pool
        from services.api.notifications import notify
    except Exception as e:                                    # noqa: BLE001
        print(f"  [uwaga] powiadomienie pominięte: {type(e).__name__}: {e}")
        return
    try:
        await open_pool()
        traf = "—" if s["trafnosc"] is None else f"{s['trafnosc']:.1%}"
        for u in await fetch_all('SELECT id FROM app."user" WHERE is_active'):
            await notify(
                u["id"], "postmortem.ready", f"Nowa analiza post-mortem ({engine})",
                f"{s['pozycji']} zamkniętych pozycji, trafność {traf}, "
                f"wynik {s['wynik_netto']:+.4f}. Hipotez: {hipotez}.",
                data={"postmortem_id": pid, "engine": engine})
    except Exception as e:                                    # noqa: BLE001
        print(f"  [uwaga] powiadomienie nie poszło: {type(e).__name__}: {e}")
    finally:
        try:
            await close_pool()
        except Exception:                                     # noqa: BLE001
            pass


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo", choices=("demo", "live"))
    ap.add_argument("--dni", type=int, default=7,
                    help="okno PIERWSZEGO przebiegu; kolejne liczą się od końca "
                         "poprzedniego dziennika")
    ap.add_argument("--sucho", action="store_true",
                    help="zbuduj prompt i wypisz go, nie pytaj modelu")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL nie jest ustawiony")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return await zbuduj(conn, args.engine, args.mode, args.dni, args.sucho)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
