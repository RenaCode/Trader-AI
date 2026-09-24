"""Dziennik jakosciowy i atrybucja newsow: pomiar zostaje nasz, wniosek jest hipoteza.

PIEC WAD, KTORE TE TESTY PILNUJA:

  1. MODEL LICZACY ZAMIAST INTERPRETUJACEGO. "trafność 58%" wygenerowane
     z rozpędu wygląda identycznie jak trafność policzona — i trafia do tabeli,
     która z założenia jest pamięcią projektu. Wszystkie liczby muszą pochodzić
     z SQL-a w `postmortem.py`, a prompt musi tego zabraniać wprost.

  2. WNIOSEK BEZ ETYKIETY. Zdanie modelu zapisane obok pomiaru staje się
     po tygodniu nieodróżnialne od ustalenia. Każdy wniosek niesie
     `rodzaj: "hipoteza"` i `oparte_na` z identyfikatorami pozycji.

  3. ZERWANY KONTRAKT Z PANELEM. `Learning.jsx` renderuje `findings` przez
     `Array.isArray(...) && map(f => f.finding / f.action)`. Obiekt zamiast
     tablicy albo inne nazwy pól dają PUSTĄ sekcję bez jednego błędu — analiza
     jest w bazie i nie widzi jej nikt.

  4. DRUGA KOPIA REGUŁY "KTÓRE ARTYKUŁY SIĘ LICZĄ". Cecha `news_sent`
     i atrybucja decyzji muszą pytać TYM SAMYM zapytaniem. Rozjazd nie objawia
     się awarią: dziennik opisywałby inny zbiór artykułów niż ten, który
     wytworzył liczbę, na której zapadła decyzja.

  5. ROLA Z NADMIAREM UPRAWNIEŃ. Proces wysyłający historię handlu do obcego
     API nie może widzieć zaszyfrowanych kluczy giełdy — to jest dokładnie ten
     układ, który migracja 0052 rozmontowała dla skanera wiadomości.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from services.engine.postmortem import (POZYCJI_MAX, SCHEMA, SYSTEM,
                                        opis_pomiaru, probka_do_promptu,
                                        statystyki, zbuduj_prompt)

KORZEN = Path(__file__).resolve().parents[1]
CHART = KORZEN / "charts/trader/values.yaml"
MIGRACJA = KORZEN / "db/migrations/0054_rola_postmortemu.sql"


def _poz(pid, pnl, godzin=5.0, fees=0.01):
    return {"id": pid, "symbol": "X", "side": "long", "qty": 1, "avg_price": 100,
            "realized_pnl": pnl, "fees_paid": fees, "opened_at": None,
            "closed_at": None, "godzin": godzin, "regime": "trend_up",
            "confidence": 0.6, "rationale": "bo tak", "sygnaly": None, "newsy": None}


# ------------------------------------------------------------------- pomiar
def test_zero_nie_jest_wygrana():
    """Pozycja zamknięta na wejściu oddała prowizję i nie zarobiła nic."""
    s = statystyki([_poz(1, 1.0), _poz(2, 0.0), _poz(3, -1.0)])
    assert s["wygranych"] == 1
    assert s["trafnosc"] == round(1 / 3, 4)


def test_wynik_brutto_pokazuje_ile_zjadly_prowizje():
    """Liczba, która w tym projekcie rozstrzyga wszystko (docs/08, brama 0).

    Bez niej „wynik −0,02" nie mówi, czy strategia przegrała z rynkiem, czy
    z własnym kosztem — a to są dwa różne wnioski i dwie różne reakcje.
    """
    s = statystyki([_poz(1, -0.01, fees=0.05), _poz(2, -0.01, fees=0.05)])
    assert s["wynik_netto"] == -0.02
    assert s["prowizje"] == 0.1
    assert s["wynik_brutto"] == 0.08


def test_mediana_trzymania_a_nie_srednia():
    s = statystyki([_poz(1, 1.0, godzin=1), _poz(2, 1.0, godzin=2),
                    _poz(3, 1.0, godzin=100)])
    assert s["godzin_mediana"] == 2.0


def test_opis_pomiaru_niesie_liczby_a_nie_przymiotniki():
    s = statystyki([_poz(1, 1.0), _poz(2, -1.0)])
    opis = opis_pomiaru(s)
    assert opis.startswith("POMIAR:")
    for fragment in ("2 zamkniętych", "50.0%", "(1/2)"):
        assert fragment in opis, f"brak {fragment!r} w: {opis}"


# --------------------------------------------------------------- próbka
def test_przy_nadmiarze_pokazujemy_SKRAJNE_a_nie_pierwsze_z_brzegu():
    """WADA: `LIMIT n` po czasie pokazałby modelowi początek okresu.

    Błąd mieszka w ogonach rozkładu. Obcięcie chronologiczne dałoby próbkę,
    z której model opisze co innego niż okres, a statystyki obok będą dotyczyć
    całości — i nic tej rozbieżności nie zgłosi.
    """
    pozycje = [_poz(i, float(i)) for i in range(POZYCJI_MAX * 2)]
    probka = probka_do_promptu(pozycje)
    assert len(probka) == POZYCJI_MAX
    wyniki = [p["realized_pnl"] for p in probka]
    assert min(wyniki) == 0.0 and max(wyniki) == float(POZYCJI_MAX * 2 - 1)
    # Srodek rozkladu NIE wchodzi - to jest cala tresc tego wyboru.
    assert float(POZYCJI_MAX) not in wyniki


def test_maly_okres_idzie_w_calosci():
    pozycje = [_poz(i, float(i)) for i in range(5)]
    assert probka_do_promptu(pozycje) == pozycje


def test_prompt_mowi_wprost_ze_probka_nie_jest_calym_okresem():
    """Model, który nie wie o obcięciu, weźmie próbkę za populację."""
    pozycje = [_poz(i, float(i)) for i in range(POZYCJI_MAX * 2)]
    probka = probka_do_promptu(pozycje)
    import datetime as dt
    t = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)
    p = zbuduj_prompt("crypto", t, t, statystyki(pozycje), pozycje, probka)
    assert "To NIE jest cały okres" in p
    assert f"pokazano {POZYCJI_MAX} z {len(pozycje)}" in p


# ------------------------------------------------- granica pomiar / hipoteza
def test_prompt_zabrania_modelowi_liczenia():
    """WADA 1. Zakaz musi być w systemie, a liczby muszą być podane gotowe."""
    assert "NIE LICZ NICZEGO" in SYSTEM
    import datetime as dt
    t = dt.datetime(2026, 9, 24, tzinfo=dt.timezone.utc)
    pozycje = [_poz(1, 1.0), _poz(2, -1.0)]
    p = zbuduj_prompt("crypto", t, t, statystyki(pozycje), pozycje, pozycje)
    assert "policzone, nie licz ich ponownie" in p
    assert "trafność: 0.5" in p


def test_schema_wymaga_oparcia_wniosku_na_pozycjach():
    """WADA 2. Hipoteza, której nie da się odnieść do transakcji, jest opowieścią."""
    f = SCHEMA["properties"]["findings"]["items"]
    assert set(f["required"]) == {"finding", "action", "oparte_na"}
    assert f["properties"]["oparte_na"]["items"]["type"] == "integer"


def test_pusta_lista_wnioskow_jest_dozwolona():
    """Model zmuszony do wniosku wymyśli go. `findings` nie jest w `required`
    z zawartością — a system mówi wprost, że pusta lista jest poprawna."""
    assert "Pusta lista wniosków jest poprawną odpowiedzią" in SYSTEM


def test_kontrakt_z_panelem_uczenia():
    """WADA 3. `Learning.jsx` czyta `findings` jako TABLICĘ obiektów
    `{finding, action}`. Każdy inny kształt daje pustą sekcję bez błędu."""
    zrodlo = (KORZEN / "services/engine/postmortem.py").read_text()
    blok = zrodlo.split("findings = [")[1].split("]")[0]
    for pole in ('"finding"', '"action"', '"rodzaj": "hipoteza"', '"oparte_na"'):
        assert pole in blok, f"brak {pole} w budowanym wniosku"

    jsx = (KORZEN / "web/src/components/Learning.jsx").read_text()
    assert "Array.isArray(p.findings)" in jsx, (
        "panel zmienił sposób czytania `findings` — ten test pilnuje kontraktu, "
        "który tam jest, a nie tego, który był")
    assert "f.finding" in jsx and "f.action" in jsx


def test_summary_zaczyna_sie_od_pomiaru_a_nie_od_narracji():
    """Kolejność jest tu treścią: najpierw zmierzone, potem zinterpretowane."""
    zrodlo = (KORZEN / "services/engine/postmortem.py").read_text()
    assert 'summary = f"{opis_pomiaru(s)}\\n\\n{dane.get(\'summary\'' in zrodlo


# ------------------------------------------------- jedno źródło reguły newsów
def test_cecha_i_atrybucja_pytaja_TYM_SAMYM_zapytaniem():
    """WADA 4. Dwie kopie rozjadą się przy pierwszej zmianie okna."""
    zrodlo = (KORZEN / "services/ingest/features.py").read_text()
    assert zrodlo.count("SQL_WKLAD_NEWSOW") >= 3, "stała musi być używana, nie tylko istnieć"
    # `compute_news` NIE MA prawa mieć własnej kopii podzapytania.
    ciało = zrodlo.split("def compute_news")[1].split("def compute_cross_section")[0]
    assert "SQL_WKLAD_NEWSOW.format" in ciało
    assert "FROM news.article_instrument" not in ciało, (
        "podzapytanie zostało przepisane do `compute_news` — od tej chwili "
        "atrybucja decyzji może opisywać inny zbiór artykułów niż cecha")


def test_atrybucja_liczy_sie_dla_SWIECY_a_nie_chwili_decyzji():
    """Cechy do decyzji biorą się z `event_time == bar_start` (patrz
    `cechy_i_obce`). Atrybucja liczona z `as_of` opisywałaby okno przesunięte
    względem tego, co model widział."""
    zrodlo = (KORZEN / "services/engine/loop.py").read_text()
    blok = zrodlo.split('rows.append((did, "news"')[0].split("if bar_start is not None:")[1]
    assert "wklady_newsow(cur, instrument_id, bar_start)" in blok


def test_artykul_nie_podwaja_sie_w_sumie_wkladow():
    """`weight` i `contribution` zerem — artykuł jest WEJŚCIEM do cechy,
    a nie osobnym składnikiem wyniku."""
    zrodlo = (KORZEN / "services/engine/loop.py").read_text()
    wiersz = zrodlo.split('rows.append((did, "news"')[1].split("))")[0]
    assert wiersz.rstrip().endswith("0.0, 0.0")


# ----------------------------------------------------- rola i zadanie cykliczne
def test_zadanie_nie_chodzi_rola_silnika():
    """WADA 5. Rola silnika dałaby temu Jobowi klucze giełdy."""
    szablon = (KORZEN / "charts/trader/templates/postmortem-cron.yaml").read_text()
    assert '"rola" "postmortem"' in szablon
    assert '"rola" "silnik"' not in szablon


def test_rola_istnieje_w_charcie_i_w_migratorze():
    """Literówka w nazwie ma zatrzymać `helm template`, a brak wpisu
    w migratorze zostawiłby rolę bez hasła — czyli Job, który się nie zaloguje."""
    role = yaml.safe_load(CHART.read_text())["db"]["role"]
    assert role["postmortem"]["user"] == "trader_postmortem"
    zmienna = role["postmortem"]["secretKey"]
    assert zmienna == "POSTMORTEM_DB_PASSWORD"
    migrator = (KORZEN / "db/migrate.py").read_text()
    assert f'"trader_postmortem": "{zmienna}"' in migrator


def _bez_komentarzy(tekst: str) -> str:
    """Sam SQL. Uzasadnienie w komentarzach WYMIENIA tabele, których migracja
    celowo nie nadaje — szukanie nazwy w całym pliku trafiałoby w wyjaśnienie,
    dlaczego czegoś nie ma, i meldowało to jako obecność."""
    return "\n".join(l.split("--")[0] for l in tekst.splitlines())


def test_rola_nie_dostaje_kluczy_gieldy():
    """Nadanie, którego ta migracja NIE MA PRAWA zawierać."""
    sql = _bez_komentarzy(MIGRACJA.read_text())
    for zakazane in ("app.api_credential", "ON app.secret ", "market.", "feat.",
                     "risk.", "exec.order", "exec.fill"):
        assert zakazane not in sql, (
            f"migracja nadaje `trader_postmortem` prawo do {zakazane} — proces "
            f"wysyłający historię handlu do obcego API nie ma tego widzieć")
    # A to, czego potrzebuje, ma być nadane wprost.
    for wymagane in ("app.secret_llm", "exec.position", "journal.decision",
                     "learn.postmortem", "news.api_spend"):
        assert f"ON {wymagane}" in sql or f"ON {wymagane}," in sql, (
            f"migracja nie nadaje prawa do {wymagane} — proces nie zbuduje dziennika")


def test_oba_silniki_maja_dziennik():
    """`enabled` NIE jest tu sprawdzane, i to jest celowe.

    To przelacznik WDROZENIA, nie wlasnosc konfiguracji: zadanie startuje
    dopiero, gdy obraz zawiera `services/engine/postmortem.py`. Test pilnujacy
    `enabled is True` kazalby wlaczyc je przed zbudowaniem obrazu, czyli
    wymuszalby awarie, przed ktora chroni komentarz w charcie.

    Lista silnikow to co innego - opisuje, CO ma byc oceniane, i rozjazd tutaj
    znaczylby silnik bez dziennika, o ktorym nikt sie nie dowie.
    """
    v = yaml.safe_load(CHART.read_text())["postmortemCron"]
    assert v["silniki"] == ["crypto", "equity"]


def test_cron_ma_strefe_jawnie():
    """Bez `timeZone` k3s liczy harmonogram w strefie kontrolera, a dziennik
    wydatków liczy dobę w UTC — przesunięcie myli przy zestawianiu chwil."""
    szablon = (KORZEN / "charts/trader/templates/postmortem-cron.yaml").read_text()
    assert re.search(r'timeZone:\s*"Etc/UTC"', szablon)


def test_brak_hasla_nowej_roli_nie_klodzie_poda_api():
    """Dodanie roli do chartu nie ma prawa zatrzymać dashboardu.

    `trader.rolaHaslaEnv` jest wstrzykiwany do kontenera STARTOWEGO poda API
    (api.yaml). `secretKeyRef` bez `optional` przy kluczu nieobecnym w sekrecie
    daje `CreateContainerConfigError` — czyli pod API nie wstaje, a wraz z nim
    znika JEDYNE miejsce, w którym widać stan systemu. Powód jest przy tym
    zupełnie niezwiązany z tym, co API robi.

    Z `optional` czerwony robi się ten Job, który tej roli używa — awaria trafia
    tam, gdzie jej przyczyna. Rola bez hasła nadal się nie zaloguje, więc
    gwarancja z migracji 0034 zostaje nietknięta.
    """
    helpers = (KORZEN / "charts/trader/templates/_helpers.tpl").read_text()
    blok = helpers.split('define "trader.rolaHaslaEnv"')[1].split("{{- end -}}")[0]
    assert "optional: true" in blok, (
        "hasła ról nie są opcjonalne — dodanie nowej roli przed wpisaniem jej "
        "klucza do sekretu zatrzyma pod API")


def test_sekret_nowej_roli_jest_wymieniony_w_dokumentacji_wdrozenia():
    """Rola bez hasła to Job, który się nie loguje. Instrukcja musi to mówić
    w pliku, który czyta się PRZED wdrożeniem, a nie po awarii."""
    sql = MIGRACJA.read_text()
    assert "POSTMORTEM_DB_PASSWORD" in sql
    assert "PRZED migracja" in sql
