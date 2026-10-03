"""Kanarki na LICZNIK PRÓB triażu: czy mierzy to, co ma mierzyć.

Dwa rozjazdy między tym, co kod deklarował, a tym, co robiła produkcja. Oba
były ciche: żaden pod nie padł, żaden licznik błędów nie urósł, trzynaście
testów świeciło na zielono.

CO ZAPISAŁA BAZA 11.09.2026 MIĘDZY 14:13 A 22:10

    triage_reason: 'BrakDostawcy: obaj dostawcy odcieci — gemini: wyczerpany
                    limit DOBOWY …; anthropic: awaria TRWALA: brak srodkow…'
    liczba: 106,  triage_attempts: 3 dla WSZYSTKICH 106
    triage_at - ingested_at: 301 s dla każdego próbkowanego

Trzy próby z odstępem PRZERWA_PO_AWARII_S = 900 s wymagają co najmniej 1800 s
między pierwszą a ostatnią. Zmierzone: 301 s, czyli jeden cykl — odstęp nie
obowiązywał, bo `SELECT` w `nadrob_zaleglosci` nie pobierał `triage_at`.
Do tego żaden z tych 106 artykułów nie został ani razu oceniony: `BrakDostawcy`
znaczy „wywołanie NIE poszło do API" (kontrakt w `llm.Dostawca`), a mimo to
podbijało licznik prób. Licznik mówił „3 próby" tam, gdzie prób było zero.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


from services.ingest import news_worker
from services.ingest.llm import BrakDostawcy

import test_petla_triazu as tpt

ZRODLO = (Path(__file__).resolve().parents[1]
          / "services/ingest/news_worker.py").read_text(encoding="utf-8")


def teraz_minus(s: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=s)


class AtrapaOdcieta(tpt.AtrapaDostawcy):
    """Obaj dostawcy odcięci przez bezpiecznik — stan produkcji z 11.09."""

    def czy_ma_czym_pytac(self) -> bool:
        return False

    def powod_niedostepnosci(self) -> str:
        return "gemini: wyczerpany limit DOBOWY; anthropic: awaria TRWALA"


# ==================================== odstęp między próbami faktycznie działa

def test_PRZERWA_PO_AWARII_obowiazuje_gdy_baza_oddaje_tylko_kolumny_z_SELECT(
        monkeypatch):
    """Artykuł, którego triaż padł przed chwilą, NIE MA iść do modelu od razu.

    Przechodzi tylko wtedy, gdy zapytanie o zaległości POBIERA `triage_at`:
    atrapa oddaje wyłącznie kolumny wymienione w `SELECT`-cie, więc brak
    kolumny znaczy tu dokładnie to samo, co na produkcji — `None`, czytane
    przez `wolno_ponowic` jako „dawno".
    """
    baza = tpt.AtrapaNewsow([tpt.nowy_artykul(
        "Fed podniósł stopy o 25 pb", triage_verdict="failed", triage_attempts=1,
        triage_reason="503 u dostawcy", triage_at=teraz_minus(5),
        event_time=teraz_minus(3600))])
    ocenione = tpt._licznik_triazu(monkeypatch, worth=False)
    tpt._licznik_analizy(monkeypatch)

    asyncio.run(news_worker.nadrob_zaleglosci(
        baza, baza, tpt.AtrapaDostawcy(), {}, [], tpt.swiezy_budzet(),
        tpt.puste_stats(), verbose=False))

    assert ocenione == [], (
        f"triaż ponowiony {len(ocenione)} raz 5 s po awarii, choć "
        f"PRZERWA_PO_AWARII_S = {news_worker.PRZERWA_PO_AWARII_S} s")


def test_SELECT_zaleglosci_pobiera_kolumny_ktore_czyta_polityka_ponawiania():
    """Ten sam warunek widziany wprost w źródle — bez atrapy i bez uruchamiania."""
    sql = re.search(r'"SELECT a\.id,(.*?)ORDER BY a\.event_time DESC"', ZRODLO, re.S)
    assert sql, "nie znaleziono zapytania o zaległości"
    # `\b` na końcu, bo "a.triage_attempts" ZACZYNA SIĘ od "a.triage_at" —
    # test na goły podciąg przechodził w obie strony i nie pilnował niczego.
    for kolumna in ("triage_at", "analysis_at"):
        assert re.search(rf"\ba\.{kolumna}\b", sql.group(0)), (
            f"a.{kolumna} nie jest pobierane, a `wolno_ponowic` na nim stoi")


# ==================================== BrakDostawcy nie jest próbą u dostawcy

def test_BRAK_DOSTAWCY_nie_zjada_budzetu_prob(monkeypatch):
    """`BrakDostawcy` znaczy „wywołanie NIE poszło do API" (kontrakt `llm.Dostawca`).

    Nikt nie został zapytany, więc nie ma czego liczyć jako próbę. Przed
    poprawką każdy cykl dopisywał artykułowi próbę, więc po trzech cyklach —
    kwadransie ciszy u dostawcy — artykuł był martwy NA ZAWSZE, nie zostawszy
    ani razu ocenionym.
    """
    wpis = tpt.Wpis("Fed podniósł stopy o 25 pb")

    async def pobierz(client, url):
        return [wpis], "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    tpt._licznik_triazu(monkeypatch, wyjatek=BrakDostawcy(
        "obaj dostawcy odcieci — gemini: wyczerpany limit DOBOWY"))
    tpt._licznik_analizy(monkeypatch)

    baza = tpt.AtrapaNewsow()
    ai = AtrapaOdcieta()
    for _ in range(news_worker.PROBY_PO_AWARII_MAX + 1):
        asyncio.run(news_worker.run_cycle(baza, ai, http=None, verbose=False))

    artykul = baza.po_tytule("Fed podniósł stopy o 25 pb")
    assert artykul["triage_attempts"] == 0, (
        f"budżet ponowień zjedzony ({artykul['triage_attempts']} z "
        f"{news_worker.PROBY_PO_AWARII_MAX}) przez odmowy WŁASNEGO bezpiecznika, "
        f"bez ani jednego wywołania API")
    assert artykul["triage_verdict"] is None, (
        "odcięty bezpiecznik zapisał artykułowi werdykt, choć żaden model go "
        "nie widział")
    assert news_worker.etap_do_wykonania(artykul) == "triaz", (
        "artykuł nigdy nie wróci na triaż, choć nie został oceniony ani razu")


def test_artykul_przezywa_odciecie_dostawcy_i_wraca_gdy_dostawca_wroci(monkeypatch):
    """Oba błędy razem — dokładnie scenariusz z 11.09, tyle że dokończony.

    Kwadrans ciszy u dostawcy skasował wtedy całe okno newsów. Po poprawce ten
    sam kwadrans ma być NIEWIDOCZNY w wyniku: artykuł czeka i zostaje oceniony,
    gdy dostawca wraca.
    """
    wpis = tpt.Wpis("Fed podniósł stopy o 25 pb")

    async def pobierz(client, url):
        return [wpis], "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    tpt._licznik_triazu(monkeypatch, wyjatek=BrakDostawcy("obaj dostawcy odcieci"))
    tpt._licznik_analizy(monkeypatch)

    baza = tpt.AtrapaNewsow()
    for _ in range(news_worker.PROBY_PO_AWARII_MAX):
        asyncio.run(news_worker.run_cycle(baza, AtrapaOdcieta(), http=None,
                                          verbose=False))

    # Dostawca wraca. Artykuł ma jeszcze prawo pójść na triaż.
    ocenione = tpt._licznik_triazu(monkeypatch, worth=False)
    asyncio.run(news_worker.run_cycle(baza, tpt.AtrapaDostawcy(), http=None,
                                      verbose=False))

    assert ocenione == ["Fed podniósł stopy o 25 pb"], (
        "artykuł nie wrócił na triaż po powrocie dostawcy — budżet prób zjadło "
        "odcięcie, w którym nikogo nie pytano")


class AtrapaZwierajacaSieWTrakcie(tpt.AtrapaDostawcy):
    """Żywa na wejściu w pętlę, odcięta przy pierwszym artykule.

    Ten stan jest REALNY, a nie wymyślony dla testu: pierwszy artykuł dostaje
    `Wyczerpany` i dopiero PRZEZ NIEGO bezpiecznik odcina dostawcę, więc
    warunek sprawdzony przed pętlą jeszcze o niczym nie wie. Dlatego sam
    strażnik na wejściu nie wystarcza i obsługa wyjątku musi mówić to samo.
    """

    def __init__(self):
        self._zywa = True

    def czy_ma_czym_pytac(self) -> bool:
        return self._zywa

    def powod_niedostepnosci(self) -> str:
        return "" if self._zywa else "anthropic: brak srodkow na koncie"


def test_BRAK_DOSTAWCY_rzucony_W_TRAKCIE_petli_tez_nie_jest_proba(monkeypatch):
    """Strażnik na wejściu pętli tego nie złapie — bezpiecznik zwiera się później."""
    wpis = tpt.Wpis("Fed podniósł stopy o 25 pb")
    ai = AtrapaZwierajacaSieWTrakcie()

    async def pobierz(client, url):
        return [wpis], "ok"

    async def triaz(_ai, tytul, zajawka):
        ai._zywa = False
        raise BrakDostawcy("anthropic: brak srodkow na koncie")

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    monkeypatch.setattr(news_worker, "triage", triaz)
    tpt._licznik_analizy(monkeypatch)

    baza = tpt.AtrapaNewsow()
    asyncio.run(news_worker.run_cycle(baza, ai, http=None, verbose=False))

    artykul = baza.po_tytule("Fed podniósł stopy o 25 pb")
    assert artykul["triage_attempts"] == 0 and artykul["triage_verdict"] is None, (
        "zwarcie bezpiecznika W TRAKCIE pętli zapisało artykułowi próbę i werdykt, "
        "choć wywołanie nie poszło do API")


def test_odciety_dostawca_NIE_ZATRZYMUJE_zapisywania_artykulow(monkeypatch):
    """Kontrakt `llm.Dostawca` wprost: „MA DALEJ ZAPISYWAĆ artykuły".

    Zapis stoi w pętli WYŻEJ niż analiza, więc `break` na strażniku ucinał
    razem z analizą także zapis pozostałych wpisów kanału. Kanał RSS nie odda
    jutro tego, czego dziś nie pobraliśmy — kilkugodzinne odcięcie dostawcy
    kasowałoby materiał BEZPOWROTNIE, a nie tylko z opóźnieniem.
    """
    wpisy = [tpt.Wpis(f"Wiadomość {i}") for i in range(5)]

    async def pobierz(client, url):
        return wpisy, "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    tpt._licznik_triazu(monkeypatch, worth=False)
    tpt._licznik_analizy(monkeypatch)

    baza = tpt.AtrapaNewsow()
    s = asyncio.run(news_worker.run_cycle(baza, AtrapaOdcieta(), http=None,
                                          verbose=False))

    assert s["zapisane"] == 5, (
        f"zapisano {s['zapisane']} z 5 wpisów — odcięty dostawca zatrzymał "
        f"zbieranie darmowego materiału, nie tylko płatne decyzje")
    assert s["pominiete"] == 5
    assert all(a["triage_verdict"] is None for a in baza.artykuly)


def test_wyczerpany_budzet_TEZ_nie_zatrzymuje_zapisywania(monkeypatch):
    """Ta sama zasada dla drugiej bramki: budżet ogranicza WYDAWANIE, nie zbieranie."""
    wpisy = [tpt.Wpis(f"Wiadomość {i}") for i in range(4)]

    async def pobierz(client, url):
        return wpisy, "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    tpt._licznik_triazu(monkeypatch, worth=False)
    tpt._licznik_analizy(monkeypatch)

    class BazaPoLimicie(tpt.AtrapaNewsow):
        """Dziennik mówi, że dzisiejszy budżet wywołań jest już wyczerpany."""

        def _wykonaj(self, sql, params):
            if sql.startswith("SELECT count(*) AS n FROM news.api_spend"):
                return {"n": news_worker.DZIENNY_LIMIT_WYWOLAN}
            return super()._wykonaj(sql, params)

    baza = BazaPoLimicie()
    s = asyncio.run(news_worker.run_cycle(baza, tpt.AtrapaDostawcy(), http=None,
                                          verbose=False))

    assert s["zapisane"] == 4, (
        f"zapisano {s['zapisane']} z 4 — wyczerpany budżet skasował materiał, "
        f"którego kanał RSS jutro nie odda")
    assert s["limit"] is not True or s["zapisane"] == 4


# ==================================== cisza potoku widoczna z linii logu

def test_cykl_bez_ANI_JEDNEJ_analizy_nie_moze_wygladac_jak_cykl_zdrowy(monkeypatch):
    """Twardy warunek: `koszt $0.0000, błędów 0` bez analiz ma być NIEMOŻLIWE.

    Linia z produkcji, powtarzana co pięć minut przez dwie i pół doby, gdy
    `news.article_analysis` nie dostawało ani jednego wiersza:

        [04:55:05] kanałów 4, wpisów 100, nowych 0, zapisanych 0, przez filtr 0,
        odsianych 0, przeanalizowanych 0, koszt $0.0000, błędów 0, 1 s
    """
    async def pusto(client, url):
        return [], "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pusto)
    tpt._licznik_triazu(monkeypatch, worth=False)
    tpt._licznik_analizy(monkeypatch)

    s = asyncio.run(news_worker.run_cycle(tpt.AtrapaNewsow(), tpt.AtrapaDostawcy(),
                                          http=None, verbose=False))

    assert s["przeanalizowane"] == 0 and s["koszt"] == 0.0
    assert s["cisza"], (
        "cykl bez ani jednej analizy nie powiedział o tym niczego — "
        "cisza potoku jest nie do odróżnienia od spokojnej doby")
    assert "BEZ ANALIZ" in s["cisza"] and "ostatnia" in s["cisza"]


def test_cykl_ktory_cos_przeanalizowal_NIE_krzyczy(monkeypatch):
    """Kanarek na drugą stronę: ostrzeżenie przy zdrowym cyklu nauczyłoby je ignorować."""
    wpis = tpt.Wpis("Fed podniósł stopy o 25 pb")

    async def pobierz(client, url):
        return [wpis], "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    tpt._licznik_triazu(monkeypatch, worth=True)
    tpt._licznik_analizy(monkeypatch, relevant=True)

    s = tpt.cykl(tpt.AtrapaNewsow())

    assert s["przeanalizowane"] == 1
    assert s["cisza"] == "", f"zdrowy cykl krzyczy: {s['cisza']!r}"


def test_opis_ciszy_nazywa_ODCIECIE_DOSTAWCY_gdy_to_ono_zatrzymalo_potok():
    s = dict(news_worker.puste_stats(), odciety="gemini: wyczerpany limit DOBOWY",
             czekaja=65, od_ostatniej_analizy_h=62.0)
    opis = news_worker.opis_ciszy(s, analizuj=True)
    assert "gemini: wyczerpany limit DOBOWY" in opis
    assert "62 h temu" in opis


def test_opis_ciszy_mowi_o_PORZUCONYCH_bo_o_nich_nie_mowilo_nic():
    s = dict(news_worker.puste_stats(), porzucone=106, od_ostatniej_analizy_h=None)
    opis = news_worker.opis_ciszy(s, analizuj=True)
    assert "106" in opis and "NIGDY" in opis


# ==================================== druga bramka: liczba wywołań

def test_bramka_liczy_WYWOLANIA_a_nie_tylko_dolary():
    """Kwotowa bramka nie zadziałała ani razu: 11.09 potok stanął przy $0,114
    z $5, zatrzymany progiem Gemini liczonym w ZAPYTANIACH."""
    b = news_worker.Budzet(0.0, news_worker.DZIENNY_LIMIT_WYWOLAN - 1)
    assert b.wyczerpany() == ""
    b.dolicz(0.0)                      # wywołanie darmowe — też się liczy
    assert "wywołań" in b.wyczerpany(), (
        "limit liczby wywołań nie wiąże, więc pętla na darmowym progu dostawcy "
        "znów przejdzie niezauważona")


def test_bramka_kwotowa_dalej_wiaze():
    b = news_worker.Budzet(news_worker.DAILY_LIMIT_USD, 0)
    assert "limit dobowy" in b.wyczerpany()


def test_obie_bramki_sa_sprawdzane_w_OBU_petlach_po_artykulach():
    """Bramka w jednej pętli, a nie w drugiej, to bramka, której nie ma."""
    zaleglosci = ZRODLO[ZRODLO.index("async def nadrob_zaleglosci"):
                        ZRODLO.index("async def run_cycle")]
    cykl = ZRODLO[ZRODLO.index("async def run_cycle"):ZRODLO.index("async def main")]
    for nazwa, fragment in (("nadrob_zaleglosci", zaleglosci), ("run_cycle", cykl)):
        assert "budzet.wyczerpany()" in fragment, f"{nazwa} pomija bramkę budżetu"
        assert "ai.czy_ma_czym_pytac()" in fragment, (
            f"{nazwa} nie sprawdza, czy dostawca żyje — kontrakt `llm.Dostawca` "
            f"wymaga tego OD WOŁAJĄCEGO")
