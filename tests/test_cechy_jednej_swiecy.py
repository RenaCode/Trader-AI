"""Decyzja dla świecy T zapada na cechach świecy T — nie T−1 i nie na mieszance.

DLACZEGO TO JEST WŁASNY PLIK

Trener skleja parę cena–cechy SZTYWNO: `train.load_panel` robi
`FROM f JOIN p ON p.t = f.event_time`, więc jedna obserwacja ucząca to JEDNA
świeca i komplet cech policzony z niej samej. Produkcja czytała przez
`feat.as_of`, a ta funkcja robi `DISTINCT ON (feature_def_id) ORDER BY
event_time DESC` (migracja 0004) — czyli każda cecha bierze swoją najświeższą
wartość NIEZALEŻNIE od pozostałych.

Przy progu `max_staleness_min = 600` dla świecy 4-godzinnej (dwie i pół świecy)
dawało to dwa rozjazdy naraz, oba bez jednego błędu w logu:

  * wektor o jedną świecę za stary — potok cech dowozi świecę 08:00–12:00
    dopiero 12:20–12:35, a cykl decyzyjny wypada 12:00–12:05;
  * wektor ZŁOŻONY Z RÓŻNYCH ŚWIEC — cecha szybka z T−1, wolna z T−2.

Żaden licznik tego nie widział: `MIN_CECH` było spełnione (cech jest komplet,
tyle że nie tych), a próg 600 minut nie migał ani razu.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services.engine.horizons import H4H, HORIZONS
from services.engine.loop import MIN_CECH, current_bar, latest_features, okno_decyzji

SWIECA = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)   # 08:00–12:00
ROLE = ("rsi", "atr_pct", "ret_short", "ret_mid", "ema_spread", "funding_z")


class KursorCech:
    """Atrapa `feat.as_of` odtwarzająca JEJ SEMANTYKĘ, nie jej wynik.

    `DISTINCT ON (feature_def_id) ... ORDER BY event_time DESC` plus oba filtry
    czasu. Atrapa oddająca gotową listę byłaby ślepa dokładnie na to, o co tu
    chodzi: na to, że każda cecha wybiera świecę OSOBNO.
    """

    def __init__(self, wiersze: list[tuple[str, datetime, float]]):
        # wiersze: [(rola, event_time, value)]
        self.wiersze = wiersze
        self._wynik: list[dict] = []

    def execute(self, sql, params=None):
        assert "feat.as_of" in sql, f"test dotyczy wyłącznie odczytu cech: {sql}"
        _iid, as_of, _ids, staleness = params
        minuty = int(staleness.split()[0])
        od = as_of - timedelta(minutes=minuty)
        najswiezsze: dict[str, tuple[datetime, float]] = {}
        for rola, t, v in self.wiersze:
            if not (od <= t <= as_of):
                continue
            if rola not in najswiezsze or t > najswiezsze[rola][0]:
                najswiezsze[rola] = (t, v)
        self._wynik = [{"feature_name": f"{rola}__4h@1", "value": v, "event_time": t}
                       for rola, (t, v) in sorted(najswiezsze.items())]
        return self

    def fetchall(self):
        return self._wynik


def czytaj(wiersze, teraz):
    return latest_features(KursorCech(wiersze), 1, teraz, H4H, [1, 2, 3], SWIECA)


# ------------------------------------------------ mieszanka świec w jednym wektorze

def test_wektor_NIE_MIESZA_swiec():
    """KANAREK. Trzy cechy policzone dla świecy 08:00, trzy zaległe z 04:00.

    Przed poprawką `feat.as_of` oddawało wszystkie sześć i szły do `evaluate()`
    jako jeden wektor — kształt, którego trener nie widział ani razu, bo jego
    złączenie po `event_time` takiej pary nie dopuszcza.
    """
    poprzednia = SWIECA - timedelta(minutes=H4H.bar_minutes)
    wiersze = ([(r, SWIECA, 0.5) for r in ROLE[:3]]
               + [(r, poprzednia, 0.9) for r in ROLE[3:]])
    cechy, obce = czytaj(wiersze, SWIECA + timedelta(hours=4, minutes=25))

    assert set(cechy) == set(ROLE[:3]), (
        f"do decyzji weszły cechy z więcej niż jednej świecy: {sorted(cechy)}")
    assert set(obce) == set(ROLE[3:])
    assert all(t == SWIECA for t in
               [SWIECA]), "kotwica testu"


def test_komplet_cech_POPRZEDNIEJ_swiecy_to_ZA_MALO_CECH():
    """Stan, w którym silnik decydował: cech jest komplet, tylko nie tej świecy.

    `MIN_CECH` było spełnione, więc pominięcie nigdy nie zachodziło i decyzja
    zapadała na wektorze o jedną świecę za starym. Po poprawce ten sam wejściowy
    stan bazy daje ZERO cech świecy bieżącej — czyli ścieżkę pominięcia,
    zostawiającą wiersz w `journal.pominiecie`.
    """
    poprzednia = SWIECA - timedelta(minutes=H4H.bar_minutes)
    wiersze = [(r, poprzednia, 0.5) for r in ROLE]
    cechy, obce = czytaj(wiersze, SWIECA + timedelta(hours=4, minutes=2))

    assert cechy == {}, f"decyzja dostała cechy świecy {poprzednia:%H:%M}: {cechy}"
    assert len(cechy) < MIN_CECH
    assert len(obce) == len(ROLE), (
        "spóźnione cechy muszą zostać ZAUWAŻONE — inaczej „potok spóźniony” "
        "zapisze się jako „żadnej cechy”, czyli jak martwy worker")


def test_komplet_cech_TEJ_swiecy_przechodzi():
    """Kanarek odwrotny. Gdyby poprawka po prostu odcinała cechy, wszystkie
    testy wyżej przechodziłyby razem z silnikiem, który nie handluje nigdy."""
    wiersze = [(r, SWIECA, 0.5) for r in ROLE]
    cechy, obce = czytaj(wiersze, SWIECA + timedelta(hours=4, minutes=25))
    assert set(cechy) == set(ROLE) and obce == {}
    assert len(cechy) >= MIN_CECH


def test_cecha_ZE_SWIECY_OTWARTEJ_tez_nie_wchodzi():
    """Filtr jest równością, nie „nie starsze niż". Wartość policzona dla świecy
    jeszcze trwającej (12:00–16:00) opisuje niepełne dane — `features.py`
    świadomie ją pomija, ale wyzwanie jest to samo co przy cesze zaległej."""
    otwarta = SWIECA + timedelta(minutes=H4H.bar_minutes)
    wiersze = [(r, SWIECA, 0.5) for r in ROLE[:4]] + [(ROLE[4], otwarta, 0.9)]
    cechy, obce = czytaj(wiersze, otwarta + timedelta(minutes=25))
    assert ROLE[4] not in cechy and obce == {ROLE[4]: otwarta}


# --------------------------------------- odłożenie decyzji, a nie jej utrata

def test_spozniony_potok_ODKLADA_decyzje_w_TYM_SAMYM_oknie():
    """Rozstrzygnięcie kwestii „czy odkładanie zatrzyma handel".

    Harmonogram zmierzony na produkcji: świeca 08:00–12:00, cykl decyzyjny
    12:00–12:05, cecha dostępna 12:20–12:35 (history_worker co 1200 s, features
    co 900 s, świeca otwarta pomijana). Odłożenie kosztuje 20–35 minut, a nie
    transakcję: `current_bar` oddaje tę samą świecę przez CAŁE następne okno,
    a `decided_for_bar` blokuje dopiero po zapadnięciu decyzji.
    """
    for minuta in (0, 20, 35, 239):
        teraz = SWIECA + timedelta(minutes=H4H.bar_minutes + minuta)
        assert current_bar(teraz, H4H) == SWIECA, (
            f"o {teraz:%H:%M} pytanie dotyczy już innej świecy — odłożona "
            f"decyzja dla {SWIECA:%H:%M} nigdy by nie zapadła")
        assert okno_decyzji(SWIECA, H4H) <= teraz


def test_opoznienie_ponad_swiece_to_juz_UTRATA_swiecy():
    """Druga strona tego samego rozstrzygnięcia — i powód, dla którego ta gałąź
    musi zostawiać licznik. Potok spóźniony o więcej niż świecę sprawia, że
    ŻADEN cykl w oknie nie zobaczy kompletu, a silnik przestaje handlować
    całkowicie — wyglądając przy tym jak silnik bez sygnałów."""
    teraz = SWIECA + timedelta(minutes=2 * H4H.bar_minutes + 10)
    assert current_bar(teraz, H4H) != SWIECA


# ------------------------------------------- dlaczego próg staleness nie migał

@pytest.mark.parametrize("bar", sorted(HORIZONS))
def test_prog_staleness_przepuszczal_CALA_poprzednia_swiece(bar):
    """Miara, która nie mogła zareagować. `max_staleness_min` jest z definicji
    dłuższy niż świeca (pilnuje tego `test_dozor`), więc komplet cech świecy
    T−1 mieścił się w nim ZAWSZE. Próg chroni przed martwym potokiem i taki
    zostaje — nie jest natomiast i nie może być odpowiedzią na pytanie „czy to
    jest wektor TEJ świecy"."""
    h = HORIZONS[bar]
    assert h.max_staleness_min > h.bar_minutes, (
        f"{bar}: gdyby próg był krótszy od świecy, sam by tu wystarczył")
