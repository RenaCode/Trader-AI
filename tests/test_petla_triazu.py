"""Artykuł oceniony RAZ ma nie wracać na ocenę — ani z RSS-a, ani z zaległości.

CO SIĘ DZIAŁO NA PRODUKCJI

Deduplikacja pyta o `content_hash`, ale wiersz w `news.article` powstawał
WYŁĄCZNIE po udanej analizie. Artykuł odrzucony przez triaż, artykuł, na którym
triaż padł, i artykuł uznany przez analizę za nieistotny nie zostawiały żadnego
śladu — więc w następnym cyklu wracały jako „nowe". Log poda `trader-news`
z 2026-09-10 pokazuje to w każdym z 338 cykli:

    [15:56:49] kanałów 5, wpisów 146, nowych 65, zapisanych 0, przez filtr 0, ...

Triaż odrzuca z założenia ~95% strumienia, więc praktycznie cały ruch był płatnie
oceniany co 5 minut — ~288 razy na dobę zamiast raz. Stąd dobowy limit Gemini
wypalony w kilkanaście minut i triaż schodzący zaraz potem na płatnego Claude'a.

DLACZEGO TESTY IDĄ PRZEZ `run_cycle`, A NIE PRZEZ CZYTANIE ŹRÓDŁA

Bo błąd nie był w żadnej pojedynczej instrukcji — każda z osobna była poprawna.
Był w KOLEJNOŚCI: zapis po decyzji zamiast przed nią. Widać go dopiero, gdy
puścić dwa cykle po sobie i policzyć, ile razy ten sam tytuł poszedł do modelu.
"""
from __future__ import annotations

import ast
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.ingest import news_worker

KORZEN = Path(__file__).resolve().parents[1]
MIGRACJA = (KORZEN / "db/migrations/0044_werdykt_triazu.sql").read_text(encoding="utf-8")
ZRODLO = (KORZEN / "services/ingest/news_worker.py").read_text(encoding="utf-8")


def _dopuszczalne(kolumna: str) -> set[str]:
    """Wartości z CHECK-a w migracji.

    Czytane Z MIGRACJI, nie przepisane tutaj — kopia zbioru w pliku testowym
    rozjechałaby się z bazą przy pierwszej zmianie i test utrwalałby stan
    sprzed niej.
    """
    m = re.search(rf"{kolumna} IS NULL OR {kolumna} IN \(([^)]*)\)", MIGRACJA)
    assert m, f"nie znaleziono CHECK dla kolumny {kolumna} w migracji 0044"
    return set(re.findall(r"'([^']+)'", m.group(1)))


WERDYKTY_TRIAZU = _dopuszczalne("triage_verdict")
WERDYKTY_ANALIZY = _dopuszczalne("analysis_verdict")


def _stale_tekstowe(wezel: ast.AST) -> set[str]:
    """Wartości, jakie wyrażenie może przyjąć — o ile da się to ustalić z kodu.

    Wyrażenia nierozstrzygalne statycznie oddają pusty zbiór. NIE ZGADUJEMY:
    fałszywy alarm w teście kończy się jego wyłączeniem.
    """
    if isinstance(wezel, ast.Constant) and isinstance(wezel.value, str):
        return {wezel.value}
    if isinstance(wezel, ast.IfExp):
        return _stale_tekstowe(wezel.body) | _stale_tekstowe(wezel.orelse)
    return set()


def _werdykty_z_kodu(funkcja: str) -> set[str]:
    """Werdykty przekazywane do `funkcja` w źródle workera (trzeci argument)."""
    uzyte: set[str] = set()
    for w in ast.walk(ast.parse(ZRODLO)):
        if isinstance(w, ast.Call) and getattr(w.func, "id", None) == funkcja:
            uzyte |= _stale_tekstowe(w.args[2])
    return uzyte


# ============================================================== atrapy

class Wpis:
    """Wpis RSS w takim kształcie, w jakim widzi go worker (feedparser)."""

    def __init__(self, tytul: str, zajawka: str = "zajawka"):
        self.title = tytul
        self.summary = zajawka
        self.link = "https://przyklad.test/" + tytul[:10]
        self.published_parsed = time.gmtime()


