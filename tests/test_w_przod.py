"""H-4: test w przód „czy trening się uczy" — pary okien, miara okna, werdykt.

Kryterium jest zapisane w renacode-infra: docs/projekty/trader/hipotezy.md PRZED pomiarem; ten plik pilnuje,
że kod liczy dokładnie to, co tam stoi: okna rozłączne, wersje zamrożone przed
oknem, n = liczba okien, werdykt z pierwszych N_OKIEN i ani jednego więcej,
miara na danych sprzed decyzji, remis = różnica dokładnie zero, okno
policzone raz zamrożone. Bez bazy: panel syntetyczny, kursor-atrapa.
"""
from __future__ import annotations

import stat
import subprocess
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from services.engine import w_przod
from services.engine.horizons import H4H
from services.engine.portfolio import Egzekucja, indeks_swiec
from services.engine.signals import DEFAULT_WEIGHTS, REGIMES
from services.engine.w_przod import (
    DEFINICJA_MIARY, KOD_NIEGOTOWE, N_OKIEN, POCZATEK, PROG_LEPSZYCH, _ogon_dwumianowy,
    dane_okna, miara_okna, pary_okien, poczatek_tygodnia, werdykt)
from tests.test_tor_niegotowy_nie_zabija_drugiego import _skrypt_trenera

UTC = timezone.utc


def _niedziela(d: int) -> datetime:
    """Planowy trener: niedziela 04:40 czasu warszawskiego = 02:40 UTC."""
    return datetime(2026, 10, d, 2, 40, tzinfo=UTC)


def test_tydzien_zaczyna_sie_w_poniedzialek_o_polnocy_UTC():
    assert poczatek_tygodnia(datetime(2026, 10, 11, 2, 40, tzinfo=UTC)) == \
        datetime(2026, 10, 5, tzinfo=UTC)
    assert POCZATEK.weekday() == 0


def test_wersja_oceniana_dopiero_w_tygodniu_PO_zapisaniu():
    wersje = [(40, _niedziela(4)), (41, _niedziela(11)), (42, _niedziela(18))]
    pary = pary_okien(wersje, teraz=datetime(2026, 10, 27, tzinfo=UTC))
    assert [(p.od.day, p.nowa, p.poprzednia) for p in pary] == [(12, 41, 40), (19, 42, 41)]
    for p in pary:
        assert p.do - p.od == timedelta(days=7)
        zapis_nowej = dict(wersje)[p.nowa]
        assert zapis_nowej < p.od, "prognoza wersji dotknęła okna sprzed jej zapisu"


def test_okna_sa_rozlaczne_a_okno_otwarte_nie_liczy_sie():
    wersje = [(v, _niedziela(4) + timedelta(days=7 * i)) for i, v in enumerate(range(40, 46))]
    pary = pary_okien(wersje, teraz=datetime(2026, 11, 4, tzinfo=UTC))
    for a, b in zip(pary, pary[1:]):
        assert a.do <= b.od
    assert all(p.do <= datetime(2026, 11, 4, tzinfo=UTC) for p in pary)


def test_reczna_powtorka_w_tym_samym_tygodniu_NIE_daje_drugiej_pary():
    """04.10: planowy przebieg i ręczne powtórzenie kilka godzin później. Dwie
    wersje przed jednym oknem to jedna para (najnowsza), nie dwie próby."""
    wersje = [(39, _niedziela(4) - timedelta(days=7)), (40, _niedziela(4)),
              (41, _niedziela(4) + timedelta(hours=6)), (42, _niedziela(11))]
    pary = pary_okien(wersje, teraz=datetime(2026, 10, 20, tzinfo=UTC))
    assert [(p.nowa, p.poprzednia) for p in pary] == [(42, 41)]


