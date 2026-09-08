"""Pozycjonowanie tłumu jako CECHA, nie jako polecenie handlowe.

Pytanie brzmiało „czy kopiować ruchy najlepszych traderów". Kopiować się nie da
(brak API, ranking blokowany, podmiot unijny bez perpetuali) i nie warto:
czołówka rankingu przy dziesiątkach tysięcy uczestników to prawy ogon rozkładu,
zdominowany przez szczęście pomnożone przez dźwignię. Wybór po fakcie, bez
próby out-of-sample, to dokładnie ten błąd, przeciwko któremu zbudowany jest
cały ten system.

Zagregowane pozycjonowanie jest natomiast mierzalne — i te testy pilnują
JEDYNEJ rzeczy, która mogłaby uczynić pomiar bezwartościowym: zaglądania
w przyszłość. Przeciek przyszłości POPRAWIA wynik backtestu, więc nie objawia
się jako awaria; objawia się jako strategia, która działa wyłącznie na papierze.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services.engine.horizons import get as get_horizon
from services.ingest import features

CHWILA = datetime(2026, 9, 1, tzinfo=timezone.utc)


class KursorAtrapa:
    """Oddaje meta instrumentu, potem odczyty pozycjonowania."""

    def __init__(self, odczyty, src=99):
        self._odczyty = odczyty
        self._src = src
        self._faza = 0

    def execute(self, sql, params=None):
        self._faza = 1 if "meta->>'funding_from'" in sql else 2

    def fetchone(self):
        return {"src": self._src}

    def fetchall(self):
        return self._odczyty


def policz(odczyty, *, bar="4h", dni=40):
    """Zwraca {nazwa_cechy: [(czas, wartosc, znane_od)]}."""
    h = get_horizon(bar)
    ids = {"long_ratio": 1, "long_ratio_z": 2, "oi_zmiana": 3}
    zapisane = []

    pierwotne = features.insert_values
    features.insert_values = lambda cur, rows: (zapisane.extend(rows), len(rows))[1]
    try:
        features.compute_positioning(
            KursorAtrapa(odczyty), 7, h, ids,
            CHWILA - timedelta(days=dni), CHWILA)
    finally:
        features.insert_values = pierwotne

    nazwy = {v: k for k, v in ids.items()}
    out: dict[str, list] = {k: [] for k in ids}
    for fid, _inst, t, wartosc, znane in zapisane:
        out[nazwy[fid]].append((t, wartosc, znane))
    return out


def odczyt(t, udzial=None, oi=None):
    return {"event_time": t, "long_ratio": udzial, "open_interest": oi}


# ============================================ przeciek przyszlosci

def test_odczyt_PO_zamknieciu_swiecy_NIE_wchodzi_do_tej_swiecy():
    """Odczyt z 08:00 nie może opisywać świecy zaczynającej się o 04:00.

    To jest jedyny błąd w tym module, który POPRAWIA wynik backtestu — więc nie
    zgłosi się sam. Strategia wyglądałaby na skuteczną dokładnie do pierwszego
    dnia na żywym rynku.
    """
    swieca = datetime(2026, 8, 20, 4, tzinfo=timezone.utc)
    wynik = policz([
        odczyt(swieca, udzial=0.50),
        odczyt(swieca + timedelta(hours=1), udzial=0.90),   # JUZ W TRAKCIE swiecy
    ])
    pasujace = [w for t, w, _ in wynik["long_ratio"] if t == swieca]
    assert pasujace == [50.0], (
        f"świeca 04:00 dostała {pasujace} — wartość 90 pochodzi z 05:00, "
        f"czyli z przyszłości względem decyzji podejmowanej o 04:00")


def test_wartosc_jest_znana_dopiero_po_ZAMKNIECIU_swiecy():
    """Bitemporalność: `znane_od` musi wskazywać koniec świecy, nie jej początek.
    Bez tego backtest sięga po wartość wcześniej, niż ona istniała."""
    swieca = datetime(2026, 8, 20, 4, tzinfo=timezone.utc)
    wynik = policz([odczyt(swieca, udzial=0.5)])
    (t, _, znane), = [w for w in wynik["long_ratio"] if w[0] == swieca]
    assert znane >= t + timedelta(hours=4), (
        f"cecha świecy {t} zgłoszona jako znana od {znane} — przed jej zamknięciem")


# ============================================ zmiana otwartych pozycji

def test_zmiana_OI_liczona_miedzy_SWIECAMI_a_nie_miedzy_odczytami():
    """Odczyty są godzinowe, świeca czterogodzinna. Liczenie wobec poprzedniego
    ODCZYTU dałoby cechę opisującą jedną godzinę z czterech — i to nie tę,
    w której podejmowana jest decyzja."""
    baza = datetime(2026, 8, 20, 0, tzinfo=timezone.utc)
    odczyty = []
    for i in range(9):                       # 9 godzin: OI rosnie o 10% co godzine
        odczyty.append(odczyt(baza + timedelta(hours=i), oi=100 * (1.1 ** i)))
    wynik = policz(odczyty)
    zmiany = [round(w, 2) for t, w, _ in wynik["oi_zmiana"]
              if baza <= t <= baza + timedelta(hours=8)]
    # Miedzy swiecami 4h mija czteroktotny wzrost o 10%: 1.1**4 - 1 = 46,41%
    assert zmiany and all(abs(z - 46.41) < 0.1 for z in zmiany), (
        f"zmiany {zmiany} — wygląda na liczenie wobec poprzedniej GODZINY "
        f"(10%), nie wobec poprzedniej świecy")


def test_poziom_OI_NIE_jest_cecha():
    """Poziom otwartych pozycji rośnie latami razem z rynkiem, więc jako cecha
    byłby głównie zegarem — model nauczyłby się z niego daty, nie zachowania."""
    assert "open_interest" not in features.ROLES
    assert "oi_poziom" not in features.ROLES


# ============================================ dwa zrodla, jedna tabela

def test_brak_JEDNEGO_zrodla_nie_kasuje_drugiego():
    """Źródła przychodzą z osobnych wywołań i mogą zawieść niezależnie. Godzina
    bez udziału kont nie może wymazać ostatniego znanego udziału — inaczej
    dziura w jednym API robi dziurę w obu cechach."""
    baza = datetime(2026, 8, 20, 0, tzinfo=timezone.utc)
    wynik = policz([
        odczyt(baza, udzial=0.60, oi=100),
        odczyt(baza + timedelta(hours=4), oi=120),          # bez udzialu
    ])
    po = [w for t, w, _ in wynik["long_ratio"] if t == baza + timedelta(hours=4)]
    assert po == [60.0], (
        f"świeca bez świeżego udziału kont dostała {po} zamiast ostatniej "
        f"znanej wartości")


def test_zapis_NIE_NADPISUJE_istniejacej_wartosci_pustka():
    """Drugi przebieg, w którym jedno źródło zawiodło, ma UZUPEŁNIĆ brakującą
    kolumnę, a nie skasować tę, która już jest."""
    from pathlib import Path

    zrodlo = Path("services/ingest/pozycjonowanie.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (instrument_id, event_time) DO UPDATE" in zrodlo
    assert "coalesce(EXCLUDED.long_ratio" in zrodlo
    assert "coalesce(EXCLUDED.open_interest" in zrodlo


def test_brak_perpetuala_to_ZERO_cech_a_nie_wyjatek():
    """Instrument bez odwzorowania na perpetual (np. nowy spot) nie może
    wywracać liczenia cech dla wszystkich pozostałych."""
    h = get_horizon("4h")
    kursor = KursorAtrapa([], src=None)
    assert features.compute_positioning(kursor, 7, h, {}, CHWILA - timedelta(days=5),
                                        CHWILA) == 0


# ============================================ wpiecie w harmonogram

def test_zbieranie_JEST_wpiete_w_cykliczny_proces():
    """Mechanizm, którego nic nie woła, to w tym projekcie znany wzorzec awarii:
    zaprojektowane, okolumnowane, niewpięte. Zdarzyło się to wykrywaniu
    delistingu i wysyłce zdarzenia `learning.drift_high` — oba istniały
    miesiącami i nie zrobiły nic ani razu."""
    from pathlib import Path

    worker = Path("services/ingest/history_worker.py").read_text(encoding="utf-8")
    assert "import pozycjonowanie" in worker
    assert "tlum.przebieg" in worker


def test_awaria_pozycjonowania_NIE_KASUJE_swiec_ani_stawek():
    """Trzy zadania w jednym cyklu. Wspólny `try` sprawiłby, że nowe źródło
    danych potrafi wyzerować dwa działające od dawna."""
    from pathlib import Path

    worker = Path("services/ingest/history_worker.py").read_text(encoding="utf-8")
    fragment = worker[worker.index("ile_swiec = ile_stawek"):worker.index("# PULS TUTAJ")]
    assert fragment.count("try:") >= 3, (
        "każde źródło musi mieć własny `try` — inaczej awaria jednego kasuje "
        "dorobek pozostałych")
    assert fragment.count("conn.rollback()") >= 3


def test_pozycjonowanie_NIE_DOTYKA_decyzji_ani_ryzyka():
    """To jest zbieranie danych, nie strategia. Dopóki trener nie pokaże
    przewagi out-of-sample, ten moduł nie ma prawa wpłynąć na żadne zlecenie —
    a test jest tu po to, żeby „na razie tylko zbieramy" nie zmieniło się
    po cichu w „już handlujemy"."""
    from pathlib import Path

    zrodlo = Path("services/ingest/pozycjonowanie.py").read_text(encoding="utf-8")
    for zakazane in ("exec.", "risk.", "journal.planned_order", "zloz_plan",
                     "killswitch"):
        assert zakazane not in zrodlo, (
            f"kolektor danych sięga po {zakazane} — to już nie jest zbieranie")


