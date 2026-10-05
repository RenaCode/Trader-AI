"""Kolizja na adresie ma pominąć JEDEN wiersz, a nie ubić cały cykl.

DLACZEGO TEN PLIK ISTNIEJE MIMO ŻE `news.article.url` NIE MA DZIŚ `UNIQUE`

Bo o jedno ograniczenie prosiłem w pierwszej wersji raportu z naprawy, a zapis
artykułu leży w miejscu, które takiej pomyłki nie wybacza:

    `cur.execute` z `INSERT INTO news.article` NIE LEŻY W ŻADNYM `try`
    wewnątrz `run_cycle`.

Sprawdzone drzewem składniowym, nie na oko — pilnuje tego
`test_ZAPIS_NADAL_NIE_LEZY_W_ZADNYM_TRY` niżej. `IntegrityError` wychodzi więc
z obu pętli (`for feed` i `for entry`), wywraca cykl i wprowadza transakcję
w stan `aborted`. Przepadają wtedy wszystkie pozostałe artykuły tej partii
i WSZYSTKIE KOLEJNE KANAŁY tego przebiegu — a RSS ich jutro nie odda. To jest ta
sama kategoria straty co limit dobowy osiągnięty o 14:00, tylko wywołana
duplikatem: awaria GORSZA od tej, przed którą deduplikacja broni.

Stan `news.article` na dziś (odczytany z `db/migrations/0003_news.sql`, nie
z pamięci — patrz `test_STAN_OGRANICZEN_TABELI_JEST_TAKI_JAK_ZAKLADAMY`):

    news.feed              linie  3-16   UNIQUE: url text NOT NULL UNIQUE
    news.article           linie 18-31   UNIQUE: UNIQUE (content_hash)

`UNIQUE` w linii 6 należy do `news.feed`, nie do `news.article` — to dwie różne
tabele, obie z kolumną `url`, i pomylenie ich jest tutaj łatwe.

Testy niżej NIE ZAKŁADAJĄ żadnej z tych dwóch wersji. Atrapa bazy egzekwuje
`UNIQUE (url)` tak, JAKBY migracja już istniała, i odtwarza przy tym prawdziwą
semantykę `ON CONFLICT` Postgresa. Dzięki temu odpowiadają na pytanie ważniejsze
niż „czy ograniczenie jest": **czy cykl przeżyje, jeśli kiedykolwiek będzie.**
"""
from __future__ import annotations

import ast
import asyncio
import re
import time
from pathlib import Path

import pytest

from services.ingest import news_worker
from services.ingest.llm import Dostawca

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO = (KORZEN / "services/ingest/news_worker.py").read_text(encoding="utf-8")
MIGRACJA = (KORZEN / "db/migrations/0003_news.sql").read_text(encoding="utf-8")


class NaruszenieUnikalnosci(Exception):
    """To, czym u psycopg jest `UniqueViolation` — bez zależności od sterownika.

    Prawdziwy wyjątek psycopg niósłby nazwę ograniczenia i `sqlstate`; tutaj
    liczy się WYŁĄCZNIE to, że jest to wyjątek, którego `run_cycle` nie łapie.
    Użycie klasy z psycopg niczego by nie wzmocniło, a związałoby test ze
    sterownikiem, którego ta ścieżka nie dotyka.
    """


class Wpis:
    """Wpis RSS w kształcie, w jakim widzi go worker (feedparser)."""

    def __init__(self, tytul: str, link: str, zajawka: str = "zajawka"):
        self.title = tytul
        self.summary = zajawka
        self.link = link
        self.published_parsed = time.gmtime()


class AtrapaDostawcy(Dostawca):
    """Dostawca, którego w tych testach nikt nie pyta — analiza jest wyłączona."""

    nazwa = "atrapa"
    model_triazu = "tani-model"
    model_analizy = "drogi-model"

    def koszt(self, model, usage):
        return 0.0

    def uzyty_model(self, model, usage):
        return model


