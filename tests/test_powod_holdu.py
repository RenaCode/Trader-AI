"""Dziennik ma odpowiadać na pytanie „dlaczego nic nie zrobiliśmy".

TŁO. Na 818 decyzji produkcyjnych 782 to `hold` i o żadnym z nich baza nie mówiła
DLACZEGO. Najgorszy przypadek: uzasadnienie „Wynik złożony -0,644 przy pewności
70%" przy progu wejścia 0,278 — wynik dwa i pół raza ponad progiem,
`risk_verdict='allow'`, `risk_reason` puste. Prawdziwym powodem była blokada
krótkiej pozycji na instrumencie spotowym, ale ta gałąź podmienia akcję na `hold`
PRZED gałęzią ryzyka, więc do dziennika nie trafiał po niej ani jeden bit.

Klasyfikacja tamtych 782 holdów przez odtworzenie wyniku i progu:

    665  słaby wynik
     60  trzyma pozycję
     57  short niewykonalny   <- najczęstsza realna przyczyna blokowania wejść
      0  za niska pewność     <- ten warunek nigdy nie był wiążący

TE TESTY CZYTAJĄ DRZEWO SKŁADNIOWE, A NIE TEKST ŹRÓDŁA. Sprawdzanie obecności
napisu przez `in zrodlo` daje się zaspokoić KOMENTARZEM — wystarczy, że ktoś
opisze gałąź słowami, i test zielenieje przy usuniętym kodzie. Zdarzyło się to
w tym projekcie przy trzech kanarkach jednego dnia, więc warunki poniżej stawiamy
na `ast`, które komentarzy nie widzi z definicji.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
ZRODLO_PETLI = KORZEN / "services/engine/loop.py"
MIGRACJA = KORZEN / "db/migrations/0040_powod_holdu.sql"

# Zbiór powodów jest WPISANY W SCHEMAT (CHECK założony w 0040, rozszerzony
# w 0067 o `rezim_bez_progu`). Trzymamy go tu raz, a testy niżej pilnują, żeby
# kod, migracja i słownik opisów mówiły to samo.
POWODY = {"slaby_wynik", "niska_pewnosc", "short_niewykonalny",
          "trzyma_pozycje", "wstrzymanie_ryzykiem", "rezim_bez_progu",
          # 0076: wejscie po ponad 20 min od zamkniecia swiecy (audyt F-4.4).
          "decyzja_przeterminowana"}


def _migracja_definiujaca_check() -> Path:
    """OSTATNIA migracja, która definiuje zbiór powodów - nie ta, która go
    wprowadziła.

    Wpisana na sztywno 0040 przestała opisywać bazę w chwili, w której 0067
    dołożyło `rezim_bez_progu`: test czytał wtedy zbiór SPRZED zmiany i zielenił
    się na rozjeździe z produkcją. Pytanie brzmi „co dopuszcza baza PO wszystkich
    migracjach", więc odpowiada na nie ostatni plik, który ten CHECK przepisuje.
    """
    pliki = [x for x in sorted((KORZEN / "db/migrations").glob("*.sql"))
             if "powod_holdu IN (" in x.read_text(encoding="utf-8")]
    assert pliki, "żadna migracja nie definiuje zbioru powodów holdu"
    return pliki[-1]


def _drzewo_petli() -> ast.Module:
    return ast.parse(ZRODLO_PETLI.read_text(encoding="utf-8"))


def _przypisania(blok: list[ast.stmt], nazwa: str) -> list[ast.Assign]:
    """Przypisania do `nazwa` bezpośrednio w tym bloku (bez zagnieżdżeń)."""
    return [w for w in blok
            if isinstance(w, ast.Assign)
            and any(isinstance(c, ast.Name) and c.id == nazwa for c in w.targets)]


def _bloki(drzewo: ast.AST):
    """Każda lista instrukcji w drzewie - ciało funkcji, gałąź `if`, `else`..."""
    for wezel in ast.walk(drzewo):
        for pole in ("body", "orelse", "finalbody"):
            blok = getattr(wezel, pole, None)
            if isinstance(blok, list) and blok and isinstance(blok[0], ast.stmt):
                yield blok


def test_kazda_galaz_konczaca_sie_holdem_ustawia_powod():
    """Gałąź, która robi hold i nie mówi dlaczego, jest tą samą luką od nowa.

    Nie sprawdzamy, ŻE powód gdzieś w pliku jest — sprawdzamy, że jest w TYM
    SAMYM bloku, co podmiana akcji. Powód ustawiony piętro wyżej opisywałby inną
    gałąź niż ta, która faktycznie zadziałała.
    """
    holdy = 0
    for blok in _bloki(_drzewo_petli()):
        for przypisanie in _przypisania(blok, "action"):
            wartosc = przypisanie.value
            if not (isinstance(wartosc, ast.Constant) and wartosc.value == "hold"):
                continue
            holdy += 1
            powody = _przypisania(blok, "powod_holdu")
            assert powody, (
                f"loop.py:{przypisanie.lineno} ustawia action='hold' i nie ustawia "
                f"powod_holdu w tym samym bloku - decyzja znowu nie mówi dlaczego")
            assert any(isinstance(p.value, ast.Constant) and p.value.value in POWODY
                       for p in powody), (
                f"loop.py:{przypisanie.lineno} ustawia powod_holdu na wartość spoza "
                f"zbioru dopuszczonego przez CHECK w migracji 0040")
    # Bez tego licznika test przechodzi także wtedy, gdy CAŁY blok wyboru akcji
    # zniknie z pliku - czyli nie sprawdza niczego.
    # 7 od 2026-10-03: `decyzja_przeterminowana` (0076).
    assert holdy == 7, (
        f"gałęzi kończących się holdem jest {holdy}, a nie 7 - jeśli to celowa "
        f"zmiana, popraw liczbę i sprawdź, czy CHECK w migracji (0040, ostatnio "
        f"przepisany w 0076) nadal pokrywa wszystkie powody")


def test_powody_w_kodzie_migracji_i_opisach_to_ten_sam_zbior():
    """Trzy miejsca, jedna lista. Rozjazd kończy się odrzuceniem INSERT-a przez bazę."""
    from services.engine.loop import OPIS_POWODU_HOLDU

    w_kodzie = set()
    for blok in _bloki(_drzewo_petli()):
        for p in _przypisania(blok, "powod_holdu"):
            if isinstance(p.value, ast.Constant) and isinstance(p.value.value, str):
                w_kodzie.add(p.value.value)

    ostatnia = _migracja_definiujaca_check()
    tresc = ostatnia.read_text(encoding="utf-8")
    w_check = set(re.findall(r"'([a-z_]+)'", tresc.split("powod_holdu IN (", 1)[1]
                             .split(")", 1)[0]))

    assert w_kodzie == POWODY, f"pętla ustawia inny zbiór powodów: {w_kodzie}"
    assert w_check == POWODY, f"CHECK w migracji dopuszcza inny zbiór: {w_check}"
    assert set(OPIS_POWODU_HOLDU) == POWODY, "słownik opisów rozjechał się z kodem"


def test_zapis_decyzji_niesie_powod_i_oba_progi():
    """Kolumna w schemacie bez wartości w INSERT-cie to kolumna pusta na zawsze.

    `journal.decision` jest niemutowalna — czego nie zapiszemy przy decyzji, tego
    już nigdy do niej nie dopiszemy.
    """
    from datetime import datetime

    from services.engine import loop

    zapisane: dict[str, object] = {}

    class Kursor:
        def execute(self, sql, params=None):
            if "INSERT INTO journal.decision " in sql:
                zapisane["sql"], zapisane["params"] = sql, params

        def fetchone(self):
            return {"id": 1}

        def executemany(self, sql, rows):
            pass

    werdykt = type("V", (), {"verdict": "allow", "reason": None})()
    loop.record_decision(
        Kursor(), "crypto", "demo", 7, datetime(2026, 9, 9), "hold", None, 100,
        0.5, "range", {"rsi": 50.0}, werdykt, "uzasadnienie", [], {}, 12, 130,
        0.8125, "short_niewykonalny", 0.2795, 0.1258)

    kolumny = zapisane["sql"].split("journal.decision (", 1)[1].split(")", 1)[0]
    for nazwa in ("powod_holdu", "prog_wejscia", "prog_wyjscia"):
        assert nazwa in kolumny, f"INSERT nie zapisuje kolumny {nazwa}"
    for wartosc in ("short_niewykonalny", 0.2795, 0.1258):
        assert wartosc in zapisane["params"], (
            f"{wartosc!r} jest w SQL, ale nie dolatuje do parametrów")
    # Liczba znaczników musi się zgadzać z liczbą parametrów, inaczej baza
    # odrzuci instrukcję dopiero na produkcji.
    assert zapisane["sql"].count("%s") == len(zapisane["params"])


def test_petla_podaje_progi_z_aktywnego_modelu():
    """Próg ma opisywać TĘ decyzję, a nie stan rejestru modeli w chwili czytania.

    `services/ingest/purge_demo.py` kasuje całą tabelę `learn.model_version`.
    Gdyby próg mieszkał wyłącznie tam, po sprzątaniu demo pytanie „czy ten wynik
    był ponad progiem" traciłoby odpowiedź dla wszystkich starych decyzji.
    """
    zrodlo = ZRODLO_PETLI.read_text(encoding="utf-8")
    wywolanie = zrodlo.split("did = record_decision(", 1)[1].split("\n\n", 1)[0]
    assert "powod_holdu" in wywolanie, "pętla nie przekazuje powodu do dziennika"
    # OD 2026-09-27 PROGI SA PER REZIM. Dziennik musi dostac prog TEGO rezimu,
    # bo zbiorcze `model["enter"]` nie jest liczba, z ktora porownano ten wynik -
    # zapisanie go opisywaloby decyzje wielkoscia, ktorej nie uzyto.
    assert "prog_wejscia_rezimu" in wywolanie and "prog_wyjscia_rezimu" in wywolanie, (
        "pętla nie przekazuje progów - dziennik zostanie zależny od learn.model_version")
    assert 'model["enter"]' not in wywolanie and 'model["exit"]' not in wywolanie, (
        "pętla zapisuje zbiorczy próg modelu zamiast progu reżimu, w którym "
        "zapadła decyzja")


def test_migracja_dodaje_kolumny_i_przywraca_wyzwalacz():
    """Backfill zdejmuje wyzwalacz niemutowalności — i MUSI go założyć z powrotem.

    Zostawiony zdjęty wyzwalacz nie objawia się niczym: tabela wygląda tak samo,
    tylko przestaje być append-only. Kolejny „poprawimy sobie jeden wiersz"
    przechodzi wtedy bez słowa i dataset traci gwarancję, dla której powstał.
    """
    tresc = MIGRACJA.read_text(encoding="utf-8")
    for kolumna in ("powod_holdu", "prog_wejscia", "prog_wyjscia"):
        assert f"ADD COLUMN IF NOT EXISTS {kolumna}" in tresc, f"brak kolumny {kolumna}"
    assert tresc.count("DISABLE TRIGGER decision_immutable") == 1
    assert tresc.count("ENABLE TRIGGER decision_immutable") == 1
    assert tresc.index("DISABLE TRIGGER") < tresc.index("ENABLE TRIGGER")
    # Backfill, który zostawia wiersze niewyjaśnione, wygląda tak samo jak
    # backfill, który zadziałał. Migracja liczy je sama i wywraca się na nich.
    assert "RAISE EXCEPTION" in tresc, "backfill nie sprawdza, czy coś pominął"


def test_atrapa_bazy_widzi_nowe_kolumny():
    """Schemat czytany z migracji musi znać kolumny, do których pisze kod."""
    from tests.atrapa_bazy import MIGRACJE

    tresc = "\n".join(p.read_text(encoding="utf-8") for p in MIGRACJE)
    assert "journal.decision" in tresc and "powod_holdu" in tresc
    assert MIGRACJA in MIGRACJE, "migracja 0040 nie jest widziana przez atrapę"


# ------------------------------------------------------------ wersja kodu

def test_sha_kodu_scina_przedrostek_tagu(monkeypatch):
    """Tag obrazu to `sha-12de83b`, a kolumna nazywa się git_sha, nie image_tag."""
    from services.engine import wersja

    monkeypatch.setenv("GIT_SHA", "sha-12de83b")
    assert wersja.sha_kodu() == "12de83b"
    monkeypatch.setenv("GIT_SHA", "12DE83B")
    assert wersja.sha_kodu() == "12de83b"


@pytest.mark.parametrize("tag", ["latest", "sha-", "main", "v1.2.3", "   "])
def test_sha_kodu_nie_udaje_odpowiedzi(monkeypatch, tag):
    """Tag, który nie jest sha, ma dać None — a nie napis udający commit.

    `_z_repozytorium` jest tu wyłączone, bo w checkout'cie zwróciłoby prawdziwy
    sha bieżącego HEAD-a i test przestałby sprawdzać cokolwiek.
    """
    from services.engine import wersja

    monkeypatch.setenv("GIT_SHA", tag)
    monkeypatch.setattr(wersja, "_z_repozytorium", lambda: None)
    assert wersja.sha_kodu() is None


@pytest.mark.parametrize("tag", ["beef", "ada", "decade", "face", "dad"])
def test_sha_kodu_odrzuca_nazwy_z_SAMYCH_LITER_SZESNASTKOWYCH(monkeypatch, tag):
    """Sprawdzenie alfabetu bez DŁUGOŚCI przepuszcza nazwy gałęzi i tagów.

    `beef`, `ada`, `decade` składają się wyłącznie z cyfr szesnastkowych, więc
    przechodziły jako SHA i trafiłyby do `learn.model_version.git_sha` jako
    odpowiedź na pytanie „jaki kod to policzył". Dziś `GIT_SHA` pochodzi
    z `sha-<short>` z workflow `docker-publish`, więc to była luka, a nie
    awaria — ale luka w jedynym miejscu, które ma nie zgadywać.
    """
    from services.engine import wersja

    monkeypatch.setenv("GIT_SHA", tag)
    monkeypatch.setattr(wersja, "_z_repozytorium", lambda: None)
    assert wersja.sha_kodu() is None


def test_sha_kodu_PRZEPUSZCZA_prawdziwe_dlugosci(monkeypatch):
    """Kanarek do poprzedniego: granica nie może odciąć prawdziwych skrótów.

    `git rev-parse --short` daje dziś 7 znaków, pełny sha-1 ma 40, a repozytoria
    wydłużają skrót, gdy rośnie liczba obiektów."""
    from services.engine import wersja

    monkeypatch.setattr(wersja, "_z_repozytorium", lambda: None)
    for sha in ("1298b23", "40f41e4ab", "a" * 40):
        monkeypatch.setenv("GIT_SHA", f"sha-{sha}")
        assert wersja.sha_kodu() == sha


def test_BRUDNE_drzewo_robocze_jest_NAZWANE_w_sha(monkeypatch, tmp_path):
    """`git rev-parse HEAD` opisuje ostatni COMMIT, nie drzewo robocze.

    Scenariusz, dla którego to istnieje: analityk uruchamia lokalnie `train.py`
    bez `--sucho` na niezacommitowanej zmianie w `signals.py`. `train.py:682`
    wpisuje zwrócony sha do `learn.model_version.git_sha`, ktoś później robi
    `git checkout <ten sha>` i NIE ODTWARZA wyniku — a moduł istnieje wyłącznie
    po to, żeby dało się odpowiedzieć bez zgadywania. Sha opisujący inny kod
    niż uruchomiony jest GORSZY niż brak sha, bo wygląda na odpowiedź.
    """
    from services.engine import wersja

    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setattr(wersja, "_czy_brudne", lambda korzen: True)
    sha = wersja.sha_kodu()
    assert sha is not None and sha.endswith(wersja.SUFIKS_BRUDNY), (
        f"sha z brudnego drzewa podany jako czysty: {sha!r}")

    monkeypatch.setattr(wersja, "_czy_brudne", lambda korzen: False)
    czysty = wersja.sha_kodu()
    assert czysty is not None and not czysty.endswith(wersja.SUFIKS_BRUDNY)
    assert sha == czysty + wersja.SUFIKS_BRUDNY


def test_NIEPEWNOSC_co_do_czystosci_liczy_sie_jak_brud(monkeypatch):
    """Gdy `git status` nie odpowie, nie wiemy, czy drzewo było czyste — a
    „nie wiem, czy to ten kod" należy po tej samej stronie co „na pewno nie
    ten". Milcząca odpowiedź bez sufiksu byłaby zapewnieniem, którego nikt
    nie sprawdził."""
    from services.engine import wersja

    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.setattr(wersja, "_czy_brudne", lambda korzen: None)
    sha = wersja.sha_kodu()
    assert sha is not None and sha.endswith(wersja.SUFIKS_BRUDNY)


def test_sha_Z_OBRAZU_nigdy_nie_dostaje_sufiksu(monkeypatch):
    """Obraz buduje się z commita, więc drzewa roboczego tam nie ma. Sufiks
    doklejany również tej ścieżce zanieczyściłby każdy wiersz z produkcji."""
    from services.engine import wersja

    monkeypatch.setenv("GIT_SHA", "sha-1298b23")
    monkeypatch.setattr(wersja, "_czy_brudne", lambda korzen: True)
    assert wersja.sha_kodu() == "1298b23"


def test_trener_zapisuje_sha_i_sklad_sygnalow():
    """Sam commit nie wystarcza: SYGNALY_EKSPERYMENTALNE zmienia skład sygnałów.

    `learn.model_version.git_sha` istnieje od migracji 0006 i przez cały ten czas
    był NULL-em we wszystkich 131 wierszach — przy wynikach modelu nie dało się
    powiedzieć, jaki kod je policzył.
    """
    zrodlo = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    wstawka = zrodlo.split("INSERT INTO learn.model_version", 1)[1].split(
        "mv_id = cur.fetchone()", 1)[0]
    assert "git_sha" in wstawka, "trener nadal nie zapisuje commita"
    assert "sha_kodu()" in wstawka, "kolumna w SQL, ale wartość nie dolatuje"
    assert "sygnaly_eksperymentalne" in wstawka, (
        "commit bez składu sygnałów nie wystarcza - ta sama wersja kodu przy "
        "innej wartości SYGNALY_EKSPERYMENTALNE trenuje inny model")
    # Liczba znaczników i parametrów - ta sama pułapka co przy dzienniku.
    assert wstawka.count("%s") == 8


def test_karta_podaje_trenerowi_wersje_obrazu():
    """W kontenerze nie ma repozytorium git — sha może przyjść tylko z tagu obrazu."""
    szablon = (KORZEN / "charts/trader/templates/trainer-cron.yaml").read_text(
        encoding="utf-8")
    assert "name: GIT_SHA" in szablon
    assert ".Values.api.image.tag" in szablon.split("name: GIT_SHA", 1)[1][:200]
