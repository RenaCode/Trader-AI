"""Tor R4 (`services/engine/r4_momentum.py`, hipoteza H-3, STOP/GO 2).

Trzy rzeczy, ktore musza byc prawdziwe, zanim wynik H-3 cokolwiek znaczy:
w trendzie R4 kupuje najsilniejsze instrumenty, przy BTC pod EMA siedzi
w gotowce, a decyzja nie widzi niczego, co zamknelo sie po niej.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest

from services.engine import r4_momentum as r4
from services.engine.koszty import POSLIZG_TAKER, koszty

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
BAR = 240


def _rynek(dzienne: dict[str, float], dni: int = 200, start: float = 100.0):
    """Panele 4h i swiece 1h; cena rosnie geometrycznie o `dzienny` na dobe.
    `low` godziny 0,3% pod mniejsza z cen otwarcia i zamkniecia - limit
    pol spreadu pod cena wypelnia sie, gdy rynek nie ucieka."""
    panele, swiece = {}, {}
    for s, dzienny in dzienne.items():
        krok = (1 + dzienny) ** (1 / 24)
        godz = []
        for i in range(dni * 24):
            o, c = start * krok ** i, start * krok ** (i + 1)
            godz.append({"t": T0 + timedelta(hours=i), "open": o, "close": c,
                         "high": max(o, c) * 1.003, "low": min(o, c) * 0.997})
        swiece[s] = {g["t"]: g for g in godz}
        panele[s] = [{"t": godz[i]["t"], "price": godz[i + 3]["close"]}
                     for i in range(0, len(godz), 4)]
    return panele, swiece


def _okno(panele, dni_okna=30):
    koniec = max(r["t"] for r in panele["BTCUSDC"]) + timedelta(hours=4)
    return koniec - timedelta(days=dni_okna), koniec


def _decyzje(panele, swiece, od, do):
    indeksy = {s: {r["t"]: r for r in rows} for s, rows in panele.items()}
    filtr = r4.Filtr(panele["BTCUSDC"], BAR, r4.PARAMETRY_R4["ema_filtra_dni"])
    return r4.decyzje_okna(panele, indeksy, swiece, sorted(panele), od, do, filtr, BAR)


def _symuluj(panele, swiece, od, do, doplata=0.0, spread=None, rng=None):
    dec = _decyzje(panele, swiece, od, do)
    konca = {s: r4._cena_konca(panele, s, od, do) for s in panele}
    return dec, r4.symuluj_okno(dec, konca, od, do, udzial=1.0,
                                spread_bps=spread or dict.fromkeys(panele, 2.0),
                                kind="spot_eu", doplata=doplata, rng=rng)


def test_dni_decyzji_konwencja_b2():
    """Pierwsza decyzja na pierwszym zamknieciu doby wewnatrz okna, ostatnia
    nie pozniej niz swieca przed koncem okna (ta sluzy domknieciu)."""
    od, do = datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 1, 31, tzinfo=timezone.utc)
    dni = r4.dni_decyzji(od, do, BAR)
    assert dni[0] == datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert dni[-1] == datetime(2025, 1, 30, tzinfo=timezone.utc)
    assert len(dni) == 29


def test_trend_wejscie_w_trzy_najsilniejsze():
    panele, swiece = _rynek({"BTCUSDC": 0.004, "ETHUSDC": 0.006, "SOLUSDC": 0.009,
                             "ADAUSDC": 0.002, "XRPUSDC": -0.001, "DOGEUSDC": 0.008})
    od, do = _okno(panele)
    dec, w = _symuluj(panele, swiece, od, do)
    assert all(d.filtr for d in dec)
    assert {s for _, s in w.wejscia} == {"SOLUSDC", "DOGEUSDC", "ETHUSDC"}
    # Sklad stabilny: trzy wejscia na poczatku, potem trzymanie do konca okna.
    assert len(w.wejscia) == 3 and len(w.rundy) == 3
    assert w.ekspozycja > 0.9
    assert w.zwrot > 0


def test_btc_pod_ema_gotowka():
    """Filtr trendu: BTC pod EMA 50 d trzyma R4 w gotowce, choc cos rosnie."""
    panele, swiece = _rynek({"BTCUSDC": -0.004, "ETHUSDC": -0.003, "SOLUSDC": 0.006,
                             "ADAUSDC": 0.004})
    od, do = _okno(panele)
    dec, w = _symuluj(panele, swiece, od, do)
    assert not any(d.filtr for d in dec)
    assert w.ekspozycja == 0.0 and w.zwrot == 0.0 and w.rundy == []
    assert w.wypelnione == 0 and w.niewypelnione == 0


def test_filtr_wylacza_w_trakcie_okna_i_wychodzi_ze_wszystkiego():
    """Pozycje zamykane, gdy BTC spada pod EMA - wyjscie nie czeka konca okna."""
    panele, swiece = _rynek({"BTCUSDC": 0.004, "ETHUSDC": 0.006, "SOLUSDC": 0.009,
                             "ADAUSDC": 0.002})
    od, do = _okno(panele)
    # Ostatnie 10 dni: BTC zjezdza o 40% -> zamkniecie dobowe pod EMA 50 d.
    granica = do - timedelta(days=10)
    for r in panele["BTCUSDC"]:
        if r["t"] >= granica:
            r["price"] *= 0.6
    dec, w = _symuluj(panele, swiece, od, do)
    assert dec[0].filtr and not dec[-1].filtr
    assert len(w.rundy) == 3
    assert w.ekspozycja < 0.8


def test_brak_przecieku_przyszlosci():
    """Zmiana wszystkiego, co zamknelo sie PO decyzji, nie zmienia decyzji;
    zmiana ostatniej doby przed decyzja nie zmienia momentum (pominiety dzien)."""
    panele, swiece = _rynek({"BTCUSDC": 0.004, "ETHUSDC": 0.006, "SOLUSDC": 0.009,
                             "ADAUSDC": 0.002, "XRPUSDC": -0.001})
    od, do = _okno(panele)
    przed = _decyzje(panele, swiece, od, do)
    d = przed[10]
    rng = random.Random(1)
    zepsute = {s: [dict(r, price=r["price"] * rng.uniform(0.2, 5.0))
                   if r["t"] + timedelta(minutes=BAR) > d.t else r for r in rows]
               for s, rows in panele.items()}
    po = {x.t: x for x in _decyzje(zepsute, swiece, od, do)}[d.t]
    assert po.sygnaly == d.sygnaly
    assert po.filtr == d.filtr
    assert po.cena_decyzji == d.cena_decyzji
    # Ostatnia doba przed decyzja: momentum jej nie czyta.
    dzien = {s: [dict(r, price=r["price"] * 3.0)
                 if d.t - timedelta(days=1) < r["t"] + timedelta(minutes=BAR) <= d.t
                 else r for r in rows] for s, rows in panele.items()}
    po = {x.t: x for x in _decyzje(dzien, swiece, od, do)}[d.t]
    assert po.sygnaly == d.sygnaly


def test_momentum_z_t_minus_30_do_t_minus_1():
    panele, _ = _rynek({"BTCUSDC": 0.01})
    idx = {r["t"]: r for r in panele["BTCUSDC"]}
    t = T0 + timedelta(days=100)
    m = r4.momentum(idx, t, BAR)
    assert abs(m - (1.01 ** 29 - 1)) < 1e-9


def test_niewypelnione_wejscie_przepada_i_nie_ma_rundy():
    """Rynek ucieka w gore: `low` kolejnej godziny powyzej limitu."""
    panele, swiece = _rynek({"BTCUSDC": 0.004, "ETHUSDC": 0.006, "SOLUSDC": 0.009,
                             "ADAUSDC": 0.002})
    for s in swiece.values():
        for g in s.values():
            g["low"] = max(g["open"], g["close"]) * 1.01
    od, do = _okno(panele)
    _, w = _symuluj(panele, swiece, od, do)
    assert w.wypelnione == 0 and w.niewypelnione > 0
    assert w.rundy == [] and w.ekspozycja == 0.0
    assert w.utracone and all(u > 0 for u in w.utracone)


def test_koszt_rundy_maker_wejscie_taker_wyjscie():
    """Plaski rynek: runda kosztuje prowizje maker + taker, poslizg wyjscia,
    doplate z karty; zysk z pol spreadu przy wejsciu limitem."""
    panele, swiece = _rynek({"BTCUSDC": 0.0, "ETHUSDC": 0.0, "SOLUSDC": 0.0})
    for s in ("BTCUSDC", "ETHUSDC", "SOLUSDC"):
        for r in panele[s]:
            r["price"] = 100.0
        for g in swiece[s].values():
            g.update(open=100.0, close=100.0, low=99.0, high=101.0)
    panele["BTCUSDC"] = [dict(r, price=100.0 + i * 1e-6) for i, r in enumerate(panele["BTCUSDC"])]
    od, do = _okno(panele)
    k = koszty("spot_eu")
    _, w = _symuluj(panele, swiece, od, do, doplata=0.001,
                    spread={"BTCUSDC": 0.0, "ETHUSDC": 0.0, "SOLUSDC": 0.0})
    plaskie = [r for r in w.rundy]
    assert len(plaskie) == 3
    oczekiwany = (1 - POSLIZG_TAKER) - 1 - k.maker - k.taker * (1 - POSLIZG_TAKER) - 0.001
    for r in plaskie[1:]:
        assert abs(r - oczekiwany) < 1e-6


def test_permutacja_zmienia_wybor_ale_nie_filtr():
    panele, swiece = _rynek({"BTCUSDC": 0.004, "ETHUSDC": 0.006, "SOLUSDC": 0.009,
                             "ADAUSDC": 0.002, "XRPUSDC": -0.001, "DOGEUSDC": 0.008,
                             "LINKUSDC": 0.001})
    od, do = _okno(panele)
    _, prawdziwy = _symuluj(panele, swiece, od, do)
    _, losowy = _symuluj(panele, swiece, od, do, rng=random.Random(7))
    assert {s for _, s in losowy.wejscia} != {s for _, s in prawdziwy.wejscia}
    assert losowy.ekspozycja > 0          # filtr nadal wpuszcza do rynku
    # W silnym rozwarstwieniu prawdziwy ranking bije losowy.
    assert prawdziwy.zwrot > losowy.zwrot


def _miary(dodatnie=30, z_eksp=40, b1=(30, 40), b2=(25, 40), dsr=0.97, perm=80.0):
    return {"z_ekspozycja": z_eksp, "dodatnie": dodatnie,
            "bije": {"B1": b1, "B2": b2, "B3": (0, 0)}, "dsr": dsr,
            "percentyl_permutacji": perm}


def test_werdykt_go_tylko_przy_wszystkich_kryteriach():
    go, linie = r4.werdykt({p: _miary() for p in range(-2, 3)})
    assert go and linie[-1] == "STOP/GO 2: GO"
    for zle in (dict(dodatnie=20), dict(b1=(20, 40)), dict(b2=(20, 40)),
                dict(perm=69.9), dict(dsr=0.94)):
        po = {p: _miary() for p in range(-2, 3)}
        po[1] = _miary(**zle)       # jedno przesuniecie wystarczy przy K2/K3
        if "dodatnie" in zle or "b1" in zle or "b2" in zle:
            po = {p: _miary(**zle) for p in range(-2, 3)}
        go, linie = r4.werdykt(po)
        assert not go, zle
        assert linie[-1] == "STOP/GO 2: STOP"


# ------------------------------------------------------------ harness na pliku

def _plik():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from panel_syntetyczny import plik_syntetyczny
    return plik_syntetyczny()


def test_pomiar_na_tych_samych_oknach_co_trener_i_powtarzalny():
    from services.engine import train as tr
    from services.engine.horizons import H4H
    plik = _plik()
    w1 = r4.pomiar(plik, days=390, przesuniecia=(-1, 0), n_permutacji=4)
    w2 = r4.pomiar(_plik(), days=390, przesuniecia=(-1, 0), n_permutacji=4)
    for nazwa in r4.WARIANTY:
        for p in (-1, 0):
            a, b = w1["warianty"][nazwa][p], w2["warianty"][nazwa][p]
            assert a == b
    dane = r4.przygotuj(plik, 390, "spot_eu")
    start = tr.poczatek_siatki(tr.KOTWICA_SIATKI, dane.now - timedelta(days=390), 30)
    okna_trenera = tr.okna_wf(dane.panele, H4H, start, dane.now)
    okna_r4 = r4.okna_przesuniecia(dane, 0, 390, r4.Filtr(dane.panele["BTCUSDC"], BAR, 50))
    assert [(o.test_from, o.test_to, o.aktywne) for o in okna_r4] == \
        [(o.test_from, o.test_to, o.aktywne) for o in okna_trenera]
    assert all(o.odn["B1"] is not None and o.odn["B2"] is not None for o in okna_r4)
    # Spread per instrument z pomiaru w pliku - ten sam co model pesymistyczny H-1.
    assert dane.spread_bps["BTCUSDC"] == 1.0 and dane.spread_bps["ETHUSDC"] == 3.0


def test_warianty_roznia_sie_skala_nie_zwrotem_na_runde():
    w = r4.pomiar(_plik(), days=390, przesuniecia=(0,), n_permutacji=0)
    p, f = w["warianty"]["P"][0], w["warianty"]["F"][0]
    assert p["rund"] == f["rund"]
    if p["rund"]:
        # Rozni je tylko pyl (zalezy od notionalu) - ulamki punktu bazowego.
        assert abs(p["zwrot_na_runde_pb"] - f["zwrot_na_runde_pb"]) < 1.0
        assert abs(f["srednia_ekspozycja"] / p["srednia_ekspozycja"] - 1 / 0.15) < 1e-6


def test_eksperyment_r4_drukuje_werdykt(tmp_path, capsys):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import eksperyment

    from services.engine import panel_plik
    sciezka = tmp_path / "panel.json.gz"
    panel_plik.zapisz(sciezka, _plik())
    assert eksperyment.main(["--panel", str(sciezka), "--r4", "--hipoteza", "H-3",
                             "--days", "390", "--przesuniecia=-1,0",
                             "--permutacje", "2"]) == 0
    out = capsys.readouterr().out
    assert "## Wariant P" in out and "## Wariant F" in out
    assert out.count("STOP/GO 2:") == 2


# ------------------------------------------------------------ DSR po wejsciach

def _okno_atrapa():
    return r4.Okno(T0, T0 + timedelta(days=30), [], [], {}, {})


def _dane_atrapa():
    """Pola, ktore `ocen_przesuniecie` przekazuje do (podstawionej) symulacji."""
    from types import SimpleNamespace
    return SimpleNamespace(spread_bps={}, kind="spot_eu")


def _wynik(zwroty, chwile):
    w = r4.WynikOkna()
    w.rundy, w.wejscia_rund = list(zwroty), list(chwile)
    return w


def test_DSR_K2_liczy_T_po_chwilach_wejscia_a_nie_po_rundach(monkeypatch):
    """Audyt 2026-10-10, S2-4: bramka STOP/GO momentum liczyla T = liczba rund.
    Rundy top-k z jednego rebalansu dziela szok - trzy kopie tej samej proby
    (top-3 na kazdej dacie) podnosily DSR samym poszerzeniem top-k."""
    rng = random.Random(7)
    pojedyncze = [rng.gauss(0.004, 0.03) for _ in range(60)]
    chwile = [T0 + timedelta(days=i) for i in range(60)]

    def ocen(zwroty, ch):
        monkeypatch.setattr(r4, "symuluj_okno", lambda *a, **k: _wynik(zwroty, ch))
        return r4.ocen_przesuniecie([_okno_atrapa()], _dane_atrapa(), 1.0, 0.0,
                                    n_permutacji=0, seed=1)

    jedna = ocen(pojedyncze, chwile)
    top3 = ocen([z for z in pojedyncze for _ in range(3)],
                [t for t in chwile for _ in range(3)])

    assert top3["dsr"] == pytest.approx(jedna["dsr"], abs=0.02), (
        "szerokosc top-k podniosla DSR - T liczone po rundach, nie po wejsciach")
    assert top3["dsr_projektu"] == pytest.approx(jedna["dsr_projektu"], abs=0.02)
    assert top3["rund"] == 180 and top3["rund_niezaleznych"] == 60


def test_symulacja_zapisuje_chwile_wejscia_kazdej_rundy():
    panele, swiece = _rynek({"BTCUSDC": 0.004, "ETHUSDC": 0.006, "SOLUSDC": 0.009,
                             "ADAUSDC": 0.002, "XRPUSDC": -0.001, "DOGEUSDC": 0.008})
    od, do = _okno(panele)
    _, w = _symuluj(panele, swiece, od, do)
    assert len(w.wejscia_rund) == len(w.rundy) == 3
    # Trzy wejscia z jednej decyzji = jedna chwila, jedna obserwacja niezalezna.
    assert len(set(w.wejscia_rund)) == 1
