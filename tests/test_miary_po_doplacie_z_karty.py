"""Miary okien, bramka statusu i kup-i-trzymaj PO TEJ SAMEJ DOPŁACIE Z KARTY CO DSR.

NAZWA PLIKU MÓWI „DOPŁATA Z KARTY", NIE „PEŁNA KARTA" - i to jest treść, nie
kosmetyka. Przy `--limit` niewypełnienie stoi w karcie BEZ wartości, więc do
dopłaty nie wchodzi; „po pełnej karcie" twierdziłoby komplet, którego nie ma
(plik nazywał się tak do 2026-10-03).

CO BYŁO ŹLE (do 2026-10-02)

`Karta.doplata_poza_symulacja` - dziś spread, przy limicie także niewypełnienie
- odejmował WYŁĄCZNIE `werdykt_dsr`. Odsetek dodatnich okien, mediana Sharpe'a
OOS i porównanie z kup-i-trzymaj liczyły się na zwrotach sprzed niej, a to są
trzy z czterech warunków `status_walidacji`. Bramka składała więc werdykt
z miar dwóch różnych światów, i trzy z nich opisywały świat tańszy.

CO PILNUJĄ TE TESTY

  1. Jedna definicja dopłaty na transakcję (`koszty.zwroty_po_doplacie`)
     i jej przeniesienie na wynik okna - zwroty ORAZ krzywą kapitału
     (`portfolio.wynik_po_doplacie`).
  2. Kup-i-trzymaj płaci tę samą dopłatę za swoją JEDNĄ rundę - symetria
     porównania.
  3. `train()` od wejścia do wydruku: przy dopłacie zabijającej przewagę
     odsetek dodatnich okien i mediana MUSZĄ spaść do zera i poniżej, a liczby
     brutto zostać obok, jawnie nazwane. Na starym kodzie ten test pada, bo
     wydruk mówił „dodatnie: N" z liczbą sprzed spreadu.

NIC TU NIE DOTYKA BAZY: kursor jest atrapą, panel jest syntetyczny.
"""
from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest

from services.engine import train as trener
from services.engine.horizons import get as get_horizon
from services.engine.koszty import karta_rundy, zwroty_po_doplacie
from services.engine.portfolio import (
    Egzekucja, PortfolioResult, kup_i_trzymaj, metrics, wynik_po_doplacie,
)
from services.engine.risk_rules import START_EQUITY

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
H5M = get_horizon("5m")


# ------------------------------------------------- 1. jedna definicja dopłaty

def test_doplata_odejmowana_od_KAZDEJ_transakcji_raz():
    assert zwroty_po_doplacie([0.01, -0.02, 0.0], 0.001) == pytest.approx(
        [0.009, -0.021, -0.001])
    assert zwroty_po_doplacie([], 0.001) == []


def _wynik_dwoch_transakcji() -> PortfolioResult:
    """Dwie transakcje, krzywa co godzinę. Ruchu ceny nie ma - liczy się koszt."""
    t = [T0 + timedelta(hours=i) for i in range(6)]
    trades = [
        {"ret": 0.004, "opened_at": t[0], "closed_at": t[2], "notional": 10_000.0,
         "symbol": "AAA", "conf": 0.5, "side": 1},
        {"ret": 0.002, "opened_at": t[2], "closed_at": t[5], "notional": 20_000.0,
         "symbol": "BBB", "conf": 0.5, "side": 1},
    ]
    curve = [(x, START_EQUITY + 40.0 * (i >= 2) + 40.0 * (i >= 5))
             for i, x in enumerate(t)]
    return PortfolioResult(trades=trades, equity_curve=curve, avg_exposure=15_000.0)


def test_wynik_po_doplacie_przesuwa_zwroty_I_krzywa_kapitalu():
    """Zwrot z konta MUSI zmaleć o dopłatę razy notional - inaczej porównanie
    z kup-i-trzymaj (liczone na `total_return`) zostałoby przed kosztem, choć
    Sharpe okna już po nim."""
    res = _wynik_dwoch_transakcji()
    po = wynik_po_doplacie(res, 0.0010)
    assert [t["ret"] for t in po.trades] == pytest.approx([0.003, 0.001])
    koszt = 0.0010 * (10_000 + 20_000)
    assert po.equity_curve[-1][1] == pytest.approx(res.equity_curve[-1][1] - koszt)
    m_brutto, m_po = metrics(res, 1), metrics(po, 1)
    assert m_po["total_return"] == pytest.approx(
        m_brutto["total_return"] - koszt / START_EQUITY * 100, abs=1e-4)
    assert m_po["sharpe"] < m_brutto["sharpe"]
    # Ekspozycja to pozycje, a dopłata nie zmienia ani jednej.
    assert m_po["avg_exposure"] == m_brutto["avg_exposure"]
    # Wejście obciąża krzywą PO świecy decyzji, nie w niej.
    assert po.equity_curve[0][1] == res.equity_curve[0][1]
    assert po.equity_curve[1][1] == pytest.approx(res.equity_curve[1][1] - 5.0)


