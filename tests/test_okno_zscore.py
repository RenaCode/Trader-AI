"""Okno z-score cech pochodnych liczy się z HISTORII, nie z długości przebiegu.

`funding_z` i `long_ratio_z` deklarują w swoim opisie okno 30 dni. Liczone były
natomiast wyłącznie ze świec objętych BIEŻĄCYM przebiegiem — a cykliczny worker
liczy okno jednej doby. Na świecy 4-godzinnej to sześć punktów siatki, czyli
mniej niż próg dwudziestu obserwacji: cecha nie powstawała ani razu.

Awaria nie miała żadnego objawu. Wiersze `funding_z__4h` były w bazie (dopisywał
je ręczny `--backfill`, wstecz, z datowaniem `znane_od`), więc wyglądały na
znane o czasie. Silnik czytał je przez `feat.as_of()` już PO decyzji, a w samej
chwili decyzji `f.get("funding_z")` zwracało None i `funding_carry` — jedyny
sygnał niepochodzący z ceny — po cichu wypadał z każdej decyzji.

Te testy odtwarzają dokładnie okno produkcyjne (jedna doba, świeca 4 h) i
pilnują dwóch rzeczy naraz: że cecha w ogóle powstaje ORAZ że jej wartość
pochodzi z zadeklarowanych 30 dni, a nie z ostatnich kilku świec.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.engine.horizons import get as get_horizon
from services.ingest import features

TERAZ = datetime(2026, 9, 8, 16, tzinfo=timezone.utc)

# Okno cyklicznego workera: `features.py` bez --backfill liczy `now - 1 dzień`.
DOBA = timedelta(days=1)


class KursorHistorii:
    """Atrapa kursora, która RESPEKTUJE zakres czasu z zapytania.

    To jest tu istotne, a nie ozdobne: gdyby atrapa oddawała całą historię
    niezależnie od parametrów, test przechodziłby także wtedy, gdy kod pyta
    o dwie doby wstecz. Sprawdzamy zachowanie, którego brak był całą wadą —
    czy przebieg SIĘGA po rozgrzewkę okna.
    """

    def __init__(self, wiersze: list[dict], src: int = 99):
        self._wiersze = wiersze
        self._src = src
        self._meta = False
        self._wynik: list[dict] = []

    def execute(self, sql, params=None):
        if "meta->>'funding_from'" in sql:
            self._meta = True
            return
        self._meta = False
        _src, od, do = params[0], params[1], params[2]
        self._wynik = [w for w in self._wiersze if od <= w["event_time"] <= do]

    def fetchone(self):
        return {"src": self._src} if self._meta else None

    def fetchall(self):
        return self._wynik


def przechwyc(fn, kursor, ids, *, dni: float, bar: str = "4h") -> dict[str, list]:
    """Uruchamia liczenie cech na oknie `dni` i zwraca {rola: [(czas, wartość)]}."""
    h = get_horizon(bar)
    zapisane: list[tuple] = []
    pierwotne = features.insert_values
    features.insert_values = lambda cur, rows: (zapisane.extend(rows), len(rows))[1]
    try:
        fn(kursor, 7, h, ids, TERAZ - timedelta(days=dni), TERAZ)
    finally:
        features.insert_values = pierwotne

    nazwy = {v: k for k, v in ids.items()}
    out: dict[str, list] = {k: [] for k in ids}
    for fid, _inst, t, wartosc, _znane in zapisane:
        out[nazwy[fid]].append((t, wartosc))
    return out


def stawki_z_wyskokiem() -> list[dict]:
    """40 dni stawek co 8 h: spokojne wahanie, a w ostatniej dobie skok.

    Rozkład jest dobrany tak, żeby ODRÓŻNIĆ dwa okna, a nie tylko sprawdzić, czy
    cokolwiek powstało. W oknie 30-dniowym skok to ponad dwa odchylenia; w oknie
    samej ostatniej doby wszystkie wartości są IDENTYCZNE, więc odchylenie jest
    zerowe i z-score nie da się policzyć w ogóle.
    """
    wiersze = []
    t = TERAZ - timedelta(days=40)
    k = 0
    while t <= TERAZ:
        spokoj = t < TERAZ - DOBA - timedelta(hours=8)
        rate = (0.0003 if k % 2 == 0 else 0.0001) if spokoj else 0.0009
        wiersze.append({"event_time": t, "rate": rate})
        t += timedelta(hours=8)
        k += 1
    return wiersze


def pozycje_z_wyskokiem() -> list[dict]:
    """To samo dla udziału kont długich; odczyty godzinowe."""
    wiersze = []
    t = TERAZ - timedelta(days=40)
    k = 0
    while t <= TERAZ:
        spokoj = t < TERAZ - DOBA - timedelta(hours=1)
        udzial = (0.53 if k % 2 == 0 else 0.51) if spokoj else 0.62
        wiersze.append({"event_time": t, "long_ratio": udzial, "open_interest": 100.0})
        t += timedelta(hours=1)
        k += 1
    return wiersze


# ============================================ cecha w ogóle powstaje

def test_funding_z_powstaje_w_CYKLICZNYM_oknie_jednej_doby():
    """Sedno wady: sześć świec 4 h w dobie to mniej niż próg dwudziestu próbek.

    Bez rozgrzewki okna z-score `funding_z` nie powstawał NIGDY w produkcji —
    a mimo to leżał w bazie, dopisany wstecz ręcznym backfillem.
    """
    wynik = przechwyc(features.compute_funding,
                      KursorHistorii(stawki_z_wyskokiem()),
                      {"funding_rate": 1, "funding_z": 2}, dni=1)
    assert wynik["funding_rate"], "brak nawet stawki — atrapa nie oddała danych"
    assert wynik["funding_z"], (
        "cykliczny przebieg (okno 1 doby, świeca 4h) nie policzył funding_z: "
        f"stawka ma {len(wynik['funding_rate'])} świec, z-score zero. "
        "Okno z-score liczy się z długości przebiegu zamiast z historii")
    assert len(wynik["funding_z"]) == len(wynik["funding_rate"]), (
        "z-score powstał tylko dla części świec — rozgrzewka jest za krótka")


def test_long_ratio_z_powstaje_w_CYKLICZNYM_oknie_jednej_doby():
    """Ta sama wada w `compute_positioning` — i z tego samego powodu."""
    wynik = przechwyc(features.compute_positioning,
                      KursorHistorii(pozycje_z_wyskokiem()),
                      {"long_ratio": 1, "long_ratio_z": 2, "oi_zmiana": 3}, dni=1)
    assert wynik["long_ratio"], "brak nawet udziału kont — atrapa nie oddała danych"
    assert wynik["long_ratio_z"], (
        "cykliczny przebieg (okno 1 doby, świeca 4h) nie policzył long_ratio_z")
    assert len(wynik["long_ratio_z"]) == len(wynik["long_ratio"])


# ============================================ okno ma DŁUGOŚĆ, którą deklaruje

def test_zscore_mierzy_odchylenie_od_30_DNI_a_nie_od_przebiegu():
    """Wartość, nie tylko obecność.

    W ostatniej dobie wszystkie stawki są równe. Gdyby okno kończyło się na
    przebiegu, odchylenie byłoby zerowe — cecha albo by nie powstała, albo
    wyszłaby zerem. Odniesiona do 30 dni skok jest wyraźny.
    """
    wynik = przechwyc(features.compute_funding,
                      KursorHistorii(stawki_z_wyskokiem()),
                      {"funding_rate": 1, "funding_z": 2}, dni=1)
    z = [w for _t, w in wynik["funding_z"]]
    assert z and min(z) > 2.5, (
        f"z-score {z} — skok o rząd wielkości ponad 30-dniowe wahanie musi dać "
        f"kilka odchyleń; wartość bliska zeru znaczy, że okno objęło wyłącznie "
        f"samą podwyżkę")


def test_okno_krotsze_niz_prog_daje_TE_SAME_wartosci_co_dluzsze():
    """Ta sama świeca policzona w cyklu i w backfillu ma dać JEDNĄ wartość.

    Cecha jest deterministyczną funkcją historii, więc nie może zależeć od tego,
    jak długie okno akurat liczył proces. Rozjazd tutaj to rozjazd
    trening–produkcja: trener liczy backfillem, silnik czyta to, co dopisał cykl.
    """
    ids = {"funding_rate": 1, "funding_z": 2}
    krotkie = przechwyc(features.compute_funding, KursorHistorii(stawki_z_wyskokiem()),
                        ids, dni=1)
    dlugie = przechwyc(features.compute_funding, KursorHistorii(stawki_z_wyskokiem()),
                       ids, dni=10)
    wspolne = dict(krotkie["funding_z"])
    assert wspolne, "krótki przebieg nie policzył niczego"
    for t, w in dlugie["funding_z"]:
        if t in wspolne:
            assert abs(w - wspolne[t]) < 1e-9, (
                f"świeca {t}: backfill dał {w}, cykl {wspolne[t]} — ta sama "
                f"chwila ma dwie wartości zależne od długości przebiegu")


# ============================================ dlaczego akurat jedna doba

def test_cykliczny_worker_NIE_ma_backfillu_wiec_okno_to_JEDNA_doba():
    """Uzasadnienie okna z testów wyżej, wzięte z produkcji, nie z założenia.

    `features.py` bez `--backfill` liczy `now - 1 dzień` (patrz `main`), a chart
    uruchamia go właśnie tak. Gdyby to się zmieniło, powyższe testy mierzyłyby
    okno, którego produkcja nie używa.
    """
    chart = Path("charts/trader/values.yaml").read_text(encoding="utf-8")
    linia = [l for l in chart.splitlines() if "services/ingest/features.py" in l]
    assert linia, "nie ma polecenia workera cech w charcie"
    assert "--backfill" not in linia[0], (
        "worker cech dostał --backfill — okno przestało być dobowe, "
        "przejrzyj założenia testów okna z-score")

    zrodlo = Path("services/ingest/features.py").read_text(encoding="utf-8")
    assert "days=args.days if args.backfill else 1" in zrodlo, (
        "zmieniło się okno cyklicznego przebiegu — testy wyżej zakładają dobę")
