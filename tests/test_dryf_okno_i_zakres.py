"""Dryf cech: okno pomiaru i zbiór cech, na których się go liczy.

Znaleziska 3 i 6 audytu 2026-09-23. Obie wady mieszkają w jednym bloku
`services/engine/train.py` i obie mają ten sam skutek: `dozor.py` dostaje
liczbę, która nie jest tym, za co ją bierze.

  3. OKNO BYŁO POCHODNĄ `--days`. Przy produkcyjnym `--days 1400` „nowe"
     znaczyło ostatnie 466 dni, a „stare" poprzedzające 934. Dwa sąsiednie
     nocne odczyty dzieliły ~99,8% danych, a `ocen_dryf_trwaly` wymaga
     TRZECH KOLEJNYCH przebiegów powyżej progu w rozstępie 48 h — w
     przekonaniu, że to trzy niezależne dowody. Były jednym, policzonym trzy
     razy: dokładnie ten błąd, przed którym broni `DRYF_TRWALY_GODZIN`
     warstwę wyżej.

  6. `max_psi` LICZYŁ SIĘ PO WSZYSTKICH AKTYWNYCH CECHACH — także tych ze
     świecy 1h i 5m i z eksperymentalnych uniwersów. `load_panel` filtruje je
     ściśle i ma na to spisane uzasadnienie; blok dryfu nie miał ani jednego
     z tych filtrów. Przebiegi `--bar 4h` i `--bar 1h` zapisywały więc TĘ SAMĄ
     liczbę, a `ocen_dryf_trwaly` filtruje historię po `bar` wierząc, że
     rozdziela dwa modele — rozdzielał dwa wiersze o identycznej wartości.

DLACZEGO TEST CZYTA ŹRÓDŁO, A NIE URUCHAMIA POMIARU. Blok dryfu jest środkiem
`train()` — żeby do niego dojść, trzeba bazy, paneli i całego walk-forward.
A obie wady mieszkają w TREŚCI zapytań i w GRANICACH okien, nie w liczbie,
która z nich wychodzi: przebieg na danych pokazałby `max_psi` i nie
powiedziałby ani z jakiego okna, ani po których cechach. Test zawężony do
samego bloku (etykiety sekcji istnieją w tym pliku od początku) patrzy
dokładnie na ten poziom, na którym ta wada istnieje.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.engine import train

KORZEN = Path(__file__).resolve().parents[1]
TERAZ = datetime(2026, 9, 23, 3, 0, tzinfo=timezone.utc)


def _blok_dryfu() -> str:
    """Sam blok dryfu ze źródła, bez komentarzy.

    Granice są etykietami sekcji, które w tym pliku istnieją od początku —
    zawężenie do nich znaczy, że test nie zapala się od zmian w sąsiedztwie.
    """
    zrodlo = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    od = zrodlo.index("# --- dryf cech ---")
    # Koniec bloku = koniec funkcji `dryf_cech` (od refaktoru T2 dryf jest
    # osobna funkcja; wczesniej konczyla go etykieta „podsumowanie").
    do = zrodlo.index("\ndef ", od)
    return "\n".join(w for w in zrodlo[od:do].splitlines()
                     if not w.lstrip().startswith("#"))


# ------------------------------------------------------- znalezisko 3: okno

def test_okno_dryfu_NIE_jest_pochodna_dlugosci_panelu():
    """`days // 3` przy `--days 1400` to okno 466-dniowe. Kolejny nocny odczyt
    wymienia w nim 1/466 danych, czyli nie jest nowym pomiarem — a cała
    maszyneria trwałości w dozorze zakłada, że jest."""
    blok = _blok_dryfu()
    assert "days // 3" not in blok, (
        "okno dryfu nadal liczy się z `--days` — przy 1400 dniach trzy kolejne "
        "przebiegi dzielą ~99,8% danych i nie są trzema dowodami")
    assert "DRYF_OKNO_NOWE_DNI" in blok and "DRYF_OKNO_STARE_DNI" in blok, (
        "okno dryfu musi mieć własny parametr, niezależny od długości panelu "
        "treningowego — to dwie różne wielkości i wiąże je tylko przypadek")


def test_okno_dryfu_jest_porownywalne_z_rytmem_reakcji_dozoru():
    """Próg przydatności okna jest wyprowadzalny, a nie dobrany.

    `dozor.DRYF_TRWALY_PRZEBIEGOW` kolejnych odczytów w rozstępie
    `DRYF_TRWALY_GODZIN` ma być trzema niezależnymi dowodami. Żeby kolejny
    odczyt niósł cokolwiek nowego, doba musi być zauważalną częścią okna —
    przyjmujemy, że okno nie może być dłuższe niż kwartał. Przy 466 dniach
    doba to 0,2% okna.
    """
    from services.engine.dozor import DRYF_TRWALY_GODZIN, DRYF_TRWALY_PRZEBIEGOW

    rozstep_dni = DRYF_TRWALY_GODZIN / 24.0 / max(DRYF_TRWALY_PRZEBIEGOW - 1, 1)
    udzial = rozstep_dni / train.DRYF_OKNO_NOWE_DNI
    assert udzial >= 0.02, (
        f"kolejny odczyt wymienia {udzial:.1%} okna ({train.DRYF_OKNO_NOWE_DNI} "
        f"dni) — 'trzy kolejne przebiegi' to wtedy jeden dowód policzony trzy "
        f"razy")
    assert train.DRYF_OKNO_NOWE_DNI < train.DRYF_OKNO_STARE_DNI, (
        "okno stare ma być ODNIESIENIEM, a nie drugim pomiarem bieżącego stanu")


def test_okna_dryfu_NIE_ZACHODZA_na_siebie():
    """Wspólna granica, a nie dwa niezależne przedziały: wartość policzona raz
    do „starych" i raz do „nowych" zaniżałaby przesunięcie średniej dokładnie
    o tyle, o ile jest wspólna. Stąd `>= granica_stare AND < granica_nowe`
    z jednej strony i `>= granica_nowe` z drugiej."""
    blok = _blok_dryfu()
    assert "event_time >= %s AND event_time < %s" in " ".join(blok.split()), (
        "okno stare używa `BETWEEN` albo innego przedziału domkniętego "
        "obustronnie — wartość na granicy wpada do obu okien")


# ------------------------------------------------ znalezisko 6: zakres cech

def test_dryf_liczy_sie_po_cechach_TEGO_horyzontu():
    """Bez filtru po `bar` przebieg 4h i przebieg 1h zapisują tę samą liczbę,
    a `ocen_dryf_trwaly` filtruje historię po `bar` wierząc, że rozdziela dwa
    modele. `max` po nadzbiorze jest zawsze >= `max` po podzbiorze, więc błąd
    działa w stronę FAŁSZYWEGO ALARMU — a czujka, która miga, uczy
    odklikiwać."""
    blok = " ".join(_blok_dryfu().split())
    assert "params->>'bar' = %s" in blok, (
        "feature_def bez filtru po świecy — `max_psi` bierze cechy, których "
        "model tego horyzontu w ogóle nie widzi")


def test_dryf_liczy_sie_po_cechach_TEGO_uniwersum():
    """Ten sam filtr, co w `load_panel`, i z tego samego powodu: dwa warianty
    cechy przekrojowej policzone dla różnych zbiorów to dwie różne cechy."""
    blok = " ".join(_blok_dryfu().split())
    assert "params->>'uniwersum' IS NULL OR params->>'uniwersum' = %s" in blok, (
        "feature_def bez odcisku uniwersum — eksperymentalny wariant cechy "
        "podnosi `max_psi` przebiegu produkcyjnego")


def test_dryf_liczy_sie_po_instrumentach_TEGO_przebiegu():
    """Agregat po wszystkich instrumentach w bazie miesza pary, których to
    uniwersum nie handluje, z tymi, na których stoi model."""
    blok = " ".join(_blok_dryfu().split())
    assert "instrument_id = ANY(%s)" in blok, (
        "feature_value bez filtru po instrumentach przebiegu")


def test_filtry_dryfu_sa_TE_SAME_co_w_load_panel():
    """Kanarek do dwóch poprzednich. Dwa miejsca decydujące, które cechy
    należą do modelu, rozjeżdżają się po cichu — a rozjazd widać dopiero
    wtedy, gdy czujka alarmuje o cesze, której model nie widzi."""
    zrodlo = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    panel = zrodlo[zrodlo.index("def load_panel"):zrodlo.index("def atr_threshold")]
    for warunek in ("params->>'bar'", "params->>'uniwersum'"):
        assert warunek in panel, f"{warunek} zniknął z load_panel"
        assert warunek in _blok_dryfu(), f"{warunek} zniknął z bloku dryfu"


# ------------------------------------------------ granice okien, nie ich nazwy

def test_granice_okien_liczone_od_DZIS_a_nie_od_poczatku_panelu():
    """Stała o dobrej nazwie, użyta w złym miejscu, wygląda w przeglądzie zmian
    jak poprawka.

    Blok dryfu jest środkiem `train()`, którego nie da się wywołać bez bazy,
    paneli i całego walk-forward — a wada mieszka w GRANICACH okien, nie
    w liczbie, która z nich wychodzi. Dlatego patrzymy na to, od czego te
    granice się liczą: okno nowe kończy się DZIŚ i ma dokładnie
    `DRYF_OKNO_NOWE_DNI`, a stare przylega do niego od dołu, bez dziury
    i bez zakładki — każde z tych dwóch przesuwa mianownik wskaźnika.
    """
    blok = " ".join(_blok_dryfu().split())
    assert "now - timedelta(days=DRYF_OKNO_NOWE_DNI)" in blok, (
        "granica okna nowego nie liczy się od `now` — okno liczone od początku "
        "panelu wygląda identycznie w przeglądzie zmian i mierzy co innego")
    assert "granica_nowe - timedelta(days=DRYF_OKNO_STARE_DNI)" in blok, (
        "okno stare nie przylega do nowego — zostaje dziura albo zakładka")

    granica_nowe = TERAZ - timedelta(days=train.DRYF_OKNO_NOWE_DNI)
    granica_stare = granica_nowe - timedelta(days=train.DRYF_OKNO_STARE_DNI)
    assert granica_stare < granica_nowe < TERAZ
    assert (TERAZ - granica_nowe).days == train.DRYF_OKNO_NOWE_DNI
    assert (granica_nowe - granica_stare).days == train.DRYF_OKNO_STARE_DNI


def test_okno_dryfu_ma_uzasadnienie_przy_stalej():
    """W tym pliku każdy próg niesie wyprowadzenie. Liczba bez niego zostanie
    przy pierwszym niewygodnym przebiegu podniesiona i nikt nie będzie umiał
    powiedzieć, co się przez to zmieniło."""
    zrodlo = (KORZEN / "services/engine/train.py").read_text(encoding="utf-8")
    przed = zrodlo[:zrodlo.index("DRYF_OKNO_NOWE_DNI = ")]
    komentarz = przed[przed.rindex("\n\n"):]
    assert len(re.findall(r"^#", komentarz, re.M)) >= 8, (
        "stała okna dryfu bez wyprowadzenia — w tym pliku to jest wada")
