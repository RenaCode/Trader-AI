"""Stala siatka okien, piec przesuniec i horyzont IC (audyt Fable T4: F-2.2, F-2.6).

Werdykt zalezal od godziny uruchomienia: siatka startowala w `now - --days`,
wiec przesuwala sie o dobe z kazdym nocnym przebiegiem (13 zmian statusu
w 35 przebiegach). Siatka jest teraz zakotwiczona w kalendarzu, a status
liczy sie z minimum po przesunieciach {-2..+2} dni.
"""
from __future__ import annotations

import contextlib
import io
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.engine import train as tr  # noqa: E402
from services.engine.horizons import HORIZONS, get as get_horizon  # noqa: E402
from services.engine.portfolio import Egzekucja  # noqa: E402

from panel_syntetyczny import DNI, KONIEC, plik_syntetyczny  # noqa: E402

H4 = get_horizon("4h")
UTC = timezone.utc


# ------------------------------------------------------------ kotwica siatki

def test_poczatek_siatki_lezy_na_kotwicy_i_nie_przed_panelem():
    kotwica = datetime(2022, 9, 1, tzinfo=UTC)
    od = datetime(2022, 12, 3, 4, 34, tzinfo=UTC)
    start = tr.poczatek_siatki(kotwica, od, 30)
    assert start >= od
    assert (start - kotwica) % timedelta(days=30) == timedelta(0)
    assert start - od < timedelta(days=30)


def test_dwa_przebiegi_w_odstepie_doby_licza_TE_SAME_okna():
    """Panel konczacy sie dobe pozniej (nocny przebieg nastepnego dnia) daje
    ten sam poczatek siatki - chyba ze w te dobe wypadl punkt siatki."""
    kotwica = tr.KOTWICA_SIATKI
    dzis = datetime(2026, 10, 3, 4, 40, tzinfo=UTC)
    for dni in (1400, 1399, 400):
        a = tr.poczatek_siatki(kotwica, dzis - timedelta(days=dni), 30)
        b = tr.poczatek_siatki(kotwica, dzis + timedelta(days=1) - timedelta(days=dni), 30)
        assert a == b


def test_bez_kotwicy_siatka_jak_przed_T4():
    od = datetime(2025, 1, 1, 7, tzinfo=UTC)
    assert tr.poczatek_siatki(None, od, 30) == od


def _plik_do(koniec: datetime) -> dict:
    """Te same dane, przebieg uruchomiony w chwili `koniec`."""
    plik = plik_syntetyczny()
    plik["do"] = koniec
    plik["ostatnia_swieca"] = koniec
    plik["panele"] = {s: [r for r in p if r["t"] <= koniec] for s, p in plik["panele"].items()}
    return plik


def _przebieg(plik: dict, od, przesuniecia=(0,), dni=300) -> tuple[dict, list[str]]:
    wydruk = io.StringIO()
    with contextlib.redirect_stdout(wydruk):
        wynik = tr.train(None, "crypto", dni, 2, H4, "spot_eu", Egzekucja(limit=True),
                         sucho=True, wybor="ic", panel=plik, od=od,
                         przesuniecia=przesuniecia)
    okna = [re.sub(r"\s+Sharpe.*", "", w) for w in wydruk.getvalue().splitlines()
            if re.match(r"\s+okno\s+\d+", w)]
    return wynik, okna


def test_przebieg_DOBE_POZNIEJ_daje_ten_sam_status_i_te_same_okna():
    """Kryterium akceptacji T4: dwa `--sucho` w odstepie 24 h - ten sam status."""
    wczoraj, dzis = KONIEC - timedelta(days=1), KONIEC
    a, okna_a = _przebieg(_plik_do(wczoraj), tr.KOTWICA_SIATKI, tr.PRZESUNIECIA_SIATKI)
    b, okna_b = _przebieg(_plik_do(dzis), tr.KOTWICA_SIATKI, tr.PRZESUNIECIA_SIATKI)
    assert okna_a and okna_a == okna_b
    assert a["status"] == b["status"]
    assert a["stabilny"]["dodatnie_pct"] == b["stabilny"]["dodatnie_pct"]
    # DSR drga o ~1e-6: dopłata za pył liczy się z cen okna `now - days`,
    # które przesuwa się z zegarem. Okna i status - bez zmian.
    assert a["stabilny"]["dsr"] == pytest.approx(b["stabilny"]["dsr"], rel=1e-3)


def test_KANAREK_bez_kotwicy_okna_przesuwaja_sie_z_zegarem():
    """Bez kotwicy ta sama para przebiegow liczy INNE okna - to byla wada."""
    _, okna_a = _przebieg(_plik_do(KONIEC - timedelta(days=1)), None)
    _, okna_b = _przebieg(_plik_do(KONIEC), None)
    assert okna_a != okna_b


def test_przesuniecia_musza_zawierac_zero():
    with pytest.raises(ValueError, match="0"):
        tr.train(None, "crypto", DNI - 10, 2, H4, "spot_eu", sucho=True, wybor="ic",
                 panel=plik_syntetyczny(), przesuniecia=(-1, 1))


def test_przesuniecie_ujemne_ma_zapas_panelu():
    """`--days` = caly plik i przesuniecie -2: zabraknie 2 dni panelu - odmowa,
    a nie cicho krotsze pierwsze okno."""
    with pytest.raises(SystemExit, match="poza plik"):
        tr.train(None, "crypto", DNI, 2, H4, "spot_eu", sucho=True, wybor="ic",
                 panel=plik_syntetyczny(), od=tr.KOTWICA_SIATKI,
                 przesuniecia=tr.PRZESUNIECIA_SIATKI)


