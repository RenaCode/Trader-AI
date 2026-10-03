"""Narzedzie pomiarowe strony krotkiej - czesci, ktore da sie sprawdzic bez bazy.

DLACZEGO TO W OGOLE TESTUJEMY

Bo to jest narzedzie, ktorego wynik ma rozstrzygnac decyzje o kierunku calego
projektu ("odblokowac krotka strone czy nie"). Narzedzie pomiarowe, ktore samo
nie jest sprawdzone, dostarcza liczbe wygladajaca na pomiar - a to jest gorsze
niz brak liczby, bo brak widac.
"""
import ast
from pathlib import Path


from services.engine.wartosc_krotkiej import przedzial

ZRODLO = Path(__file__).resolve().parents[1] / "services/engine/wartosc_krotkiej.py"


def test_przedzial_jest_POWTARZALNY():
    """Bootstrap losuje, wiec bez stalego ziarna ta sama proba dawalaby za kazdym
    razem inny przedzial. Wynik, ktorego nie da sie powtorzyc, nie nadaje sie do
    tego, zeby na niego powolac sie w decyzji."""
    proba = [0.01, -0.02, 0.003, 0.015, -0.008, 0.002, -0.011, 0.004]
    assert przedzial(proba) == przedzial(proba)


def test_przedzial_obejmuje_zero_dla_proby_bez_przewagi():
    """Proba symetryczna wokol zera NIE MOZE dac przedzialu po jednej stronie -
    inaczej narzedzie znajdowaloby przewage w szumie, czyli dokladnie tam, gdzie
    ten projekt juz raz ja 'znalazl' (Sharpe 0,0452)."""
    proba = [0.01, -0.01] * 30
    lo, hi = przedzial(proba)
    assert lo <= 0 <= hi


def test_przedzial_NIE_obejmuje_zera_dla_proby_z_wyrazna_przewaga():
    """Kanarek do poprzedniego: gdyby przedzial byl zawsze szeroki, tamten test
    przechodzilby niezaleznie od tego, co narzedzie liczy."""
    proba = [0.02, 0.018, 0.021, 0.019, 0.022, 0.017] * 10
    lo, hi = przedzial(proba)
    assert lo > 0


def test_proba_jednoelementowa_nie_udaje_przedzialu():
    """Jedna transakcja nie ma rozrzutu. `nan` mowi 'nie da sie policzyc';
    liczba w tym miejscu mowilaby 'policzono' i byla by nieprawda."""
    lo, hi = przedzial([0.01])
    assert lo != lo and hi != hi        # nan != nan


def test_narzedzie_jest_WYLACZNIE_do_odczytu():
    """POMIAR NIE MA PRAWA ZMIENIC MIERZONEGO.

    Skrypt chodzi po bazie produkcyjnej i po dzienniku decyzji, ktory jest
    z zalozenia niemutowalny (trigger `journal.forbid_mutation` z migracji
    0005). Sprawdzamy to po TRESCI ZAPYTAN, a nie po deklaracji w docstringu -
    deklaracja przetrwa kazda zmiane kodu, a ten warunek nie.
    """
    zrodlo = ZRODLO.read_text()
    drzewo = ast.parse(zrodlo)
    zapytania = [
        w.value.upper()
        for w in ast.walk(drzewo)
        if isinstance(w, ast.Constant) and isinstance(w.value, str)
    ]
    zakazane = ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "DROP ", "ALTER ")
    for q in zapytania:
        for slowo in zakazane:
            assert slowo not in q, f"narzędzie pomiarowe pisze do bazy: {q[:80]}"
    # `conn.commit()` tez nie ma prawa wystapic - transakcja tylko czytajaca
    # nie ma czego zatwierdzac, a jego obecnosc znaczylaby, ze ktos cos zapisal.
    assert ".commit()" not in zrodlo


