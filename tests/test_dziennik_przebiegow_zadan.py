"""`risk.job_run`: każdy CronJob zapisuje wynik, `dozor --co zadania` go czyta.

CO BYŁO ZŁE (audyt Fable F-8.4, 2026-10-03)

`trader-trainer-29838400` (25.09, kod 2), `trader-postmortem-29848670` (02.10)
i `trader-zywotnosc-29848900` (02.10) padły i były widoczne wyłącznie
w `kubectl get jobs`. Kubernetes nie powiadamia, a historia jobów ginie po
`failedJobsHistoryLimit`.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from services.engine import przebieg_zadania as pz
from services.engine.dozor import (
    BRAK_WIERSZY_PO_DNIACH, ocen_zadania, przebieg_planowy, sprawdz_zadania)

KORZEN = Path(__file__).resolve().parents[1]
TERAZ = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------- opakowanie
def test_opakowanie_oddaje_kod_polecenia_i_zapisuje_go(monkeypatch):
    zapisane = []
    monkeypatch.setattr(pz, "zapisz", lambda n, t, k: zapisane.append((n, k)) or True)
    kod = pz.main(["--nazwa", "kurs", "--", sys.executable, "-c",
                   "import sys; sys.exit(3)"])
    assert kod == 3
    assert zapisane == [("kurs", 3)]


def test_nieudany_zapis_NIE_zmienia_kodu_wyjscia(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pz.main(["--nazwa", "kurs", "--", sys.executable, "-c", "pass"]) == 0
    assert "NIE ZAPISANY" in capsys.readouterr().err


def test_SIGTERM_dochodzi_do_polecenia(tmp_path):
    """Opakowanie jest PID 1 kontenera. Próba pożarowa przywraca próg limitu
    we własnym handlerze SIGTERM — sygnał zatrzymany na opakowaniu zostawiłby
    podniesiony próg."""
    dziecko = tmp_path / "dziecko.py"
    gotowe = tmp_path / "gotowe"
    dziecko.write_text(textwrap.dedent(f"""
        import signal, sys, time, pathlib
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(7))
        pathlib.Path({str(gotowe)!r}).write_text("1")
        time.sleep(30)
    """))
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONPATH"] = str(KORZEN)
    proc = subprocess.Popen(
        [sys.executable, "-B", str(KORZEN / "services/engine/przebieg_zadania.py"),
         "--nazwa", "proba-killswitcha", "--", sys.executable, str(dziecko)],
        env=env, stderr=subprocess.PIPE)
    for _ in range(100):
        if gotowe.exists():
            break
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 7, "sygnał nie doszedł do polecenia"


# ----------------------------------------------------------- czysta ocena
def _p(id_, godzin_temu, kod=0):
    return {"id": id_, "finished_at": TERAZ - timedelta(hours=godzin_temu),
            "exit_code": kod}


DZIENNIK_STARY = TERAZ - timedelta(days=30)


def test_zdrowe_zadanie_to_cisza():
    h = {"trainer": [_p(3, 9), _p(2, 33), _p(1, 57)]}
    assert ocen_zadania(h, TERAZ, DZIENNIK_STARY) == []


def test_ostatni_przebieg_z_kodem_niezerowym_ZGLASZA():
    h = {"postmortem": [_p(9, 6, kod=1), _p(8, 30)]}
    braki = ocen_zadania(h, TERAZ, DZIENNIK_STARY)
    assert [k for k, _ in braki] == ["postmortem:9"]
    assert "kodem 1" in braki[0][1]


def test_cisza_dluzsza_niz_dwa_typowe_odstepy_ZGLASZA():
    """Odstęp z danych: trening dobowy milczący 50 h to cisza."""
    h = {"trainer": [_p(3, 50), _p(2, 74), _p(1, 98)]}
    braki = ocen_zadania(h, TERAZ, DZIENNIK_STARY)
    assert [k for k, _ in braki] == ["trainer:cisza:3"]


def test_odstep_tygodniowy_nie_alarmuje_po_dobie():
    """Harmonogram zmieniony na tygodniowy (F-2.3) nie może zapalić czujki."""
    h = {"trainer": [_p(3, 30), _p(2, 30 + 168), _p(1, 30 + 336)]}
    assert ocen_zadania(h, TERAZ, DZIENNIK_STARY) == []


def test_brak_wierszy_tuz_po_wdrozeniu_to_jeszcze_nie_alarm():
    h = {"backup": []}
    assert ocen_zadania(h, TERAZ, TERAZ - timedelta(days=1)) == []
    assert ocen_zadania(h, TERAZ, None) == []
    stary = TERAZ - timedelta(days=BRAK_WIERSZY_PO_DNIACH + 1)
    assert [k for k, _ in ocen_zadania(h, TERAZ, stary)] == ["backup:brak"]


# ------------------------------------------ planowy a ręczny (poprawka B)
PLANOWY = "trader-trainer-29849700-x7k2q"     # Job z CronJoba: minuta planowa
RECZNY = "trader-trainer-reczny-0410-b8m4z"    # `kubectl create job --from=...`


def test_nazwa_poda_odroznia_przebieg_planowy_od_recznego():
    assert przebieg_planowy({"pod": PLANOWY})
    assert przebieg_planowy({"pod": "trader-backup-29849040-abcde"})
    assert not przebieg_planowy({"pod": RECZNY})
    assert not przebieg_planowy({"pod": "trader-trainer-recznie-1-abcde"})
    # Bez nazwy poda - zachowanie sprzed zmiany: każdy przebieg jak planowy.
    assert przebieg_planowy({"pod": None}) and przebieg_planowy({})


def test_padniety_PLANOWY_zakryty_udanym_RECZNYM_ZGLASZA():
    """04.10: planowy trener padł na `connection refused`, ręczne powtórzenie
    przeszło i czujka patrząca na ostatni wiersz zamilkła. Harmonogram, który
    padł, pójdzie znowu za tydzień — człowiek nie."""
    h = {"trainer": [{**_p(8, 2), "pod": RECZNY},
                     {**_p(7, 5, kod=1), "pod": PLANOWY}]}
    braki = ocen_zadania(h, TERAZ, DZIENNIK_STARY)
    assert [k for k, _ in braki] == ["trainer:planowy:7"]
    opis = braki[0][1]
    assert "PLANOWY" in opis and "kodem 1" in opis
    assert "Ręczne powtórzenie" in opis and RECZNY in opis and "przeszło" in opis


def test_udany_planowy_po_padnietym_to_cisza():
    """Kanarek do poprzedniego: czujka, która zgłasza każdy dawny błąd,
    też przeszłaby tamten test."""
    h = {"trainer": [{**_p(9, 1), "pod": PLANOWY},
                     {**_p(8, 2), "pod": RECZNY},
                     {**_p(7, 5, kod=1), "pod": PLANOWY}]}
    assert ocen_zadania(h, TERAZ, DZIENNIK_STARY) == []


def test_reczny_po_udanym_planowym_to_cisza():
    h = {"trainer": [{**_p(8, 2), "pod": RECZNY},
                     {**_p(7, 5), "pod": PLANOWY}]}
    assert ocen_zadania(h, TERAZ, DZIENNIK_STARY) == []


def test_padniety_reczny_zglasza_jak_dotad():
    h = {"trainer": [{**_p(8, 2, kod=2), "pod": RECZNY},
                     {**_p(7, 5), "pod": PLANOWY}]}
    assert [k for k, _ in ocen_zadania(h, TERAZ, DZIENNIK_STARY)] == ["trainer:8"]


# ----------------------------------------------------------- przebieg
class Atrapa:
    def __init__(self, wiersze, zgloszone=()):
        self.wiersze = wiersze
        self.zgloszone = set(zgloszone)

    async def fetch_all(self, sql, params=None):
        if "row_number()" in sql:
            return self.wiersze
        if "min(finished_at)" in sql:
            return [{"od": DZIENNIK_STARY}]
        if "notify.notification" in sql:
            return [{"x": 1}] if params[0] in self.zgloszone else []
        if 'app."user"' in sql:
            return [{"id": 1}]
        raise AssertionError(sql)


def _uruchom(atrapa, **kw):
    wyslane = []

    async def notify(uid, kod, tytul, tresc, **reszta):
        wyslane.append({"kod": kod, **reszta})

    kod = asyncio.run(sprawdz_zadania(atrapa.fetch_all, notify, teraz=TERAZ,
                                      zadania=["postmortem", "kurs"], **kw))
    return kod, wyslane


def test_nieudane_zadanie_powiadamia_raz():
    wiersze = [{"nazwa": "postmortem", **_p(9, 6, kod=1)},
               {"nazwa": "kurs", **_p(5, 1)}]
    kod, wyslane = _uruchom(Atrapa(wiersze))
    assert kod == 1
    assert [(w["kod"], w["data"]["klucz"]) for w in wyslane] == [
        ("job.nieudany", "postmortem:9")]
    assert wyslane[0]["severity"] == "warning"

    kod, wyslane = _uruchom(Atrapa(wiersze, zgloszone={"postmortem:9"}))
    assert (kod, wyslane) == (0, [])


# ----------------------------------------------------------- wpięcie w chart
bez_helma = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


def _cronjoby():
    wynik = subprocess.run(["helm", "template", str(KORZEN / "charts" / "trader")],
                           capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    return [d for d in yaml.safe_load_all(wynik.stdout)
            if d and d.get("kind") == "CronJob"]


@bez_helma
def test_kazdy_cronjob_zapisuje_przebieg_a_zywotnosc_go_pilnuje():
    nazwy = set()
    lista = None
    for cj in _cronjoby():
        nazwa = cj["metadata"]["name"]
        c = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        polecenie = c["command"]
        if nazwa.startswith("trader-diagnostyka"):
            continue        # zawieszone, uruchamiane ręcznie
        if nazwa == "trader-backup":
            assert "trap zapisz_przebieg EXIT" in polecenie[-1]
            nazwy.add("backup")
            continue
        assert polecenie[:2] == ["python", "services/engine/przebieg_zadania.py"], (
            f"{nazwa}: nie idzie przez przebieg_zadania.py")
        nazwy.add(polecenie[polecenie.index("--nazwa") + 1])
        if nazwa == "trader-zywotnosc":
            skrypt = polecenie[-1]
            linia = next(w for w in skrypt.splitlines() if "--co zadania" in w)
            lista = linia.split("--zadania", 1)[1].split()[0].strip('"').split(",")
    assert lista is not None, "zywotnosc nie woła `--co zadania`"
    assert set(lista) == nazwy - {"zywotnosc"}, (
        f"rozjazd: zadania w czujce {sorted(lista)}, zapisujące {sorted(nazwy)}")


def test_pulapka_kopii_zapisuje_kod_i_go_nie_zmienia(tmp_path):
    """Obraz kopii nie ma naszego kodu — wynik idzie przez `trap` w powłoce.
    Uruchamiamy sam prolog skryptu z atrapą `psql` na PATH."""
    szablon = (KORZEN / "charts/trader/templates/backup.yaml").read_text(encoding="utf-8")
    poczatek = szablon.index("set -eu")
    koniec = szablon.index('ZNACZNIK="$(date')
    # Wyrażenia helma -> dowolny napis; atrapa `psql` zapisuje argumenty.
    prolog = re.sub(r"\{\{.*?\}\}", "X", textwrap.dedent(
        "                  " + szablon[poczatek:koniec]))
    atrapa = tmp_path / "psql"
    slad = tmp_path / "slad"
    atrapa.write_text(f'#!/bin/sh\necho "$@" >> {slad}\n')
    atrapa.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOSTNAME": "pod-1"}
    for koniec_skryptu, oczekiwany in (("false", 1), ("true", 0)):
        slad.unlink(missing_ok=True)
        wynik = subprocess.run(["sh", "-c", prolog + "\n" + koniec_skryptu],
                               env=env, capture_output=True, text=True)
        assert wynik.returncode == oczekiwany, wynik.stderr
        zapis = slad.read_text()
        assert "INSERT INTO risk.job_run" in zapis
        assert f"'backup', now() - make_interval(secs => 0), {oczekiwany}, 'pod-1'" in zapis


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_rola_procesu_dopisuje_wiersz_ale_NIE_podaje_roli():
    import psycopg

    with psycopg.connect(DB) as c:
        c.execute("SET ROLE trader_proba")
        c.execute("INSERT INTO risk.job_run (nazwa, started_at, exit_code)"
                  " VALUES ('proba-killswitcha', now(), 0)")
        c.execute("RESET ROLE")   # proba nie czyta dziennika - tylko dopisuje
        assert c.execute("SELECT rola FROM risk.job_run ORDER BY id DESC LIMIT 1"
                         ).fetchone()[0] == "trader_proba"
        c.rollback()
        c.execute("SET ROLE trader_proba")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO risk.job_run (nazwa, started_at, exit_code, rola)"
                      " VALUES ('trainer', now(), 0, 'trader_silnik')")
        c.rollback()


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_opakowanie_i_czujka_na_postgresie(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB)
    assert pz.zapisz("kurs", 1.5, 0)
    from services.api import db as api_db

    async def przebieg():
        await api_db.open_pool()
        try:
            async def notify(*a, **kw):
                raise AssertionError("świeży, udany przebieg nie powinien alarmować")
            return await sprawdz_zadania(api_db.fetch_all, notify, sucho=True,
                                         zadania=["kurs"])
        finally:
            await api_db.close_pool()

    assert asyncio.run(przebieg()) == 0


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_planowy_padl_reczny_przeszedl_NA_POSTGRESIE(monkeypatch):
    """Ten sam przypadek co wyżej, ale przez prawdziwy SELECT: atrapa oddająca
    `pod` przeszłaby także wtedy, gdyby zapytanie tej kolumny nie pobierało."""
    nazwa = f"test-planowy-{os.getpid()}"
    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("HOSTNAME", f"trader-{nazwa}-29849700-x7k2q")
    assert pz.zapisz(nazwa, 3.0, 1)
    monkeypatch.setenv("HOSTNAME", f"trader-{nazwa}-reczny-b8m4z")
    assert pz.zapisz(nazwa, 3.0, 0)
    from services.api import db as api_db

    async def przebieg():
        await api_db.open_pool()
        try:
            async def notify(*a, **kw):
                raise AssertionError("tryb suchy nie wysyła")
            return await sprawdz_zadania(api_db.fetch_all, notify, sucho=True,
                                         zadania=[nazwa])
        finally:
            await api_db.close_pool()

    assert asyncio.run(przebieg()) == 1