def test_doplata_zero_to_ten_sam_wynik_co_do_bitu():
    res = _wynik_dwoch_transakcji()
    assert wynik_po_doplacie(res, 0.0) is res


def test_transakcja_bez_notionalu_przy_doplacie_PRZERYWA():
    res = _wynik_dwoch_transakcji()
    del res.trades[0]["notional"]
    with pytest.raises(ValueError, match="notional"):
        wynik_po_doplacie(res, 0.0005)


# ------------------------------------------------- 2. symetria kup-i-trzymaj

def _panel_bh(cena_od: float, cena_do: float, n: int = 10) -> list[dict]:
    return [{"t": T0 + timedelta(hours=i), "f": {},
             "price": cena_od + (cena_do - cena_od) * i / (n - 1),
             "high": 0.0, "low": 0.0} for i in range(n)]


def test_kup_i_trzymaj_placi_JEDNA_doplate_rundy_na_instrument():
    panele = {"AAA": _panel_bh(100, 110), "BBB": _panel_bh(50, 45)}
    brutto = kup_i_trzymaj(panele, "spot")
    po = kup_i_trzymaj(panele, "spot", doplata=0.000585)
    # Wynik jest w procentach i jest średnią po instrumentach - jedna runda
    # na każdy, więc różnica to dokładnie dopłata wyrażona w punktach proc.
    assert brutto - po == pytest.approx(0.0585)


def test_doplata_odniesienia_z_karty_rynkowej_rowna_strategii_przy_rynkowej():
    """Przy egzekucji rynkowej strategia i koszyk płacą TEN SAM składnik spoza
    symulacji - spread. Przy limicie karta strategii ma niewypełnienie, którego
    koszyk kupiony po rynku nie ponosi; spread - tak samo."""
    rynkowa = karta_rundy("spot", False, spread_bps=5.85)
    limitowa = karta_rundy("spot", True, spread_bps=5.85, niewypelnienie=0.0003)
    assert rynkowa.doplata_poza_symulacja == pytest.approx(5.85e-4)
    assert limitowa.doplata_poza_symulacja == pytest.approx(5.85e-4 + 0.0003)


# ------------------------------------------------- 3. train() od wejścia do wydruku

def _cechy(kup: bool) -> dict:
    wspolne = {"atr_pct": 1.0, "rsi": 50.0, "range_pos": 0.5, "volume_z": 0.0,
               "news_sent": 0.0, "funding_rate": 0.0, "funding_z": 0.0}
    if kup:
        return wspolne | {"ema_ratio": 1.01, "ret_short": 20.0,
                          "ret_mid": 40.0, "ret_long": 80.0}
    return wspolne | {"ema_ratio": 1.0, "ret_short": 0.0,
                      "ret_mid": 0.0, "ret_long": 0.0}


class _Kursor:
    """Atrapa kursora: odpowiedź po fragmencie zapytania, reszta milczy."""

    def __init__(self, ostatnia: datetime):
        self._ostatnia = ostatnia
        self._wynik: object = None

    def execute(self, sql, params=None):
        if "FROM ref.instrument" in sql:
            self._wynik = [{"id": 1, "symbol": "AAAUSDC", "quote_ccy": "USDC"},
                           {"id": 2, "symbol": "BBBUSDC", "quote_ccy": "USDC"}]
        elif "count(*) AS n" in sql:
            self._wynik = {"n": 1}
        elif "RETURNING id" in sql:
            self._wynik = {"id": 1}
        elif "AS v FROM learn.model_version" in sql:
            self._wynik = {"v": 1}
        elif "array_agg(id) AS ids" in sql:
            self._wynik = {"ids": []}
        elif "AS ostatnia FROM market.candle" in sql:
            self._wynik = {"ostatnia": self._ostatnia}
        else:
            self._wynik = []

    def executemany(self, sql, rows):
        pass

    def fetchone(self):
        return self._wynik if isinstance(self._wynik, dict) else None

    def fetchall(self):
        return self._wynik if isinstance(self._wynik, list) else []


class _Polaczenie:
    def __init__(self, kursor):
        self._k = kursor

    def cursor(self):
        return self._k

    def commit(self):
        pass

    def rollback(self):
        pass