def test_KAZDY_uruchamialny_skrypt_ma_korzen_na_sciezce_importu():
    """Uruchomienie `python services/ingest/cos.py` ustawia `sys.path[0]` na
    katalog SKRYPTU, nie na korzeń repozytorium — więc `import services.*`
    kończy się `ModuleNotFoundError`.

    Testy tego nie łapią z definicji: pytest startuje z korzenia, więc import
    działa. Awaria pojawia się dopiero przy uruchomieniu — i tak właśnie padło
    zadanie backfillu na produkcji 2026-09-08, po przejściu wszystkich testów.

    Ten test sprawdza WSZYSTKIE skrypty uruchamialne, a nie tylko ten jeden:
    pułapka jest cechą sposobu uruchamiania, nie konkretnego pliku.
    """
    import ast
    from pathlib import Path

    korzen = Path(__file__).resolve().parents[1]
    winne = []
    for plik in sorted((korzen / "services").rglob("*.py")):
        zrodlo = plik.read_text(encoding="utf-8")
        drzewo = ast.parse(zrodlo)
        uruchamialny = any(
            isinstance(w, ast.If) and "__main__" in ast.dump(w.test)
            for w in drzewo.body)
        if not uruchamialny:
            continue
        importuje_services = any(
            isinstance(w, ast.ImportFrom) and (w.module or "").startswith("services")
            for w in ast.walk(drzewo))
        if importuje_services and "sys.path.insert" not in zrodlo:
            winne.append(str(plik.relative_to(korzen)))

    assert not winne, (
        "skrypty uruchamialne bez korzenia na sys.path — padną przy "
        "`python <plik>`: " + ", ".join(winne))