def test_tydzien_bez_nowej_wersji_nie_daje_pary():
    wersje = [(40, _niedziela(4)), (41, _niedziela(18))]
    pary = pary_okien(wersje, teraz=datetime(2026, 10, 27, tzinfo=UTC))
    assert [(p.od.day, p.nowa, p.poprzednia) for p in pary] == [(19, 41, 40)]


def test_okna_sprzed_zapisania_hipotezy_sie_nie_licza():
    wersje = [(30, datetime(2026, 9, 6, tzinfo=UTC)), (31, datetime(2026, 9, 13, tzinfo=UTC))]
    assert pary_okien(wersje, teraz=datetime(2026, 10, 10, tzinfo=UTC)) == []


def test_werdykt_dopiero_po_N_rozstrzygnietych_oknach():
    stan, opis = werdykt([0.1] * (N_OKIEN - 1) + [0.0] * 5)
    assert stan == "w toku" and "remisów 5" in opis


def test_kryterium_progowe_i_kanarek():
    assert werdykt([0.1] * PROG_LEPSZYCH + [-0.1] * (N_OKIEN - PROG_LEPSZYCH))[0] == "UCZY SIĘ"
    assert werdykt([0.1] * (PROG_LEPSZYCH - 1)
                   + [-0.1] * (N_OKIEN - PROG_LEPSZYCH + 1))[0] == "NIE WYKAZANO"


def test_okna_po_N_nie_zmieniaja_werdyktu():
    """Zatrzymanie warunkowe: dokładanie okien, aż wynik „wyjdzie", to nie test."""
    pierwsze = [0.1] * (PROG_LEPSZYCH - 1) + [-0.1] * (N_OKIEN - PROG_LEPSZYCH + 1)
    assert werdykt(pierwsze + [0.1] * 50)[0] == "NIE WYKAZANO"


def test_test_znakow_liczy_ogon_dwumianowy_i_pomija_remisy():
    """10/12 to p = 79/4096 ≈ 0,019 - liczba zapisana w kryterium. Remisy miedzy
    oknami rozstrzygnietymi nie zabieraja miejsca w pierwszych N_OKIEN."""
    assert _ogon_dwumianowy(PROG_LEPSZYCH, N_OKIEN) == pytest.approx(79 / 4096)
    roznice = []
    for d in [0.1] * PROG_LEPSZYCH + [-0.1] * (N_OKIEN - PROG_LEPSZYCH):
        roznice += [d, 0.0]
    stan, opis = werdykt(roznice)
    assert stan == "UCZY SIĘ" and f"remisów {N_OKIEN}" in opis and "p = 0.019" in opis


def test_kolejnosc_kalendarza_decyduje_ktore_okna_licza():
    """Te same roznice w innej kolejnosci to inny werdykt - pierwsze 12, nie
    „najlepsze 12" ani „wszystkie"."""
    dobre, zle = [0.1] * PROG_LEPSZYCH, [-0.1] * (N_OKIEN - PROG_LEPSZYCH)
    assert werdykt(dobre + zle + zle)[0] == "UCZY SIĘ"
    assert werdykt(zle + zle + dobre)[0] == "NIE WYKAZANO"


# ------------------------------------------------- miara okna, bez bazy

OD = POCZATEK
DO = POCZATEK + timedelta(days=7)
ROZGRZEWKA_ATR = 2.0


def _cechy(kupuj: bool, atr: float) -> dict:
    """Silny sygnal kupna w `trend_up` albo neutralny (jak tests/test_portfolio.py)."""
    sila = 1.0 if kupuj else 0.0
    return {"atr_pct": atr, "ema_ratio": 1.0 + 0.01 * sila, "rsi": 50.0,
            "range_pos": 0.5, "ret_short": 20.0 * sila, "ret_mid": 40.0 * sila,
            "ret_long": 80.0 * sila, "volume_z": 0.0, "news_sent": 0.0,
            "funding_rate": 0.0, "funding_z": 0.0}