def _przebieg(monkeypatch, spread_bps: float, *, limit: bool = False,
              kup_i_trzymaj=None) -> str:
    """`train()` na syntetycznym, rosnącym rynku z migającym sygnałem kupna.

    Cena rośnie o 0,1% na świecę, pozycja trzyma ~5 świec: brutto około
    +0,5% na transakcję przy koszcie symulacji 0,28% rundy (spot, taker).
    Dopłata 300 pb na rundę zjada to z naddatkiem."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(days=21)
    krok = timedelta(minutes=H5M.bar_minutes)
    n = int((now - start) / krok)
    # Przy limicie zakres świecy szeroki - limit ma się wypełniać, inaczej
    # nie byłoby transakcji do zmierzenia.
    zakres = 1.01 if limit else 1.0
    panel = [{"t": start + krok * i, "f": _cechy((i // 5) % 2 == 0),
              "price": 100.0 * 1.001 ** i, "high": 100.0 * 1.001 ** i * zakres,
              "low": 100.0 * 1.001 ** i / zakres} for i in range(n)]

    monkeypatch.setattr(trener, "MIN_INSTRUMENTS", 1)
    monkeypatch.setattr(trener, "load_panel", lambda *a, **k: list(panel))
    monkeypatch.setattr(trener, "n_konfiguracji", lambda *a, **k: 1)
    monkeypatch.setattr(trener, "odchylenie_prob_z_bazy", lambda *a, **k: (None, 0))
    monkeypatch.setattr(trener, "sha_kodu", lambda: "test")
    if kup_i_trzymaj is not None:
        monkeypatch.setattr(trener, "kup_i_trzymaj", kup_i_trzymaj)
    wydruk = io.StringIO()
    with redirect_stdout(wydruk):
        trener.train(_Polaczenie(_Kursor(panel[-1]["t"])), "crypto", 21, 1, H5M,
                     kind="spot", sucho=True, wybor="bazowe",
                     spread_bps=spread_bps,
                     egzekucja=Egzekucja(limit=True) if limit else None)
    return wydruk.getvalue()


def _okna_i_mediana(wydruk: str) -> tuple[int, int, float]:
    m = re.search(r"Okna: (\d+), dodatnie: (\d+)", wydruk)
    med = re.search(r"Mediana Sharpe \(OOS\): ([+-][\d.]+)", wydruk)
    assert m and med, wydruk[-3000:]
    return int(m.group(1)), int(m.group(2)), float(med.group(1))


def test_bramka_i_okna_widza_DOPLATE_a_nie_swiat_bez_spreadu(monkeypatch):
    """SEDNO. Ta sama symulacja, raz bez dopłaty, raz z dopłatą większą niż
    przewaga. Odsetek dodatnich okien i mediana muszą się przewrócić - bo to
    one, obok DSR, decydują o `validated`. Przed poprawką oba przebiegi
    meldowały te same dodatnie okna."""
    bez = _przebieg(monkeypatch, 0.0)
    okien, dodatnich, mediana = _okna_i_mediana(bez)
    assert okien >= 2 and dodatnich == okien and mediana > 0, (
        "kontrola: bez dopłaty syntetyczny rynek ma dawać dodatnie okna")

    z = _przebieg(monkeypatch, 300.0)
    okien_z, dodatnich_z, mediana_z = _okna_i_mediana(z)
    assert okien_z == okien
    assert dodatnich_z == 0, (
        f"dopłata 300 pb na rundę przy przewadze ~0,2% zostawiła {dodatnich_z}/"
        f"{okien_z} dodatnich okien — odsetek liczy się PRZED kosztem, który "
        f"odejmuje DSR")
    assert mediana_z < 0
    # Odniesienie płaci tę samą dopłatę za swoją rundę - z karty rynkowej.
    assert "kup-i-trzymaj po 300.00 pb na swoją jedną rundę" in z

    # Brutto obok, jawnie nazwane - i równe temu, co raportował przebieg bez
    # dopłaty, bo to ta sama symulacja.
    linia = re.search(r"brutto \(przed dopłatą [\d.]+ pb, NIE do bramki\): "
                      r"dodatnie (\d+)/(\d+), mediana Sharpe ([+-][\d.]+)", z)
    assert linia, "brak wiersza brutto obok miar po dopłacie z karty"
    assert int(linia.group(1)) == dodatnich and float(linia.group(3)) == pytest.approx(mediana)


def test_wiersz_okna_nadal_czytany_przez_porownaj_warianty(monkeypatch):
    """`scripts/porownaj_warianty.py` paruje okna po WZOR_OKNA. Dopisek brutto
    nie może rozerwać wzorca, a liczba, którą skrypt czyta, ma być po karcie."""
    from scripts.porownaj_warianty import WZOR_OKNA
    z = _przebieg(monkeypatch, 300.0)
    okna = list(WZOR_OKNA.finditer(z))
    assert okna, z[-3000:]
    for m in okna:
        assert float(m.group(3)) < 0, "WZOR_OKNA czyta Sharpe sprzed dopłaty"
        assert "brutto: Sharpe/transakcję=+" in m.group(6)


# ------------------------------------------------- 4. kup-i-trzymaj W TRENERZE
#
# Dwie mutacje, które przechodziły przez wszystkie testy powyżej (sprawdzone
# 2026-10-03 przez wstawienie ich do `train.py`):
#   (i)  `kup_i_trzymaj(...)` wołane w trenerze BEZ `doplata=` - odniesienie
#        brutto przy strategii po dopłacie,
#   (ii) porównanie z odniesieniem liczone na `oos_m_brutto` zamiast `oos_m`.
# Atrapa `kup_i_trzymaj` oddaje wynik zależny od tego, CZY dostała dopłatę, więc
# licznik „Pokonano kup-i-trzymaj" mówi wprost, którą ścieżką poszedł trener.

WZOR_POKONANE = re.compile(r"Pokonano kup-i-trzymaj PRZY TEJ SAMEJ EKSPOZYCJI w (\d+)/(\d+)")


def _pokonane(wydruk: str) -> tuple[int, int]:
    m = WZOR_POKONANE.search(wydruk)
    assert m, wydruk[-3000:]
    return int(m.group(1)), int(m.group(2))


def test_odniesienie_w_trenerze_DOSTAJE_doplate_z_karty(monkeypatch):
    """Zabija mutację (i). Odniesienie po dopłacie = -1000%, brutto = +1000%:
    strategia bije pierwsze w każdym oknie, drugiego w żadnym."""
    wywolania = []

    def atrapa(panele, kind, doplata=0.0):
        wywolania.append(doplata)
        return -1000.0 if doplata else 1000.0

    pokonane, porownywalne = _pokonane(
        _przebieg(monkeypatch, 300.0, kup_i_trzymaj=atrapa))
    assert porownywalne >= 2
    assert pokonane == porownywalne, (
        "odniesienie liczone BEZ dopłaty z karty — strategia płaci spread, "
        "koszyk nie")
    assert 0.03 in [pytest.approx(d) for d in wywolania]


def test_porownanie_z_odniesieniem_bierze_zwrot_PO_doplacie(monkeypatch):
    """Zabija mutację (ii). Odniesienie = 0%: zwrot strategii po dopłacie 300 pb
    jest ujemny w każdym oknie, brutto - dodatni. Licznik po dopłacie musi być
    zerem, licznik brutto (osobny wiersz) - kompletem."""
    pokonane, porownywalne = _pokonane(_przebieg(
        monkeypatch, 300.0, kup_i_trzymaj=lambda panele, kind, doplata=0.0: 0.0))
    assert porownywalne >= 2
    assert pokonane == 0, (
        f"{pokonane}/{porownywalne} okien „pobiło” zerowe odniesienie przy "
        f"dopłacie zjadającej przewagę — porównanie idzie na zwrocie BRUTTO")


def test_przy_limicie_doplata_NIE_udaje_pelnej_karty(monkeypatch):
    """Przy `--limit` niewypełnienie nie ma wartości i nie wchodzi do dopłaty.
    Wydruk ma to powiedzieć z nazwy i podać, ile zleceń symulacja nie
    wypełniła - a nie nazywać dopłaty „pełną kartą"."""
    z = _przebieg(monkeypatch, 5.85, limit=True)
    assert "pełna karta" not in z
    assert "karta BEZ: niewypelnienie (NIEZMIERZONE)" in z
    assert re.search(r"Niewypełnione zlecenia z limitem w symulacji OOS: \d+", z)


def test_blok_przewagi_nazywa_KAZDA_liczbe_brutto_albo_po_doplacie(monkeypatch):
    """Do 2026-10-03 blok „PRZEWAGA PO KOSZTACH" stawiał Sharpe złączony brutto
    obok mediany okien po dopłacie, bez słowa o tym. Każdy wiersz z liczbą
    w tym bloku ma się zaczynać od nazwy miary, z której pochodzi."""
    z = _przebieg(monkeypatch, 5.85)
    blok = z[z.index("PRZEWAGA BRUTTO"):z.index("RACHUNEK KOSZTU RUNDY")]
    wiersze = [w.strip() for w in blok.splitlines()[1:] if re.search(r"\d", w)
               and not w.startswith("PO DOPŁACIE")]
    assert wiersze
    for w in wiersze:
        assert w.startswith(("brutto:", "po dopłacie:")), w
    assert "po dopłacie: Sharpe/transakcję" in blok