def test_wynik_podaje_min_srednia_max_i_status_z_minimum():
    wynik, _ = _przebieg(plik_syntetyczny(), tr.KOTWICA_SIATKI, tr.PRZESUNIECIA_SIATKI)
    assert set(wynik["przesuniecia"]) == {"-2", "-1", "+0", "+1", "+2"}
    lo, sr, hi = wynik["stabilny"]["dsr"]
    assert lo <= sr <= hi
    assert lo == min(w["dsr"] or 0.0 for w in wynik["przesuniecia"].values())
    statusy = {w["status"] for w in wynik["przesuniecia"].values()}
    assert wynik["status"] == ("validated" if statusy == {"validated"} else
                               "degraded" if statusy == {"degraded"} else "learning")
    # Przesuniecie 0 to przebieg glowny - te same liczby co na gorze.
    assert wynik["przesuniecia"]["+0"]["okna"] == wynik["okna"]


# ------------------------------------------------------------ CLI i chart

def _main(monkeypatch, *argv):
    zapis = {}
    monkeypatch.setattr(tr, "train", lambda *a, **k: zapis.update(k) or {})
    monkeypatch.setattr(tr.panel_plik, "wczytaj", lambda p: {
        "engine": "crypto", "bar": "4h", "dni": 10, "do": KONIEC})
    monkeypatch.setattr(sys, "argv", ["train.py", "--panel", "x", *argv])
    tr.main()
    return zapis


def test_cli_domyslnie_kotwica_i_piec_przesuniec(monkeypatch):
    k = _main(monkeypatch, "--hipoteza", "H-1")
    assert k["od"] == tr.KOTWICA_SIATKI
    assert k["przesuniecia"] == tr.PRZESUNIECIA_SIATKI


def test_cli_od_teraz_to_siatka_sprzed_T4(monkeypatch):
    k = _main(monkeypatch, "--hipoteza", "H-1", "--od", "teraz", "--przesuniecia", "0")
    assert k["od"] is None and k["przesuniecia"] == (0,)


def test_cli_inna_kotwica_albo_przesuniecia_wymagaja_sucho(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nie-uzywane")
    for argv in (["--od", "2023-01-01"], ["--przesuniecia", "0"], ["--od", "teraz"]):
        monkeypatch.setattr(sys, "argv", ["train.py", *argv])
        with pytest.raises(SystemExit, match="--sucho"):
            tr.main()


def test_chart_trenera_podaje_kotwice_rowna_stalej_kodu():
    korzen = Path(__file__).resolve().parents[1]
    values = (korzen / "charts/trader/values.yaml").read_text(encoding="utf-8")
    szablon = (korzen / "charts/trader/templates/trainer-cron.yaml").read_text(encoding="utf-8")
    m = re.search(r'^trainerCron:.*?^  od: "(\d{4}-\d{2}-\d{2})"', values, re.S | re.M)
    assert m, "trainerCron.od nie ustawione"
    assert m.group(1) == f"{tr.KOTWICA_SIATKI:%Y-%m-%d}"
    assert "--od {{ $.Values.trainerCron.od | quote }}" in szablon


# ------------------------------------------------------------ horyzont IC

@pytest.mark.parametrize("bar", sorted(HORIZONS))
def test_horyzont_IC_znaczy_12_godzin_na_kazdym_horyzoncie(bar):
    h = HORIZONS[bar]
    assert h.ic_horizon_bars * h.bar_minutes == 12 * 60


def test_4h_zachowuje_dawne_3_swiece():
    """Tor produkcyjny bez zmiany wag: 3 swiece 4h = 12 h jak dotad."""
    assert get_horizon("4h").ic_horizon_bars == 3


@pytest.mark.parametrize("horyzont_etykiety", [None, 6], ids=["z_Horizon", "z_flagi"])
def test_trener_podaje_ic_weights_horyzont_z_Horizon(monkeypatch, horyzont_etykiety):
    """Od 2026-10-07 (H-6, H-7) szpieg przyjmuje tez `tylko_dluga` - swiadomie:
    trener podaje ja dla rynku bez shortow - i sprawdza, ze `--horyzont-etykiety`
    dochodzi do `ic_weights` zamiast `Horizon.ic_horizon_bars`."""
    widziane = []
    strony = []
    prawdziwe = tr.ic_weights

    def szpieg(panels, cache, martwe, horyzont_swiec=3, tylko_dluga=False):
        widziane.append(horyzont_swiec)
        strony.append(tylko_dluga)
        return prawdziwe(panels, cache, martwe, horyzont_swiec, tylko_dluga=tylko_dluga)

    monkeypatch.setattr(tr, "ic_weights", szpieg)
    with contextlib.redirect_stdout(io.StringIO()):
        tr.train(None, "crypto", DNI, 2, H4, "spot_eu", Egzekucja(limit=True),
                 sucho=True, wybor="ic", panel=plik_syntetyczny(),
                 horyzont_etykiety=horyzont_etykiety)
    assert widziane and set(widziane) == {horyzont_etykiety or H4.ic_horizon_bars}
    assert set(strony) == {True}, "spot_eu nie ma shortow - wagi IC tylko dla longa"


def test_horyzont_IC_inny_niz_3_trafia_do_params():
    """1h dostaje 12 swiec zamiast 3 - inna hipoteza, wiec inne `params` (N)."""
    p1 = tr.params_przebiegu(get_horizon("1h"), 60, "ic", "spot_eu", True, Egzekucja())
    p4 = tr.params_przebiegu(H4, 60, "ic", "spot_eu", True, Egzekucja())
    assert p1["ic_horizon_bars"] == 12
    assert "ic_horizon_bars" not in p4
    assert p4["progi_na_wagach"] == "ic"
    assert "progi_na_wagach" not in tr.params_przebiegu(H4, 60, "argmax", "spot_eu",
                                                        True, Egzekucja())
