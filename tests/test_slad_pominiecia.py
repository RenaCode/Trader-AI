"""Pominięty instrument ZOSTAWIA ŚLAD — a `continue` bez śladu jest błędem.

CO SIĘ STAŁO

Pierwszy przebieg `run_once` miał dwie ścieżki, którymi instrument wypadał
z cyklu bez wiersza w bazie i bez licznika w pulsie:

    len(features) < 4    -> continue
    price is None        -> continue

Druga z nich nie wypisywała nawet komunikatu przy `--verbose`. Po takim
przebiegu w bazie nie było NICZEGO: instrument pominięty wyglądał dokładnie tak
samo jak instrument, którego silnik nie zdążył rozważyć, i tak samo jak silnik,
który w ogóle nie chodził. Na pytanie „czemu 5 września o 12:00 nie weszliśmy
w SOLUSDC" odpowiedzieć się nie dało.

DLACZEGO TEST STRUKTURALNY, A NIE PRZEBIEG `run_once`

`run_once` woła giełdę, risk kernel i pomiar kapitału; postawienie go na atrapie
kosztuje więcej niż sam mechanizm, o który tu chodzi, i pilnowałoby JEDNEJ
ścieżki — tej, którą akurat podłożono. Pilnowana własność jest ogólniejsza
i dotyczy każdej przyszłej gałęzi: ŻADNE `continue` w pętli oceniającej
instrumenty nie może być ciche. Tego nie da się sprawdzić przykładem, bo
przykład nie wie o gałęzi, której jeszcze nie ma.

Detektor ma własny kanarek (`test_wykrywacz_LAPIE_ciche_continue`): karmiony
kodem sprzed poprawki musi go zgłosić. Bez tego cisza tego pliku znaczyłaby
„parser nic nie zrozumiał", a nie „czysto".
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


KORZEN = Path(__file__).resolve().parents[1]
LOOP = KORZEN / "services" / "engine" / "loop.py"
# DRUGI PISARZ TEJ TABELI. Od 2026-09-26 pominięcie zapisuje także egzekucja:
# odmowa WYSŁANIA wejścia, gdy cena odjechała od ceny decyzyjnej albo gdy
# z giełdy nie da się odczytać żadnej ceny z chwili wysyłki. Szukanie powodów
# tylko w `loop.py` zostawiałoby te dwa poza porównaniem z CHECK-iem — czyli
# dokładnie tam, gdzie rozjazd kodu ze schematem wywraca cykl na produkcji,
# a test świeci na zielono.
PISARZE = (LOOP, KORZEN / "services" / "engine" / "gielda_exec.py")
MIGRACJA = KORZEN / "db" / "migrations" / "0041_pominiete_instrumenty.sql"

# Nazwa funkcji zapisującej ślad i nazwa słownika liczników. Wyciągnięte do
# stałych, bo detektor pyta o NIE, a nie o konkretną treść gałęzi.
ZAPIS = "zapisz_pominiecie"
LICZNIKI = "stats"


# ------------------------------------------------------------------ detektor

def _bloki(wezel: ast.AST):
    """Wszystkie listy instrukcji wewnątrz węzła (body, orelse, finalbody...)."""
    for _pole, wartosc in ast.iter_fields(wezel):
        if isinstance(wartosc, list) and any(isinstance(x, ast.stmt) for x in wartosc):
            yield wartosc
        elif isinstance(wartosc, ast.AST):
            pass
    for dziecko in ast.iter_child_nodes(wezel):
        yield from _bloki(dziecko)


def _zostawia_slad(blok: list[ast.stmt]) -> bool:
    """Czy ten blok zapisuje ślad, zanim przerwie iterację.

    Wystarczy JEDNO z dwóch: wiersz w `journal.pominiecie` albo podniesiony
    licznik w `stats`. Licznik sam w sobie wystarcza tam, gdzie ślad zostaje
    gdzie indziej — tak jest przy `poza_swieca`, które znaczy „decyzja dla tej
    świecy JUŻ JEST w journal.decision".
    """
    for wezel in ast.walk(ast.Module(body=blok, type_ignores=[])):
        if isinstance(wezel, ast.Call) and isinstance(wezel.func, ast.Name) \
                and wezel.func.id == ZAPIS:
            return True
        if isinstance(wezel, ast.AugAssign) and isinstance(wezel.target, ast.Subscript) \
                and isinstance(wezel.target.value, ast.Name) \
                and wezel.target.value.id == LICZNIKI:
            return True
    return False


def ciche_pominiecia(zrodlo: str, funkcja: str = "run_once",
                     petla: str = "instrumenty") -> list[int]:
    """Numery linii `continue`, które NIE zostawiają po sobie żadnego śladu.

    Patrzy wyłącznie w pętlę iterującą po `petla` wewnątrz `funkcja` — czyli
    w miejsce, w którym instrument może wypaść z uniwersum decyzyjnego.
    """
    drzewo = ast.parse(zrodlo)
    cel = None
    for wezel in ast.walk(drzewo):
        if isinstance(wezel, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and wezel.name == funkcja:
            cel = wezel
    assert cel is not None, f"nie znaleziono funkcji {funkcja}"

    petle = [w for w in ast.walk(cel)
             if isinstance(w, ast.For) and isinstance(w.iter, ast.Name)
             and w.iter.id == petla]
    assert petle, f"nie znaleziono pętli `for ... in {petla}` w {funkcja}"

    ciche = []
    for p in petle:
        for blok in _bloki(p):
            if any(isinstance(x, ast.Continue) for x in blok) \
                    and not _zostawia_slad(blok):
                ciche += [x.lineno for x in blok if isinstance(x, ast.Continue)]
    return ciche


# ------------------------------------------------------------------- sprawdzenia

def test_zadne_continue_w_ocenie_instrumentow_NIE_JEST_CICHE():
    """To jest cała reguła: instrument nie wypada z cyklu bez zostawienia liczby.

    Reguła jest szersza niż dwa znane przypadki celowo — trzecia gałąź dopisana
    za pół roku ma się o ten test wywrócić, zanim wyjedzie na produkcję.
    """
    ciche = ciche_pominiecia(LOOP.read_text(encoding="utf-8"))
    assert ciche == [], (
        "instrument wypada z pętli oceniającej bez śladu w bazie i bez licznika; "
        f"linie w services/engine/loop.py: {ciche}")


def test_wykrywacz_LAPIE_ciche_continue():
    """Kanarek detektora: kod SPRZED poprawki musi zostać zgłoszony.

    Kształt przepisany z `run_once` sprzed zmiany — dwa gołe `continue`
    i jeden `continue` poprzedzony licznikiem, który zgłoszony BYĆ NIE MOŻE.
    """
    przed = (
        "async def run_once(conn):\n"
        "    for inst in instrumenty:\n"
        "        if decided_for_bar(cur, engine, mode, iid, bar_start, h):\n"
        "            stats['poza_swieca'] += 1\n"
        "            continue\n"
        "        features = latest_features(cur, iid, now, h, feature_ids)\n"
        "        if len(features) < 4:\n"
        "            if verbose:\n"
        "                print('pominięty')\n"
        "            continue\n"
        "        price = mark_price(cur, iid, now)\n"
        "        if price is None:\n"
        "            continue\n"
        "        stats['rozwazone'] += 1\n"
    )
    assert ciche_pominiecia(przed) == [10, 13]


def test_wykrywacz_NIE_krzyczy_na_sladzie_zapisanym():
    """Druga strona kanarka: poprawiona gałąź ma przechodzić."""
    po = (
        "async def run_once(conn):\n"
        "    for inst in instrumenty:\n"
        "        if len(features) < MIN_CECH:\n"
        "            stats['pominiete_bez_cech'] += 1\n"
        "            zapisz_pominiecie(cur, engine, mode, iid, bar_start,\n"
        "                              'za_malo_cech', opis)\n"
        "            continue\n"
    )
    assert ciche_pominiecia(po) == []


# --------------------------------------------- zgodność kodu ze schematem

def _check_powodow() -> set[str]:
    """Dopuszczalne wartości `powod` odczytane z migracji, a nie przepisane.

    OSTATNIA migracja, która definiuje ten CHECK, wygrywa — 0048 przebudowała
    ograniczenie z 0041. Czytanie tylko 0041 opisywałoby bazę sprzed niej."""
    ostatni = None
    for plik in sorted(MIGRACJA.parent.glob("*.sql")):
        m = re.search(r"CHECK \(powod IN \(([^)]*)\)\)", plik.read_text(encoding="utf-8"))
        if m:
            ostatni = m
    assert ostatni, "żadna migracja nie ma CHECK-a na kolumnie `powod`"
    return set(re.findall(r"'([^']+)'", ostatni.group(1)))


def _powody_w_kodzie() -> set[str]:
    """Wartości `powod` faktycznie przekazywane do `zapisz_pominiecie`.

    Czytamy WSZYSTKICH pisarzy tej tabeli (`PISARZE`), a nie sam `loop.py`.
    Plik pominięty w tej liście jest luką cichą w jedną stronę: jego powody nie
    trafiają do porównania z CHECK-iem, więc powód dopisany tam bez migracji
    wywraca cykl dopiero na produkcji.
    """
    powody = set()
    for plik in PISARZE:
        assert plik.exists(), f"pisarz {plik} nie istnieje"
        drzewo = ast.parse(plik.read_text(encoding="utf-8"))
        for wezel in ast.walk(drzewo):
            if isinstance(wezel, ast.Call) and isinstance(wezel.func, ast.Name) \
                    and wezel.func.id == ZAPIS:
                # sygnatura: (cur, engine, mode, instrument_id, bar_start, powod, szczegol)
                arg = wezel.args[5]
                assert isinstance(arg, ast.Constant), \
                    "`powod` musi być stałą — inaczej CHECK w bazie sprawdza się dopiero na produkcji"
                powody.add(arg.value)
    return powody


def test_powody_w_kodzie_i_w_CHECKU_to_ten_sam_zbior():
    """Rozjazd tych dwóch zbiorów jest cichy w jedną stronę i głośny w drugą.

    Powód dopisany w kodzie, a nie w CHECK-u, wywraca cykl na produkcji.
    Powód w CHECK-u, którego kod nie używa, to martwa wartość, której nikt nie
    szuka w zapytaniach. Test pilnuje obu kierunków.
    """
    assert _powody_w_kodzie() == _check_powodow()


def test_zapis_sladu_uzywa_kolumn_ktore_ISTNIEJA_w_migracji():
    """Nazwy kolumn i cel `ON CONFLICT` czytane z INSERT-a i z DDL-a.

    `ON CONFLICT (...)` musi wskazywać dokładnie klucz główny — inaczej Postgres
    odrzuca instrukcję komunikatem „no unique or exclusion constraint matching",
    a cały cykl decyzyjny wywraca się na zapisie pomocniczym.
    """
    zrodlo = LOOP.read_text(encoding="utf-8")
    ins = re.search(r"INSERT INTO journal\.pominiecie \(([^)]*)\)", zrodlo, re.S)
    assert ins, "brak INSERT-a do journal.pominiecie"
    kolumny_kodu = [c.strip() for c in ins.group(1).replace('"', " ").split(",")]
    kolumny_kodu = [c for c in kolumny_kodu if c]

    sql = MIGRACJA.read_text(encoding="utf-8")
    ddl = sql[sql.index("CREATE TABLE IF NOT EXISTS journal.pominiecie"):]
    for kol in kolumny_kodu:
        assert re.search(rf"^\s+{kol}\s", ddl, re.M), \
            f"kolumna {kol} nie istnieje w journal.pominiecie"

    konflikt = re.search(r"ON CONFLICT \(([^)]*)\) DO NOTHING", zrodlo, re.S)
    assert konflikt, "zapis śladu bez ON CONFLICT — powtórka w tej samej świecy wywali cykl"
    cel = [c.strip() for c in konflikt.group(1).replace('"', " ").split(",")]

    pk = re.search(r"PRIMARY KEY \(([^)]*)\)", ddl)
    assert pk, "journal.pominiecie bez klucza głównego"
    assert cel == [c.strip() for c in pk.group(1).split(",")]


def test_licznik_pominiec_jest_ZAKLADANY_a_nie_dopisywany_warunkowo():
    """Klucz nieobecny w `detail` znaczy tyle samo, co klucz o wartości zero.

    `risk.engine_heartbeat` jest nadpisywana co przebieg (ON CONFLICT (engine)
    DO UPDATE), więc licznik zakładany dopiero przy pierwszym wystąpieniu daje
    puls, w którym „nic nie pominięto" jest nieodróżnialne od „ta wersja silnika
    w ogóle tego nie liczy".
    """
    zrodlo = LOOP.read_text(encoding="utf-8")
    init = re.search(r"stats = \{(.*?)\n\n", zrodlo, re.S)
    assert init, "nie znaleziono inicjalizacji `stats` w loop.py"
    for klucz in ("pominiete_bez_cech", "pominiete_bez_ceny", "instrumenty"):
        assert f'"{klucz}": 0' in init.group(1), \
            f"`{klucz}` nie jest zakładany zerem przy starcie przebiegu"


def test_bramka_pokrycia_odklada_decyzje_PRZED_jej_zapisem():
    """22.09.2026: przez 11 dni silnik decydował na 2 z 9 sygnałów, bo `MIN_CECH`
    liczy sztuki, a 8 cech pochodnych jest gotowych 20–35 min przed cenowymi.
    Bramka ma mierzyć POKRYCIE WAG i odkładać decyzję (continue w pierwszym
    przebiegu, ze śladem), a nie ją zapisywać z pokryciem 0,05."""
    from services.engine import loop

    assert 0.5 <= loop.MIN_POKRYCIE < 1.0, "1,0 odkładałoby decyzję przy braku newsów"
    zrodlo = LOOP.read_text(encoding="utf-8")
    bramka = zrodlo.index("if pokrycie < MIN_POKRYCIE:")
    slad = zrodlo.index('"niepelne_sygnaly"', bramka)
    zapis = zrodlo.index("oceny.append(", bramka)
    assert bramka < slad < zapis, "ślad odłożenia ma poprzedzać dopisanie do ocen"
    assert "continue" in zrodlo[slad:zapis]