class BazaZUnikalnymAdresem:
    """`news.article` z `UNIQUE (content_hash)` **i** `UNIQUE (url)`.

    DRUGIE OGRANICZENIE JEST HIPOTETYCZNE — dziś go w migracji nie ma. Atrapa
    dokłada je celowo, żeby odpowiedzieć na pytanie „co się stanie, gdy ktoś je
    doda", bez czekania na migrację i bez zgadywania.

    ODTWARZA PRAWDZIWĄ SEMANTYKĘ `ON CONFLICT` POSTGRESA, i to jest cała wartość
    tej atrapy:

      * `ON CONFLICT (kolumna) DO NOTHING` połyka konflikt **tylko na tej
        kolumnie**; naruszenie każdego innego ograniczenia leci jako wyjątek,
      * `ON CONFLICT DO NOTHING` bez wskazania kolumny połyka **każde**
        naruszenie unikalności.

    Atrapa, która połykałaby wszystko niezależnie od treści zapytania, czyniłaby
    tę różnicę niewykrywalną z definicji — a to właśnie o nią tu chodzi.
    """

    # Zapytania, które ta atrapa rozumie. Każde inne kończy się `AssertionError`,
    # a nie cichym `None`: kod pytający o coś, czego atrapa nie zna, ma się
    # zatrzymać tutaj, a nie zachowywać jak przy pustym wyniku.
    def __init__(self):
        self.artykuly: list[dict] = []
        self.sql: list[tuple[str, object]] = []
        self._odpowiedz = None

    def cursor(self, row_factory=None):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass

    def execute(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.sql.append((plaski, params))
        self._odpowiedz = self._wykonaj(plaski, params)

    def fetchone(self):
        w, self._odpowiedz = self._odpowiedz, None
        return w

    def fetchall(self):
        w, self._odpowiedz = self._odpowiedz, None
        return w if isinstance(w, list) else ([] if w is None else [w])

    # ------------------------------------------------------------------ zapytania
    def _wykonaj(self, sql, params):
        if sql.startswith("SELECT id, symbol, engine::text AS engine, base_ccy, quote_ccy"):
            return []
        if sql.startswith("SELECT id, name, url, engine"):
            return self.kanaly
        if sql.startswith("UPDATE news.feed SET last_polled_at"):
            return None
        if sql.startswith("SELECT COALESCE(sum(cost_usd)"):
            return {"s": 0.0}
        if sql.startswith("SELECT count(*) AS n FROM news.api_spend"):
            return {"n": 0}
        if sql.startswith("SELECT count(*) FILTER"):
            return {"czekaja": 0, "porzucone": 0}
        if sql.startswith("SELECT EXTRACT(EPOCH"):
            return {"h": None}
        if sql.startswith("SELECT 1 FROM news.article WHERE content_hash"):
            return {"?column?": 1} if any(a["h"] == params[0]
                                          for a in self.artykuly) else None
        if sql.startswith("SELECT 1 FROM news.article WHERE url"):
            return {"?column?": 1} if any(a["url"] == params[0]
                                          for a in self.artykuly) else None
        if sql.startswith("INSERT INTO news.article ("):
            return self._wstaw(sql, params)
        if sql.startswith("SELECT a.id, a.title"):
            return []
        raise AssertionError(f"atrapa nie zna zapytania: {sql[:120]}")

    @staticmethod
    def _polykane(sql: str) -> set[str] | None:
        """Które ograniczenia połyka `ON CONFLICT` tego zapytania.

        `None` znaczy „wszystkie" (klauzula bez wskazania kolumny).
        """
        m = re.search(r"ON CONFLICT\s*(\(([^)]*)\))?\s*DO NOTHING", sql)
        assert m, "zapis artykułu bez `ON CONFLICT DO NOTHING`"
        if m.group(2) is None:
            return None
        return {k.strip() for k in m.group(2).split(",")}

    def _wstaw(self, sql, params):
        feed_id, url, title, summary, h, event_time = params
        polykane = self._polykane(sql)

        for kolumna, wartosc, pole in (("content_hash", h, "h"), ("url", url, "url")):
            if wartosc == "" and kolumna == "url":
                # Pusty adres nie jest wartością unikalną w żadnej sensownej
                # migracji (indeks częściowy `WHERE url <> ''`). Gdyby był,
                # kanał bez `link` we wpisach oddawałby jeden artykuł w całym
                # swoim życiu — patrz test niżej.
                continue
            if not any(a[pole] == wartosc for a in self.artykuly):
                continue
            if polykane is None or kolumna in polykane:
                return None                      # ON CONFLICT ... DO NOTHING
            raise NaruszenieUnikalnosci(
                f'duplicate key value violates unique constraint '
                f'"article_{kolumna}_key"')

        self.artykuly.append({"id": len(self.artykuly) + 1, "url": url,
                              "title": title, "h": h})
        return {"id": self.artykuly[-1]["id"]}


def _cykl(baza, kanaly, wpisy_kanalu, monkeypatch):
    """Pełny `run_cycle` na podstawionych kanałach, BEZ analizy.

    `analizuj=False` nie osłabia testu: sprawdzamy ZAPIS, który w `run_cycle`
    stoi PRZED każdą decyzją i przed jakimkolwiek wywołaniem modelu. Włączona
    analiza dokładałaby wyłącznie atrapy dostawcy i budżetu.
    """
    baza.kanaly = kanaly

    async def pobierz(client, url):
        return wpisy_kanalu[url], "ok"

    monkeypatch.setattr(news_worker, "fetch_feed", pobierz)
    return asyncio.run(news_worker.run_cycle(
        baza, AtrapaDostawcy(), http=None, verbose=False, analizuj=False))


def _kanal(nr: int, nazwa: str) -> dict:
    return {"id": nr, "name": nazwa, "url": f"https://kanal{nr}.test/rss",
            "engine": None, "poll_interval_s": 300, "last_polled_at": None}


# ======================================================= stan faktyczny migracji

def _tabele_z_migracji() -> dict[str, tuple[int, int, list[str]]]:
    """Bloki `CREATE TABLE` z zakresami linii i ich ograniczeniami UNIQUE."""
    out = {}
    for m in re.finditer(r"CREATE TABLE\s+([\w.]+)\s*\(", MIGRACJA):
        gleb, i = 0, m.end() - 1
        while i < len(MIGRACJA):
            if MIGRACJA[i] == "(":
                gleb += 1
            elif MIGRACJA[i] == ")":
                gleb -= 1
                if gleb == 0:
                    break
            i += 1
        cialo = MIGRACJA[m.end():i]
        out[m.group(1)] = (MIGRACJA[:m.start()].count("\n") + 1,
                           MIGRACJA[:i].count("\n") + 1,
                           [l.strip() for l in cialo.splitlines()
                            if re.search(r"\bUNIQUE\b", l, re.I)])
    return out


def test_STAN_OGRANICZEN_TABELI_JEST_TAKI_JAK_ZAKLADAMY():
    """Zapisuje fakt, który łatwo pomylić — i pilnuje, żeby zmiana była widoczna.

    `news.feed` i `news.article` mają OBIE kolumnę `url`, a `UNIQUE` w linii 6
    należy do pierwszej z nich. Pomyłka w tę stronę prowadzi do wniosku, że
    kolizja adresu jest dziś niemożliwa do połknięcia — i odwrotnie.

    Gdy ktoś doda `UNIQUE` na `news.article.url`, ten test zapali się jako
    pierwszy i skieruje czytającego do reszty tego pliku, zamiast pozwolić
    odkryć zmianę przez wywrócony cykl na produkcji.
    """
    tabele = _tabele_z_migracji()
    od_feed, do_feed, unikalne_feed = tabele["news.feed"]
    _, _, unikalne_article = tabele["news.article"]

    assert od_feed <= 6 <= do_feed, (
        "linia 6 przestała należeć do `news.feed` — sprawdź, czyje jest UNIQUE")
    assert any("url" in u for u in unikalne_feed)
    assert unikalne_article == ["UNIQUE (content_hash)"], (
        f"ograniczenia `news.article` zmieniły się na {unikalne_article}. "
        f"Jeśli doszło UNIQUE na `url`, to reszta tego pliku dopiero teraz "
        f"opisuje produkcję, a nie hipotezę — przeczytaj ją ponownie.")


def test_ZAPIS_NADAL_NIE_LEZY_W_ZADNYM_TRY():
    """Przesłanka całego pliku. Gdyby zapis dostał własny `try` z `rollback`
    i `continue`, kolizja przestałaby grozić cyklem i ten plik można by
    uprościć — ale dopóki go nie ma, `ON CONFLICT` jest jedyną obroną."""
    drzewo = ast.parse(ZRODLO)
    for w in ast.walk(drzewo):
        if not (isinstance(w, ast.AsyncFunctionDef) and w.name == "run_cycle"):
            continue
        cel = [n.lineno for n in ast.walk(w)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and n.value.startswith("INSERT INTO news.article (")]
        assert cel, "nie znaleziono zapisu artykułu w `run_cycle`"
        obejmujace = [t for t in ast.walk(w) if isinstance(t, ast.Try)
                      and t.lineno <= cel[0] <= (t.end_lineno or 0)]
        assert not obejmujace, (
            "zapis artykułu dostał `try` — przeczytaj nagłówek tego pliku "
            "i rozstrzygnij, czy `ON CONFLICT` bez wskazania kolumny jest "
            "nadal właściwym rozwiązaniem")
        return
    pytest.fail("nie znaleziono `run_cycle`")


# ============================================= cykl przeżywa kolizję na adresie

def test_KOLIZJA_ADRESU_NIE_UBIJA_CYKLU_i_kolejne_kanaly_sa_przetworzone(monkeypatch):
    """Sedno wymagania.

    Dwa wpisy kolidujące na adresie (ten sam URL, inny tytuł — czyli inny
    `content_hash`, więc dedup po treści ich nie zrówna), a za nimi DRUGI KANAŁ.
    Cykl ma się skończyć normalnie, a kanał numer dwa ma zostać przetworzony.

    Przed poprawką `ON CONFLICT (content_hash)` nie obejmowało kolizji adresu,
    więc drugi wpis wyrzucał `UniqueViolation` z obu pętli. Ginął wtedy nie ten
    jeden wiersz, tylko cała reszta przebiegu.
    """
    baza = BazaZUnikalnymAdresem()
    kanaly = [_kanal(1, "Kanał kolidujący"), _kanal(2, "Kanał za nim")]
    adres = "https://przyklad.test/news/fed-podniosl-stopy"
    wpisy = {
        kanaly[0]["url"]: [
            Wpis("BREAKING: Fed podniósł stopy o 25 pb", adres),
            # Ten sam adres z parametrem śledzącym i innym tytułem: inny
            # `content_hash`, ten sam `url` po normalizacji.
            Wpis("Fed podniósł stopy o 25 punktów bazowych",
                 adres + "?utm_source=rss", zajawka="przeredagowany lead"),
        ],
        kanaly[1]["url"]: [Wpis("EBC zostawia stopy bez zmian",
                                "https://przyklad.test/news/ebc-bez-zmian")],
    }

    s = _cykl(baza, kanaly, wpisy, monkeypatch)

    assert s["kanaly"] == 2
    tytuly = [a["title"] for a in baza.artykuly]
    assert "EBC zostawia stopy bez zmian" in tytuly, (
        "kanał przetwarzany PO kolizji nie zapisał nic — kolizja adresu ubiła "
        "cały cykl, a RSS tych wpisów jutro nie odda")
    assert len(baza.artykuly) == 2, (
        f"zapisano {tytuly} — kolidujący wpis miał zostać pominięty jako jeden "
        f"wiersz, a nie przewrócić przebieg")


def test_POMINIECIE_PRZEZ_DEDUP_NIE_JEST_LICZONE_JAKO_KOLIZJA(monkeypatch):
    """„Pominięty wiersz" i „wiersz odrzucony przez ograniczenie" to dwa różne
    zdarzenia i nie mogą zlać się w jedno ciche `continue`.

    Tu dedup po URL zatrzymuje drugi wpis PRZED zapisem — to jest praca, nie
    awaria, i licznik kolizji ma zostać na zerze.
    """
    baza = BazaZUnikalnymAdresem()
    kanaly = [_kanal(1, "Kanał")]
    adres = "https://przyklad.test/news/fed-podniosl-stopy"
    wpisy = {kanaly[0]["url"]: [
        Wpis("BREAKING: Fed podniósł stopy o 25 pb", adres),
        Wpis("Fed podniósł stopy o 25 punktów bazowych",
             adres + "?utm_source=rss", zajawka="przeredagowany lead"),
    ]}

    s = _cykl(baza, kanaly, wpisy, monkeypatch)

    assert len(baza.artykuly) == 1
    assert s["kolizje_zapisu"] == 0, (
        "deduplikacja, która zadziałała zgodnie z zamysłem, nie jest kolizją "
        "zapisu — inaczej licznik krzyczałby przy każdej zdrowej dobie")
    assert not s["opisy_bledow"]


def test_ODRZUCENIE_PRZEZ_BAZE_MIMO_ZGODY_DEDUPU_JEST_GLOSNE(monkeypatch):
    """Druga strona tego samego rozróżnienia.

    Gdy oba pytania dedupu powiedziały „nowy", a baza i tak odrzuciła wiersz,
    znaczy to, że dedup po URL nie zadziałał w oknie — wyścig albo ograniczenie
    dołożone migracją, o której ten kod nie wie. Cicha strata artykułu wygląda
    wtedy jak spokojna doba.

    Odtwarzamy to wiersznem podłożonym POZA oknem 48 h: `SELECT ... AND
    ingested_at > now() - 48h` go nie widzi, a `UNIQUE (url)` owszem.
    """
    baza = BazaZUnikalnymAdresem()
    adres = news_worker.normalizuj_url("https://przyklad.test/news/stary-wpis")
    # Wiersz "sprzed okna": atrapa nie odtwarza `WHERE ingested_at`, więc
    # odejmujemy go ręcznie z odpowiedzi na pytanie dedupu po URL.
    baza.artykuly.append({"id": 99, "url": adres, "title": "stary", "h": b"inny"})
    baza._wykonaj_oryg = baza._wykonaj

    def _poza_oknem(sql, params):
        if sql.startswith("SELECT 1 FROM news.article WHERE url"):
            return None                     # poza oknem 48 h — dedup go nie widzi
        return baza._wykonaj_oryg(sql, params)

    baza._wykonaj = _poza_oknem

    kanaly = [_kanal(1, "Kanał")]
    wpisy = {kanaly[0]["url"]: [Wpis("Nowy tytuł tego samego adresu", adres)]}

    s = _cykl(baza, kanaly, wpisy, monkeypatch)

    assert s["kolizje_zapisu"] == 1, (
        "artykuł zniknął bez śladu — odrzucenie przez ograniczenie bazy mimo "
        "zgody dedupu musi być widoczne, bo znaczy, że dedup po URL nie zadziałał")
    assert any("ograniczenie bazy" in o for o in s["opisy_bledow"])


# ================================== adres kanału nie trafia już do `news.article`

def test_WPIS_BEZ_ADRESU_nie_dostaje_adresu_KANALU(monkeypatch):
    """Do 2026-09-23 wpis bez `link` zapisywał w `url` adres kanału RSS.

    Dwa takie wpisy w jednej partii dawały dwa wiersze o identycznym `url`.
    Dziś kończy się to powielonym adresem, a po dołożeniu `UNIQUE (url)` —
    wywróconym cyklem. Niezależnie od ograniczeń był to zapis NIEPRAWDZIWY:
    odnośnik prowadził do kanału zamiast do artykułu.
    """
    baza = BazaZUnikalnymAdresem()
    kanaly = [_kanal(1, "Kanał bez odnośników")]
    wpisy = {kanaly[0]["url"]: [Wpis("Pierwszy bez odnośnika", ""),
                                Wpis("Drugi bez odnośnika", "")]}

    s = _cykl(baza, kanaly, wpisy, monkeypatch)

    assert len(baza.artykuly) == 2, (
        "kanał bez `link` we wpisach przestał oddawać artykuły")
    assert s["kolizje_zapisu"] == 0
    for a in baza.artykuly:
        assert a["url"] != kanaly[0]["url"], (
            "adres kanału trafił do `news.article.url` — odnośnik w interfejsie "
            "prowadzi do RSS-a zamiast do artykułu, a przy UNIQUE na tej "
            "kolumnie drugi taki wpis wywraca cykl")
