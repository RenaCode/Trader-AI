"""Bramka schematu w CronJobach (przegląd 2026-10-04, T-15).

CronJob z nowym obrazem na starym schemacie (migracja padła albo wisi) kończy
się kodem 3 i wierszem w `risk.job_run`, a polecenie nie rusza. Fail-open,
gdy nie da się sprawdzić. Czujka `trader-zywotnosc` idzie mimo to.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.engine import przebieg_zadania as pz

KORZEN = Path(__file__).resolve().parents[1]


def _bramka(monkeypatch, rozjazd):
    zapisane, uruchomione = [], []
    monkeypatch.setattr(pz, "czekaj_na_baze", lambda: True)
    monkeypatch.setattr(pz, "sprawdz_schemat", lambda: rozjazd)
    monkeypatch.setattr(pz, "zapisz", lambda n, t, k, **_: zapisane.append((n, k)) or True)

    class Proc:
        def __init__(self, polecenie):
            uruchomione.append(polecenie)

        def wait(self):
            return 0

        def send_signal(self, s):
            pass

    monkeypatch.setattr(pz.subprocess, "Popen", Proc)
    return zapisane, uruchomione


def test_schemat_starszy_kod_3_wiersz_i_polecenie_NIE_rusza(monkeypatch, capsys):
    zapisane, uruchomione = _bramka(monkeypatch, "baza ma schemat 0092_x, obraz oczekuje 0093_y")
    assert pz.main(["--nazwa", "proba-killswitcha", "--", "true"]) == pz.KOD_STARY_SCHEMAT == 3
    assert zapisane == [("proba-killswitcha", 3)] and uruchomione == []
    assert "NIE URUCHOMIONE" in capsys.readouterr().err


def test_schemat_rowny_kod_polecenia(monkeypatch):
    zapisane, uruchomione = _bramka(monkeypatch, None)
    assert pz.main(["--nazwa", "kurs", "--", "true"]) == 0
    assert uruchomione == [["true"]] and zapisane == [("kurs", 0)]


def test_czujka_idzie_MIMO_starego_schematu(monkeypatch, capsys):
    zapisane, uruchomione = _bramka(monkeypatch, "baza ma schemat 0092_x, obraz 0093_y")
    assert pz.main(["--nazwa", "zywotnosc", "--mimo-starego-schematu", "--", "true"]) == 0
    assert uruchomione == [["true"]]
    assert "idzie mimo to" in capsys.readouterr().err


def test_brak_uprawnien_albo_bazy_to_FAIL_OPEN(monkeypatch, tmp_path, capsys):
    (tmp_path / "0093_y.sql").write_text("")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@127.0.0.1:9/z")
    assert pz.sprawdz_schemat(tmp_path) is None
    assert "bez bramki" in capsys.readouterr().err
    monkeypatch.delenv("DATABASE_URL")
    assert pz.sprawdz_schemat(tmp_path) is None


def test_najnowsza_migracja_z_obrazu():
    assert pz.najnowsza_w_obrazie() == sorted(
        p.stem for p in (KORZEN / "db/migrations").glob("*.sql"))[-1]


@pytest.mark.skipif(not os.environ.get("TEST_DB_URL"), reason="ustaw TEST_DB_URL")
def test_BAZA_zmigrowana_przechodzi_a_obraz_nowszy_nie(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DB_URL"])
    assert pz.sprawdz_schemat() is None
    (tmp_path / "9999_przyszla.sql").write_text("")
    assert "9999_przyszla" in pz.sprawdz_schemat(tmp_path)


def test_chart_tylko_czujka_ma_wyjatek():
    szablony = (KORZEN / "charts/trader/templates").glob("*.yaml")
    z_wyjatkiem = [p.name for p in szablony if "--mimo-starego-schematu" in p.read_text()]
    assert z_wyjatkiem == ["zywotnosc-cron.yaml"]

