"""Funding ma limit wieku - stara stawka NIE trafia do cechy (audyt S-8).

`compute_funding` przenosil ostatnia znana stawke na kazda kolejna swiece bez
wzgledu na to, ile ma godzin. Gdy pobieranie fundingu stanie, `funding_rate`
i `funding_z` dalej wygladaja na swieze, a `funding_carry` gra na stawce sprzed
dni. Brak wartosci jest tu poprawnym wynikiem: brak widac (pokrycie wag
w petli, czujka swiezosci), stara liczby udajacej swieza - nie.
"""
from datetime import datetime, timedelta, timezone

from services.engine.horizons import get as get_horizon
from services.ingest import features

TERAZ = datetime(2026, 9, 8, 16, tzinfo=timezone.utc)
IDS = {"funding_rate": 1, "funding_z": 2}


class Kursor:
    """Respektuje zakres czasu z zapytania - jak `test_okno_zscore.KursorHistorii`."""

    def __init__(self, wiersze):
        self._wiersze, self._meta, self._wynik = wiersze, False, []

    def execute(self, sql, params=None):
        self._meta = "meta->>'funding_from'" in sql
        if not self._meta:
            _src, od, do = params
            self._wynik = [w for w in self._wiersze if od <= w["event_time"] <= do]

    def fetchone(self):
        return {"src": 99} if self._meta else None

    def fetchall(self):
        return self._wynik


def stawki(do: datetime) -> list[dict]:
    """40 dni stawek co 8 h, z wahaniem (zeby z-score dalo sie policzyc), az do `do`."""
    out, t, k = [], TERAZ - timedelta(days=40), 0
    while t <= do:
        out.append({"event_time": t, "rate": 0.0003 if k % 2 else 0.0001})
        t, k = t + timedelta(hours=8), k + 1
    return out


def policz(wiersze, *, dni=1, bar="4h"):
    zapisane = []
    pierwotne = features.insert_values
    features.insert_values = lambda cur, rows: (zapisane.extend(rows), len(rows))[1]
    try:
        features.compute_funding(Kursor(wiersze), 7, get_horizon(bar), IDS,
                                 TERAZ - timedelta(days=dni), TERAZ)
    finally:
        features.insert_values = pierwotne
    return {nazwa: [t for fid, _i, t, _w, _z in zapisane if fid == f]
            for nazwa, f in IDS.items()}


def test_STOJACE_pobieranie_fundingu_NIE_daje_cechy_tylko_UWAGE_w_logu(capsys):
    """Sedno S-8. Ostatnia stawka sprzed trzech dni: do dzis kazda swieca
    dostawala ja jako swieza `funding_rate` i `funding_z`."""
    wynik = policz(stawki(TERAZ - timedelta(days=3)))
    assert wynik["funding_rate"] == [], (
        f"stawka sprzed 3 dni przeniesiona na {len(wynik['funding_rate'])} swiec")
    assert wynik["funding_z"] == []
    log = capsys.readouterr().out
    assert "PRZETERMINOWANY" in log and "limit 16 h" in log, (
        f"brak cechy bez linii w logu wyglada jak spokojny cykl: {log!r}")


def test_swieze_stawki_daja_ceche_na_KAZDEJ_swiecy(capsys):
    """Kanarek: przy zywym pobieraniu limit nie zabiera ani jednej swiecy."""
    wynik = policz(stawki(TERAZ))
    assert len(wynik["funding_rate"]) == 6           # doba swiec 4h
    assert len(wynik["funding_z"]) == 6
    assert "PRZETERMINOWANY" not in capsys.readouterr().out


def test_granica_wieku_odcina_swiece_ponad_16_h():
    """Ostatnia stawka 08:00 dnia poprzedniego. Swieca 1h z 20:00 (12 h) ja
    dostaje, z 04:00 (20 h) juz nie."""
    ostatnia = TERAZ - timedelta(hours=32)            # 2026-09-07 08:00
    wynik = policz(stawki(ostatnia), dni=2, bar="1h")
    zapisane = set(wynik["funding_rate"])
    assert ostatnia + timedelta(hours=12) in zapisane
    assert ostatnia + timedelta(hours=16) in zapisane
    assert ostatnia + timedelta(hours=17) not in zapisane
    assert ostatnia + timedelta(hours=20) not in zapisane


# ============================================ czujka swiezosci widzi funding

def test_czujka_swiezosci_ZGLASZA_funding_starszy_niz_limit_cechy():
    """Cecha znika po 16 h - czujka ma powiedziec dlaczego, tym samym progiem.
    Prog swiec (3 x 480 min = 24 h) zostawilby 8 h ciszy przy juz pustej cesze."""
    from services.engine.dozor import ocen_swiezosc

    limit = features.FUNDING_WIEK_MAX.total_seconds() / 60
    wiersz = {"co": "crypto: market.funding_rate", "minut_swiecy": 480,
              "prog_min": limit}
    assert ocen_swiezosc([{**wiersz, "wiek_min": limit - 30}]) == []
    (zrodlo, tytul, tresc), = ocen_swiezosc([{**wiersz, "wiek_min": limit + 60}])
    assert zrodlo == "crypto: market.funding_rate"
    assert "nieświeże" in tytul and "limit wieku" in tresc


def test_czujka_swiezosci_PYTA_o_funding_z_limitem_cechy():
    """Zapytanie czujki niesie funding i prog rowny `FUNDING_WIEK_MAX`."""
    import asyncio

    from services.engine import dozor

    zapytania = []

    async def fetch_all(sql, params=None):
        if "risk.engine_heartbeat" in sql:
            return [{"poza": False}]
        zapytania.append((" ".join(sql.split()), params))
        return []

    async def notify(*a, **k):
        raise AssertionError("bez wierszy nie ma alarmu")

    asyncio.run(dozor.sprawdz_swiezosc(fetch_all, notify, sucho=True, bar="4h"))
    sql, params = zapytania[0]
    assert "market.funding_rate" in sql and "funding_from" in sql
    assert params["prog_funding"] == 16 * 60