def test_narzedzie_NIE_SKLADA_ZLECEN():
    """Konto demo jest uzywane do pomiaru i nie wolno go zanieczyscic. Skrypt
    analityczny nie ma powodu dotykac adaptera gieldy w ogole.

    SPRAWDZAMY WYWOLANIA I IMPORTY, A NIE TEKST ZRODLA.

    Pierwsza wersja szukala napisu "zloz_zlecenie" w calym pliku i zapalila sie
    na... zdaniu w docstringu, ktore TLUMACZY, czego symulacja nie modeluje
    ("produkcja sklada je z PostOnly - `bybit.zloz_zlecenie`"). Sprawdzenie po
    tekscie karze wiec za opisanie ryzyka i zacheca do usuniecia opisu zamiast
    do usuniecia wywolania - czyli dziala dokladnie odwrotnie, niz powinno.

    Wersja po drzewie skladni patrzy na to, co kod ROBI. Taki warunek nie da sie
    obejsc komentarzem i nie przeszkadza w pisaniu prawdy o ograniczeniach.
    """
    drzewo = ast.parse(ZRODLO.read_text())

    wolane = {
        w.func.attr if isinstance(w.func, ast.Attribute) else
        (w.func.id if isinstance(w.func, ast.Name) else "")
        for w in ast.walk(drzewo) if isinstance(w, ast.Call)
    }
    for zakazane in ("zloz_zlecenie", "anuluj_zlecenie", "_post"):
        assert zakazane not in wolane, f"narzędzie pomiarowe woła {zakazane}()"

    importowane = [
        m.module or ""
        for m in ast.walk(drzewo) if isinstance(m, ast.ImportFrom)
    ] + [
        a.name
        for m in ast.walk(drzewo) if isinstance(m, ast.Import) for a in m.names
    ]
    assert not any(m.startswith("services.exchanges") for m in importowane), \
        "narzędzie pomiarowe importuje adapter giełdy — nie ma po co"


def test_narzedzie_przejezdza_ZAKRES_poslizgu_a_nie_jedna_wartosc():
    """POSLIZG MA DZIS ZERO OBSERWACJI, WIEC NIE WOLNO PODAC GO JAKO LICZBY.

    Wszystkie piec wypelnien gieldowych na produkcji ma `exec.order.limit_price`
    rowne cenie wypelnienia co do grosza - nie dlatego, ze poslizgu nie bylo,
    tylko dlatego, ze do wdrozenia z 2026-09-23 17:58 kolumna "cena wyslana"
    byla nadpisywana cena wykonania.

    Wynik wyceny krotkiej strony podany przy jednej wartosci takiego zalozenia
    mialby dokladnie te wade, co dzisiejsze Sharpe 0,0452: wygladalby na pomiar.
    Narzedzie ma wiec pokazywac ZAKRES i miejsce, w ktorym wniosek sie lamie.
    """
    drzewo = ast.parse(ZRODLO.read_text())
    flagi = [
        w.args[0].value
        for w in ast.walk(drzewo)
        if isinstance(w, ast.Call)
        and isinstance(w.func, ast.Attribute)
        and w.func.attr == "add_argument"
        and w.args and isinstance(w.args[0], ast.Constant)
    ]
    assert "--poslizg-bps" in flagi, "brak przełącznika zakresu poślizgu"

    domysl = next(
        w for w in ast.walk(drzewo)
        if isinstance(w, ast.Call) and isinstance(w.func, ast.Attribute)
        and w.func.attr == "add_argument"
        and w.args and getattr(w.args[0], "value", None) == "--poslizg-bps"
    )
    wartosci = next(k.value for k in domysl.keywords if k.arg == "default")
    assert len(wartosci.elts) >= 3, (
        "domyślny zakres musi mieć co najmniej trzy punkty — dwa nie pokazują, "
        "GDZIE wniosek się łamie, a jeden to z powrotem liczba udająca pomiar")


def test_wydruk_NAZYWA_poslizg_zalozeniem():
    """Tabela wrazliwosci bez tego zdania nadal wyglada jak pomiar - czytelnik
    zobaczy cztery liczby i wezmie srodkowa za prawde."""
    zrodlo = ZRODLO.read_text()
    assert "NIE JEST DZIŚ ZMIERZONY" in zrodlo
    assert "ZAŁOŻENIEM" in zrodlo or "ZAKRES ZAŁOŻEŃ" in zrodlo