class AtrapaDostawcy:
    """Tyle dostawcy, ile dotyka `run_cycle` poza samym wywołaniem modelu.

    Koszt zerowy celowo: limit dobowy ma w tych testach nigdy nie być wiążący,
    bo mierzymy LICZBĘ OCEN, a nie budżet.
    """
    nazwa = "atrapa"
    model_triazu = "tani-model"
    model_analizy = "drogi-model"

    def koszt(self, model, usage):
        return 0.0

    def uzyty_model(self, model, usage):
        return model


class AtrapaNewsow:
    """Minimalna baza newsów: `news.article` z werdyktami i deduplikacją.

    ŚWIADOMIE NIE WYKONUJE `WHERE` — tak samo jak `tests/atrapa_bazy.py`. Dla
    zaległości oddaje KAŻDY artykuł bez analizy, czyli zachowuje się jak baza,
    której zapytanie niczego nie odsiewa. To jest mocniejszy test niż powtórzenie
    warunku z SQL-a: pilnuje, że pominięcie ocenionych artykułów jest decyzją
    KODU, a nie skutkiem ubocznym zapytania, którego atrapa i tak by nie wykonała.
    Że produkcyjny SQL dodatkowo nie ściąga odrzuconych z bazy, sprawdza osobny
    test niżej.

    Zna za to te ograniczenia z migracji 0044, na których zapis mógłby się
    wywrócić dopiero na produkcji: zbiory wartości werdyktów i to, że werdykt
    analizy nie może stać przy artykule nieprzepuszczonym przez triaż.
    """

    def __init__(self, artykuly: list[dict] | None = None):
        self.artykuly: list[dict] = list(artykuly or [])
        self.sql: list[tuple[str, object]] = []
        self._odpowiedz = None

    # ---- połączenie i kursor w jednym: test nie potrzebuje ich rozdzielać
    def cursor(self, row_factory=None):
        return self

    def commit(self):
        pass

    # ---- interfejs kursora
    def execute(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.sql.append((plaski, params))
        self._odpowiedz = self._wykonaj(plaski, params)

    def fetchone(self):
        w, self._odpowiedz = self._odpowiedz, None
        return (w[0] if w else None) if isinstance(w, list) else w

    def fetchall(self):
        w, self._odpowiedz = self._odpowiedz, None
        return w if isinstance(w, list) else ([] if w is None else [w])

    # ---- pomocnicze
    def po_tytule(self, tytul: str) -> dict:
        pasujace = [a for a in self.artykuly if a["title"] == tytul]
        assert pasujace, f"artykuł {tytul!r} nie trafił do bazy"
        return pasujace[0]

    def _po_id(self, article_id: int) -> dict:
        return next(a for a in self.artykuly if a["id"] == article_id)

    def _wykonaj(self, sql, params):
        if sql.startswith("SELECT COALESCE(sum(cost_usd)"):
            return {"s": 0.0}
        if sql.startswith("SELECT id, symbol FROM ref.instrument"):
            return []
        if sql.startswith("SELECT id, name, url, engine"):
            return [{"id": 1, "name": "Kanał testowy", "url": "https://przyklad.test/rss",
                     "engine": None, "poll_interval_s": 300, "last_polled_at": None}]
        if sql.startswith("SELECT 1 FROM news.article WHERE content_hash"):
            return {"?column?": 1} if any(a["content_hash"] == params[0]
                                          for a in self.artykuly) else None
        if sql.startswith("INSERT INTO news.article ("):
            return self._wstaw(params)
        if sql.startswith("UPDATE news.article SET triage_verdict"):
            werdykt, powod, model, przyrost, aid = params
            assert werdykt in WERDYKTY_TRIAZU, \
                f"{werdykt!r} łamie CHECK article_triage_verdict_chk"
            a = self._po_id(aid)
            a.update(triage_verdict=werdykt, triage_reason=powod, triage_model=model,
                     triage_at=datetime.now(timezone.utc),
                     triage_attempts=a["triage_attempts"] + przyrost)
            return None
        if sql.startswith("UPDATE news.article SET analysis_verdict"):
            werdykt, przyrost, aid = params
            assert werdykt in WERDYKTY_ANALIZY, \
                f"{werdykt!r} łamie CHECK article_analysis_verdict_chk"
            a = self._po_id(aid)
            assert a["triage_verdict"] == "accepted", (
                "CHECK article_analiza_po_triazu_chk: werdykt analizy przy artykule, "
                "którego triaż nie przepuścił")
            a.update(analysis_verdict=werdykt,
                     analysis_at=datetime.now(timezone.utc),
                     analysis_attempts=a["analysis_attempts"] + przyrost)
            return None
        if sql.startswith("INSERT INTO news.article_analysis"):
            self._po_id(params[0])["ma_analize"] = True
            return None
        if sql.startswith("SELECT a.id, a.title"):
            return self._zalegle()
        return None

    def _wstaw(self, params):
        feed_id, url, title, summary, h, event_time = params
        if any(a["content_hash"] == h for a in self.artykuly):
            return None                        # ON CONFLICT (content_hash) DO NOTHING
        self.artykuly.append(nowy_artykul(title, h=h, event_time=event_time,
                                          id=len(self.artykuly) + 1))
        return {"id": self.artykuly[-1]["id"]}

    def _zalegle(self):
        """Wszystko bez analizy, ale TYLKO z okna OKNO_ZALEGLOSCI_H.

        Okno jest jedynym warunkiem `WHERE`, który atrapa odtwarza, bo jest
        TWARDĄ GRANICĄ, a nie regułą pomijania: artykuł spoza doby nie zasili
        już żadnej świecy (patrz tests/test_zaleglosci_newsow.py) i produkcja
        nigdy go tu nie zobaczy. Filtr po werdykcie zostaje świadomie
        nieodtworzony — o nim ma decydować kod, nie atrapa.
        """
        granica = przed_chwila_minus(news_worker.OKNO_ZALEGLOSCI_H * 3600)
        return [dict(a, feed="Kanał testowy") for a in self.artykuly
                if not a["ma_analize"]
                and (a["event_time"] is None or a["event_time"] > granica)]


def nowy_artykul(title: str, **nadpisz) -> dict:
    """Wiersz `news.article` w stanie tuż po wstawieniu — czyli BEZ oceny."""
    return {"id": nadpisz.pop("id", 1), "title": title, "summary": "zajawka",
            "content_hash": nadpisz.pop("h", b"x"),
            "event_time": nadpisz.pop("event_time", None),
            "triage_verdict": None, "triage_reason": None, "triage_model": None,
            "triage_at": None, "triage_attempts": 0,
            "analysis_verdict": None, "analysis_at": None, "analysis_attempts": 0,
            "ma_analize": False, **nadpisz}


WYNIK_ANALIZY = {"relevant": True, "instruments": [], "direction": "bullish",
                 "magnitude": 0.5, "horizon": "hours", "confidence": 0.6,
                 "novelty": 0.7, "reasoning": "bo tak"}
ZUZYCIE = {"input": 100, "output": 20}


@pytest.fixture
def kanal(monkeypatch):
    """Kanał RSS oddający zawsze ten sam wpis — jak prawdziwy przez kilka godzin."""
    wpis = Wpis("Fed podniósł stopy o 25 pb")

    async def pobierz(client, url):
        return [wpis], "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    return wpis


def _licznik_triazu(monkeypatch, worth: bool = False, wyjatek: Exception | None = None):
    ocenione: list[str] = []

    async def triaz(ai, tytul, zajawka):
        ocenione.append(tytul)
        if wyjatek is not None:
            raise wyjatek
        return worth, "podsumowanie tygodnia", ZUZYCIE

    monkeypatch.setattr(news_worker, "triage", triaz)
    return ocenione


def _licznik_analizy(monkeypatch, relevant: bool = True):
    analizowane: list[str] = []

    async def analiza(ai, tytul, zajawka, source, published, symbols):
        analizowane.append(tytul)
        return dict(WYNIK_ANALIZY, relevant=relevant), ZUZYCIE

    monkeypatch.setattr(news_worker, "analyze", analiza)
    return analizowane


def przed_chwila_minus(sekund: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=sekund)


def puste_stats() -> dict:
    """Liczniki cyklu w kształcie, jakiego oczekuje `nadrob_zaleglosci`."""
    return {"koszt": 0.0, "bledy": 0, "opisy_bledow": [], "zaleglosci": 0,
            "odsiane": 0}


def cykl(baza, ai=None):
    return asyncio.run(news_worker.run_cycle(baza, ai or AtrapaDostawcy(),
                                             http=None, verbose=False))


# ====================================================== rdzeń: brak powtórki

def test_artykul_ODRZUCONY_przez_triaz_nie_wraca_na_triaz_w_kolejnym_cyklu(
        monkeypatch, kanal):
    """TEST, OD KTÓREGO ZACZĘŁA SIĘ TA POPRAWKA.

    Na kodzie sprzed niej ten sam tytuł idzie do modelu w każdym cyklu, bo
    odrzucenie nie zostawia wiersza, o który mogłaby zahaczyć deduplikacja.
    """
    ocenione = _licznik_triazu(monkeypatch, worth=False)
    _licznik_analizy(monkeypatch)
    baza = AtrapaNewsow()

    pierwszy = cykl(baza)
    drugi = cykl(baza)

    assert ocenione == ["Fed podniósł stopy o 25 pb"], (
        f"triaż wywołany {len(ocenione)} razy dla jednego artykułu — "
        f"odrzucenie nie zostawiło śladu w news.article")
    assert pierwszy["nowe"] == 1 and pierwszy["zapisane"] == 1
    assert drugi["nowe"] == 0, "ten sam artykuł wrócił jako nowy"
    assert baza.po_tytule("Fed podniósł stopy o 25 pb")["triage_verdict"] == "rejected"


def test_ZALEGLOSCI_nie_triazuja_ponownie_artykulu_z_werdyktem(monkeypatch):
    """PUŁAPKA DRUGIEGO RZĘDU.

    Sam zapis artykułu przed decyzją triażu nie wystarcza: odrzucone artykuły
    nie mają wiersza w `news.article_analysis`, więc wpadają wprost w zaległości
    i pętla wraca tylnymi drzwiami — wolniej, ale trwalej, bo baza nie zapomina
    tak jak kanał RSS.
    """
    baza = AtrapaNewsow([nowy_artykul("Poradnik: jak kupić bitcoina",
                                      triage_verdict="rejected",
                                      triage_reason="poradnik")])
    ocenione = _licznik_triazu(monkeypatch, worth=False)
    _licznik_analizy(monkeypatch)

    asyncio.run(news_worker.nadrob_zaleglosci(
        baza, baza, AtrapaDostawcy(), {}, [], 0.0, puste_stats(), verbose=False))

    assert ocenione == [], "artykuł z zapisanym werdyktem poszedł na triaż drugi raz"


def test_analiza_NIEISTOTNA_konczy_sprawe_artykulu(monkeypatch, kanal):
    """`relevant=false` nie zapisuje nic do `news.article_analysis`, więc bez
    własnego werdyktu artykuł wracałby w zaległościach i był analizowany DROGIM
    modelem w kółko — ta sama pętla, tylko o rząd wielkości droższa."""
    _licznik_triazu(monkeypatch, worth=True)
    analizowane = _licznik_analizy(monkeypatch, relevant=False)
    baza = AtrapaNewsow()

    cykl(baza)
    cykl(baza)

    assert analizowane == ["Fed podniósł stopy o 25 pb"], (
        f"analiza wywołana {len(analizowane)} razy dla jednego artykułu")
    assert baza.po_tytule("Fed podniósł stopy o 25 pb")["analysis_verdict"] == "irrelevant"


# ============================================== awaria dostawcy to nie werdykt

def test_AWARIA_triazu_zostawia_artykul_do_ponowienia(monkeypatch, kanal):
    """Awaria dostawcy nie mówi nic o tym, czy artykuł jest wart analizy —
    kilkuminutowa niedostępność nie może kasować całego okna newsów."""
    _licznik_triazu(monkeypatch, wyjatek=RuntimeError("503 u dostawcy"))
    _licznik_analizy(monkeypatch)
    baza = AtrapaNewsow()

    cykl(baza)

    artykul = baza.po_tytule("Fed podniósł stopy o 25 pb")
    assert artykul["triage_verdict"] == "failed"
    assert artykul["triage_attempts"] == 1, (
        "artykuł poszedł do modelu drugi raz w tym samym cyklu — nadrabianie "
        "zaległości trafiło w tę samą niedostępność dostawcy")
    # Ponowienie NALEŻY MU SIĘ, ale dopiero po przerwie.
    assert news_worker.etap_do_wykonania(artykul) is None
    assert news_worker.etap_do_wykonania(
        dict(artykul, triage_at=przed_chwila_minus(news_worker.PRZERWA_PO_AWARII_S + 1))
    ) == "triaz"


def test_awaria_ponawiana_JEST_OGRANICZONA():
    """Bez limitu błąd TRWAŁY — wyczerpany kredyt, zły klucz, wycofany model —
    powtarza się identycznie przy każdej próbie i każda z nich jest płatna.
    To ta sama pętla, tylko rozpędzająca się wolniej."""
    ile = news_worker.PROBY_PO_AWARII_MAX
    dawno = przed_chwila_minus(news_worker.PRZERWA_PO_AWARII_S + 1)
    ostatnia_szansa = nowy_artykul("x", triage_verdict="failed", triage_at=dawno,
                                   triage_attempts=ile - 1)
    wyczerpany = nowy_artykul("x", triage_verdict="failed", triage_at=dawno,
                              triage_attempts=ile)

    assert news_worker.etap_do_wykonania(ostatnia_szansa) == "triaz"
    assert news_worker.etap_do_wykonania(wyczerpany) is None
    assert news_worker.etap_do_wykonania(
        nowy_artykul("x", triage_verdict="accepted", analysis_verdict="failed",
                     analysis_at=dawno, analysis_attempts=ile)) is None


def test_ponowienie_CZEKA_zamiast_isc_w_te_sama_awarie():
    """Nadrabianie zaległości idzie na końcu TEGO SAMEGO cyklu. Bez przerwy limit
    prób wyczerpywałby się w kilkanaście sekund i minutowa niedostępność dostawcy
    kasowałaby artykuł na zawsze — czyli możliwość ponowienia nie znaczyłaby nic."""
    swiezo_padl = nowy_artykul("x", triage_verdict="failed", triage_attempts=1,
                               triage_at=przed_chwila_minus(5))
    assert news_worker.etap_do_wykonania(swiezo_padl) is None
    assert news_worker.PRZERWA_PO_AWARII_S >= 300, \
        "przerwa krótsza niż cykl produkcyjny nie odsuwa ponowienia o ani jeden cykl"


def test_etap_do_wykonania_rozroznia_wszystkie_stany():
    """Kanarek na zbiór stanów: gdyby doszedł nowy werdykt i nikt nie dopisał tu
    gałęzi, artykuł wpadłby w domyślną i albo utknął, albo krążył."""
    e = news_worker.etap_do_wykonania
    assert e(nowy_artykul("x")) == "triaz"                       # nieoceniony
    assert e(nowy_artykul("x", triage_verdict="rejected")) is None
    assert e(nowy_artykul("x", triage_verdict="accepted")) == "analiza"
    assert e(nowy_artykul("x", triage_verdict="accepted",
                          analysis_verdict="relevant")) is None
    assert e(nowy_artykul("x", triage_verdict="accepted",
                          analysis_verdict="irrelevant")) is None


def test_zalegly_artykul_po_triazu_idzie_PROSTO_do_analizy(monkeypatch):
    """Artykuł, którego triaż już przepuścił, nie płaci za triaż drugi raz —
    tańszy model nie powie o nim nic nowego."""
    baza = AtrapaNewsow([nowy_artykul("Bank centralny tnie stopy",
                                      triage_verdict="accepted",
                                      triage_reason="decyzja regulatora")])
    ocenione = _licznik_triazu(monkeypatch, worth=True)
    analizowane = _licznik_analizy(monkeypatch)

    asyncio.run(news_worker.nadrob_zaleglosci(
        baza, baza, AtrapaDostawcy(), {}, [], 0.0, puste_stats(), verbose=False))

    assert ocenione == []
    assert analizowane == ["Bank centralny tnie stopy"]
    assert baza.artykuly[0]["analysis_verdict"] == "relevant"


# ================================================== zapis przed każdą decyzją

def test_artykul_ZA_STARY_na_analize_i_tak_jest_zapisywany(monkeypatch):
    """Ta sama pętla dla innego powodu: artykuł starszy niż doba wypadał z cyklu
    przed zapisem, więc kanał oddawał go jako „nowy" bez końca. Zapis jest
    darmowy, decyzja płatna — zapis idzie pierwszy."""
    stary = Wpis("Archiwalny wpis z zeszłego miesiąca")
    stary.published_parsed = time.gmtime(time.time() - 30 * 86400)

    async def pobierz(client, url):
        return [stary], "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    ocenione = _licznik_triazu(monkeypatch, worth=False)
    baza = AtrapaNewsow()

    cykl(baza)
    drugi = cykl(baza)

    assert ocenione == [], "za stary artykuł nie ma prawa kosztować triażu"
    assert drugi["nowe"] == 0, "za stary artykuł wrócił jako nowy"
    assert baza.po_tytule("Archiwalny wpis z zeszłego miesiąca")["triage_verdict"] is None


def test_tryb_bez_analizy_zapisuje_bez_werdyktu(monkeypatch, kanal):
    """Brak klucza to nie ocena. Artykuł ma zostać zapisany z werdyktem NIEZNANYM,
    żeby `nadrob_zaleglosci` mogło go wziąć, gdy klucz się pojawi."""
    baza = AtrapaNewsow()

    s = asyncio.run(news_worker.run_cycle(baza, None, http=None, verbose=False,
                                          analizuj=False))

    assert s["zapisane"] == 1
    assert baza.po_tytule("Fed podniósł stopy o 25 pb")["triage_verdict"] is None


# ================================================ zgodność kodu, SQL-a i bazy

def test_zapytanie_o_zaleglosci_ODSIEWA_ODRZUCONE_juz_w_bazie():
    """Filtr w Pythonie chroni przed pętlą, ale nie przed czytaniem dziesiątek
    tysięcy wierszy co pięć minut. Odrzucone to ~95% tabeli i mają odpaść po
    stronie bazy — na to jest indeks częściowy z migracji 0044."""
    zaleglosci = ZRODLO[ZRODLO.index("async def nadrob_zaleglosci"):
                        ZRODLO.index("async def run_cycle")]
    assert "a.triage_verdict IS NULL" in zaleglosci
    assert "a.triage_verdict IN ('accepted', 'failed')" in zaleglosci
    assert "article_do_oceny_idx" in MIGRACJA


def test_werdykty_uzywane_w_kodzie_MIESZCZA_SIE_w_CHECK_ach_bazy():
    """Wartość spoza CHECK-a wywraca całą transakcję cyklu — a triaż jest wtedy
    już opłacony. Ten sam kształt błędu co w tests/test_prompt_i_schemat.py."""
    uzyte_triaz = _werdykty_z_kodu("zapisz_werdykt_triazu")
    uzyte_analiza = _werdykty_z_kodu("zapisz_werdykt_analizy")
    assert uzyte_triaz, "nie znaleziono ani jednego zapisu werdyktu triażu"
    assert uzyte_analiza, "nie znaleziono ani jednego zapisu werdyktu analizy"
    assert uzyte_triaz <= WERDYKTY_TRIAZU, \
        f"kod zapisuje {uzyte_triaz - WERDYKTY_TRIAZU}, czego CHECK nie dopuszcza"
    assert uzyte_analiza <= WERDYKTY_ANALIZY, \
        f"kod zapisuje {uzyte_analiza - WERDYKTY_ANALIZY}, czego CHECK nie dopuszcza"


def test_migracja_NIE_ORZEKA_o_istniejacych_artykulach():
    """Produkcja ma dziesiątki tysięcy artykułów sprzed 0044. Wpisanie im
    werdyktu backfillem znaczyłoby przypisanie oceny, której żaden model nie
    wydał — i albo skasowanie ich na zawsze, albo nic."""
    assert not re.search(r"^\s*UPDATE\s+news\.article\b", MIGRACJA, re.M | re.I), \
        "migracja przepisuje istniejące artykuły — werdykt ma pozostać NIEZNANY"
    assert "DEFAULT 0" in MIGRACJA, "liczniki prób muszą mieć wartość dla starych wierszy"