def _panel(cena0: float, wzrost_na_swiece: float, dni_rozgrzewki: int = 30) -> list[dict]:
    """Swiece 4h od `OD - dni_rozgrzewki` do `DO + 1 dzien` (czyli takze PO oknie).

    Rozgrzewka ma `atr_pct` = 2,0, okno 1,0: prog ATR z rozgrzewki to 2,0,
    z okna bylby 1,0. Sygnal miga co 3 swiece, zeby pozycje otwieraly sie
    i zamykaly w oknie.
    """
    wiersze, t, i = [], OD - timedelta(days=dni_rozgrzewki), 0
    while t < DO + timedelta(days=1):
        cena = cena0 * (1 + wzrost_na_swiece) ** i
        atr = ROZGRZEWKA_ATR if t < OD else 1.0
        wiersze.append({"t": t, "f": _cechy((i // 3) % 2 == 0, atr), "price": cena,
                        "high": cena * 1.004, "low": cena * 0.996})
        t += timedelta(hours=4)
        i += 1
    return wiersze


def _swiece_1h(panel: list[dict]) -> dict:
    """Swiece 1h z panelu 4h: cena liniowo miedzy zamknieciami 4h."""
    sw = []
    for a, b in zip(panel, panel[1:]):
        for k in range(4):
            c0 = a["price"] + (b["price"] - a["price"]) * k / 4
            c1 = a["price"] + (b["price"] - a["price"]) * (k + 1) / 4
            sw.append({"t": a["t"] + timedelta(hours=k), "open": c0,
                       "high": max(c0, c1) * 1.003, "low": min(c0, c1) * 0.997,
                       "close": c1})
    return indeks_swiec(sw)


def _dane(egzekucja: Egzekucja | None = None, wzrost: float = 0.004,
          doplata: float = 0.0):
    panele = {"AAAUSDC": _panel(100.0, wzrost), "BBBUSDC": _panel(50.0, wzrost / 2)}
    egz = egzekucja or Egzekucja()
    swiece = ({s: _swiece_1h(p) for s, p in panele.items()}
              if egz.pesymistyczna or egz.opoznienie_min else None)
    return dane_okna(panele, OD, DO, H4H, "spot_eu", egz, swiece, doplata, doplata,
                     {"AAAUSDC": 1, "BBBUSDC": 2})


def _params(prog: float) -> dict:
    """`learn.model_version.params` w formacie zapisu trenera."""
    return {"weights": DEFAULT_WEIGHTS, "min_confidence": 0.05,
            "enter_threshold": prog, "exit_threshold": prog * 0.45,
            "enter_threshold_per_regime": {r: prog for r in REGIMES}}


def test_okno_to_wylacznie_od_do_a_prog_ATR_z_danych_SPRZED_okna():
    """Okno [od, do): ani swieca rozgrzewki, ani swieca po `do` nie handluje.
    Prog rezimu z rozgrzewki - liczba z tygodnia, ktory mierzymy, bylaby
    wiedza z przyszlosci dla kazdej decyzji w tym tygodniu."""
    dane = _dane()
    for wiersze in dane.panele.values():
        assert wiersze and all(OD <= r["t"] < DO for r in wiersze)
    assert set(dane.progi_atr.values()) == {ROZGRZEWKA_ATR}


def test_okno_bez_danych_to_None_a_nie_remis():
    """Tydzien bez panelu (potok cech stal) to brak pomiaru. Remis znaczy
    „obie wersje mialy na czym handlowac i zrobily to samo"."""
    panele = {"AAAUSDC": [r for r in _panel(100.0, 0.004) if r["t"] < OD]}
    assert dane_okna(panele, OD, DO, H4H, "spot_eu", Egzekucja(), None, 0.0, 0.0) is None


def test_ta_sama_wersja_po_obu_stronach_daje_remis_DOKLADNIE_zero():
    dane = _dane()
    a, b = miara_okna(dane, _params(0.05)), miara_okna(dane, _params(0.05))
    assert a.rund > 0, "syntetyczny panel ma dawac transakcje, inaczej test nic nie mierzy"
    assert a.miara - b.miara == 0.0


def test_dwie_wersje_bez_transakcji_to_remis_zero():
    w = miara_okna(_dane(), _params(50.0))      # prog nieosiagalny
    assert (w.rund, w.zwrot, w.ekspozycja, w.miara) == (0, 0.0, 0.0, 0.0)


def test_miara_to_zwrot_minus_koszyk_przy_tej_samej_ekspozycji():
    """r − f × B1: sama obecnosc w rosnacym rynku nie jest umiejetnoscia."""
    dane = _dane()
    w = miara_okna(dane, _params(0.05))
    assert dane.koszyk > 0, "rynek w panelu rosnie - koszyk musi zarobic"
    assert w.ekspozycja > 0 and w.zwrot > 0
    assert w.miara == pytest.approx(w.zwrot - w.ekspozycja * dane.koszyk)
    assert w.miara < w.zwrot


def test_rozne_progi_daja_rozne_miary():
    """Kanarek odwrotny do remisu: miara, ktora nie widzi roznicy wersji,
    dawalaby same remisy i test znakow nie zapadlby nigdy."""
    dane = _dane()
    assert miara_okna(dane, _params(0.05)).miara != miara_okna(dane, _params(0.6)).miara


def test_doplata_z_karty_obniza_zwrot():
    bez, z = _dane(doplata=0.0), _dane(doplata=0.002)
    assert miara_okna(z, _params(0.05)).zwrot < miara_okna(bez, _params(0.05)).zwrot


def test_model_pesymistyczny_z_opoznieniem_liczy_sie_na_swiecach_1h():
    """Konfiguracja H-4: limit, 20 min, wypelnienie w godzinie po wysylce."""
    dane = _dane(Egzekucja(limit=True, opoznienie_min=20, pesymistyczna=True,
                           spread_per_instrument={"AAAUSDC": 2.0, "BBBUSDC": 4.0}))
    w = miara_okna(dane, _params(0.05))
    assert w.wypelnione > 0
    assert w.miara == pytest.approx(w.zwrot - w.ekspozycja * dane.koszyk)


def test_wersja_bez_wag_jest_bledem_a_nie_zalozeniami():
    """Przebieg, ktory padl przed zapisem wyniku, nie ma czego oceniac.
    Podstawienie zalozen poczatkowych udawaloby wersje, ktorej nie ma."""
    with pytest.raises(ValueError, match="weights"):
        miara_okna(_dane(), {"bar": "4h"})


def test_zalozenia_poczatkowe_licza_sie_ta_sama_miara():
    dane = _dane()
    w = miara_okna(dane, None)
    assert w.miara == pytest.approx(w.zwrot - w.ekspozycja * dane.koszyk)


# --------------------------------------------- main(): zapis, zamrozenie, kody

class _Kursor:
    def __init__(self, wersje, zapisane):
        self._wersje, self._zapisane = wersje, zapisane
        self.zapisy, self._ostatnie = [], []

    def execute(self, sql, params=None):
        if "FROM learn.model_version" in sql:
            self._ostatnie = self._wersje
        elif "FROM learn.w_przod_okno" in sql:
            self._ostatnie = self._zapisane
        elif sql.startswith("INSERT INTO learn.w_przod_okno"):
            self.zapisy.append(params)
            self._ostatnie = []
        else:
            raise AssertionError(f"nieoczekiwane zapytanie: {sql}")

    def fetchall(self):
        return self._ostatnie


class _Polaczenie:
    def __init__(self, kursor):
        self.kursor, self.commity = kursor, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.kursor

    def commit(self):
        self.commity += 1


class _Zegar:
    """Podmiana `datetime` w module: `now()` staly, reszta jak w oryginale."""

    def __init__(self, teraz):
        self._teraz = teraz

    def now(self, tz=None):
        return self._teraz

    def __getattr__(self, nazwa):
        return getattr(datetime, nazwa)

    def __call__(self, *a, **k):
        return datetime(*a, **k)


def _main(monkeypatch, teraz, wersje, zapisane=(), z_danymi=True, argv=()):
    kursor = _Kursor(list(wersje), list(zapisane))
    conn = _Polaczenie(kursor)
    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setenv("GIT_SHA", "sha-abc1234")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(w_przod, "datetime", _Zegar(teraz))
    wolane = []

    def wczytaj(cur, engine, h, kind, od, do):
        wolane.append(od)
        return (_dane() if z_danymi else None), "odcisk"
    monkeypatch.setattr(w_przod, "wczytaj_okno", wczytaj)
    kod = w_przod.main(["--engine", "crypto", "--bar", "4h", *argv])
    return kod, kursor, conn, wolane


def _wersja(id_, nr, kiedy, prog):
    return {"id": id_, "version": nr, "created_at": kiedy, "params": _params(prog)}


WERSJE = [_wersja(140, 40, _niedziela(4), 0.6), _wersja(141, 41, _niedziela(11), 0.05)]
PO_PIERWSZYM_OKNIE = datetime(2026, 10, 25, 2, 40, tzinfo=UTC)


def test_przed_zamknieciem_pierwszego_okna_kod_zero_i_nic_nie_liczy(monkeypatch, capsys):
    """Stan do 2026-10-19: nie bylo czego liczyc - to nie awaria ani
    „niegotowe", wiec Job trenera nie ma powodu sie palic."""
    kod, kursor, _, wolane = _main(monkeypatch, datetime(2026, 10, 18, 2, 40, tzinfo=UTC),
                                   WERSJE)
    assert kod == 0 and kursor.zapisy == [] and wolane == []
    assert "brak okna do policzenia" in capsys.readouterr().out


def test_para_jest_liczona_i_zapisana_z_sha_i_definicja(monkeypatch, capsys):
    kod, kursor, conn, wolane = _main(monkeypatch, PO_PIERWSZYM_OKNIE, WERSJE)
    assert kod == 0 and wolane == [POCZATEK]
    assert len(kursor.zapisy) == 1 and conn.commity == 1
    (engine, bar, od, do, id_nowej, id_poprz, m_n, m_p, d, szczegoly, definicja,
     sha) = kursor.zapisy[0]
    assert (engine, bar, od, do) == ("crypto", "4h", POCZATEK, POCZATEK + timedelta(days=7))
    assert (id_nowej, id_poprz) == (141, 140)
    assert d == m_n - m_p and d != 0
    assert definicja == DEFINICJA_MIARY and sha == "abc1234"
    assert '"zalozenia"' in szczegoly and '"wersja_nowa": 41' in szczegoly
    assert "WERDYKT H-4: w toku" in capsys.readouterr().out


def test_okno_zapisane_NIE_jest_liczone_ponownie(monkeypatch):
    """Zamrozenie serii: przeliczenie po rewizji swiec albo po wdrozeniu
    zmienialoby wynik, ktory juz wszedl do testu znakow."""
    zapisane = [{"okno_od": POCZATEK, "definicja_miary": DEFINICJA_MIARY, "roznica": 0.3}]
    kod, kursor, _, wolane = _main(monkeypatch, PO_PIERWSZYM_OKNIE, WERSJE, zapisane)
    assert kod == 0 and wolane == [] and kursor.zapisy == []


def test_okno_innej_definicji_miary_nie_wchodzi_do_serii(monkeypatch, capsys):
    zapisane = [{"okno_od": POCZATEK, "definicja_miary": "H-4/m0", "roznica": 0.3}]
    _, kursor, _, wolane = _main(monkeypatch, PO_PIERWSZYM_OKNIE, WERSJE, zapisane)
    assert wolane == [POCZATEK] and len(kursor.zapisy) == 1
    assert "inną definicją miary (H-4/m0)" in capsys.readouterr().out


def test_brak_danych_w_oknie_to_kod_NIEGOTOWE_bez_zapisu(monkeypatch, capsys):
    kod, kursor, conn, _ = _main(monkeypatch, PO_PIERWSZYM_OKNIE, WERSJE, z_danymi=False)
    assert kod == KOD_NIEGOTOWE == 3
    assert kursor.zapisy == [] and conn.commity == 0
    assert "BRAK DANYCH" in capsys.readouterr().out


def test_sucho_liczy_ale_nie_zapisuje(monkeypatch):
    kod, kursor, conn, wolane = _main(monkeypatch, PO_PIERWSZYM_OKNIE, WERSJE,
                                      argv=["--sucho"])
    assert kod == 0 and wolane == [POCZATEK] and kursor.zapisy == [] and conn.commity == 0


def test_bramka_uniwersum_equity_to_kod_NIEGOTOWE_z_trenera():
    """Okno equity przy pustym uniwersum decyzyjnym: ta sama bramka i ten sam
    kod co u trenera (`train._uniwersum`), nie wlasna kopia warunku."""
    from services.engine.horizons import H1H
    from tests.test_tor_niegotowy_nie_zabija_drugiego import Kursor
    with pytest.raises(SystemExit) as wyjatek:
        w_przod.wczytaj_okno(Kursor([[]]), "equity", H1H, "stock", OD, DO)
    assert wyjatek.value.code == KOD_NIEGOTOWE


def test_okna_wstecz_tylko_na_sucho():
    """Okna sprzed 2026-10-12 nie sa testem w przod - nie wolno ich zapisac obok."""
    with pytest.raises(SystemExit):
        w_przod.main(["--od", "2026-09-07"])


# ------------------------------------------------- skrypt zadania trenera

def _przebieg_zadania(tmp_path, kod_w_przod: int) -> subprocess.CompletedProcess:
    """Skrypt trenera z atrapa `python` (wzor: test_tor_niegotowy_nie_zabija_drugiego)."""
    atrapa = tmp_path / "python"
    atrapa.write_text("#!/bin/sh\n"
                      'case "$*" in\n'
                      f'  *w_przod.py*) echo "ATRAPA w_przod"; exit {kod_w_przod} ;;\n'
                      '  *) echo "ATRAPA $1"; exit 0 ;;\n'
                      "esac\n")
    atrapa.chmod(atrapa.stat().st_mode | stat.S_IEXEC)
    skrypt = tmp_path / "zadanie.sh"
    skrypt.write_text(_skrypt_trenera())
    return subprocess.run(["sh", str(skrypt)], capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)})


def test_cron_niegotowe_okno_nie_zapala_Joba_ale_jest_w_podsumowaniu(tmp_path):
    wynik = _przebieg_zadania(tmp_path, KOD_NIEGOTOWE)
    assert wynik.returncode == 0, wynik.stdout + wynik.stderr
    assert "TORY NIEURUCHOMIONE" in wynik.stdout and "w_przod/crypto/" in wynik.stdout


def test_cron_awaria_miary_zapala_Joba(tmp_path):
    """Kanarek na `|| true`: seria, ktora po cichu przestaje przybywac,
    zabiera werdyktowi okna bez sladu."""
    assert _przebieg_zadania(tmp_path, 1).returncode == 1


def test_cron_w_przod_po_trenerze_i_po_dozorze(tmp_path):
    wynik = _przebieg_zadania(tmp_path, 0)
    assert wynik.returncode == 0
    linie = wynik.stdout.splitlines()
    pierwszy = linie.index("ATRAPA w_przod")
    inne = [i for i, w in enumerate(linie)
            if w in ("ATRAPA services/engine/dozor.py", "ATRAPA services/engine/train.py")]
    assert len(inne) >= 3 and max(inne) < pierwszy