# ============================== sygnal istnieje, ale NIE HANDLUJE

def test_sygnal_pozycjonowania_NIE_WCHODZI_do_decyzji_bez_wlaczenia():
    """Dopisanie sygnału do `SIGNALS` wystarczyłoby, żeby przy najbliższej
    kalibracji dostał wagę z IC i zaczął wpływać na produkcję — bez niczyjej
    decyzji. To jest dokładnie ten sposób, w jaki niesprawdzony pomysł wchodzi
    do systemu po cichu.

    Właściciel wybrał „zbierz i ZMIERZ, nie handluj". Ten test jest tego
    wyborem zapisanym w kodzie.
    """
    import os

    from services.engine.horizons import get as get_h
    from services.engine.signals import EKSPERYMENTALNE, precompute

    cechy = {"long_ratio_z": 2.4, "oi_zmiana": 6.0}
    poprzednie = os.environ.pop("SYGNALY_EKSPERYMENTALNE", None)
    try:
        assert "pozycjonowanie_tlumu" in EKSPERYMENTALNE
        assert "pozycjonowanie_tlumu" not in precompute(cechy, get_h("4h"))
    finally:
        if poprzednie is not None:
            os.environ["SYGNALY_EKSPERYMENTALNE"] = poprzednie


def test_sygnal_DA_SIE_wlaczyc_na_czas_pomiaru(monkeypatch):
    """Druga granica: bramka, której nie da się otworzyć, uniemożliwiałaby
    pomiar — czyli zamieniłaby „jeszcze nie zmierzone" w „nigdy nie zmierzone"."""
    from services.engine.horizons import get as get_h
    from services.engine.signals import precompute

    monkeypatch.setenv("SYGNALY_EKSPERYMENTALNE", "")
    wynik = precompute({"long_ratio_z": 2.4, "oi_zmiana": 6.0}, get_h("4h"))
    assert "pozycjonowanie_tlumu" in wynik
    assert wynik["pozycjonowanie_tlumu"].score < 0, (
        "przy tłumie wychylonym na długo sygnał ma być KONTRARIAŃSKI")


def test_sygnal_eksperymentalny_NIE_MA_wagi_domyslnej():
    """Gdyby miał, wystarczyłoby go włączyć, żeby handlował z wagą, której nikt
    nie zmierzył."""
    from services.engine.signals import DEFAULT_WEIGHTS, EKSPERYMENTALNE

    for rezim, wagi in DEFAULT_WEIGHTS.items():
        assert not (EKSPERYMENTALNE & set(wagi)), (
            f"reżim {rezim} nadaje wagę sygnałowi eksperymentalnemu")


def test_sygnal_MILCZY_gdy_wychylenie_nie_jest_swieze():
    """Sam skrajny udział długich może być trwałym stanem rynku w hossie — to
    opis, nie sygnał. Dopiero rosnące otwarte pozycje mówią, że wychylenie jest
    świeże i lewarowane."""
    from services.engine.horizons import get as get_h
    from services.engine.signals import pozycjonowanie_tlumu

    h = get_h("4h")
    assert pozycjonowanie_tlumu({"long_ratio_z": 2.4, "oi_zmiana": -3.0}, h).score == 0.0
    assert pozycjonowanie_tlumu({"long_ratio_z": 0.3, "oi_zmiana": 9.0}, h).score == 0.0
    assert pozycjonowanie_tlumu({"long_ratio_z": 2.4, "oi_zmiana": 6.0}, h).score < 0
