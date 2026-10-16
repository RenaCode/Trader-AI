"""Demo gra MODELEM-KANDYDATEM, live wyłącznie wersją `active` (decyzja 2026-10-07).

Właściciel zdecydował 2026-10-07: demo służy do trenowania algorytmu przed
live, więc silnik demo przestaje grać regułą momentum (R4, pomiar egzekucji
E-1) i gra NAJNOWSZĄ wersją `candidate` (albo nowszą `active`), żeby zbierać
dane w przód (renacode-infra: docs/projekty/trader/hipotezy.md, D-1). Bramka przejścia na live bez zmian.

Ten plik pilnuje obu połówek tej decyzji naraz:
  * WYBÓR: najnowsza wersja `candidate`/`active`, nigdy `training`/`retired`;
    jej id ląduje w `journal.decision.model_version_id`,
  * BRAK WYCIEKU NA LIVE: odmowa w `main` (start), w `run_once` i w samej
    funkcji ładującej `active_model` — trzy niezależne miejsca, bo każde
    z nich da się wywołać z pominięciem pozostałych.
"""
from __future__ import annotations

import asyncio

import pytest
import yaml

from services.engine import loop
from services.engine.horizons import get as get_horizon
from tests.atrapa_bazy import KORZEN
from tests.test_run_once import cykl, swiat  # noqa: F401  (fikstura)

H = get_horizon("4h")


class KursorWersji:
    """Atrapa `learn.model_version` dla JEDNEGO modelu: wykonuje filtr statusu
    i `ORDER BY version DESC LIMIT 1` z zapytania `active_model`."""

    def __init__(self, wersje):
        self.wersje = wersje
        self.zapytania: list[tuple] = []
        self._wynik = None

    def execute(self, sql, params=None):
        self.zapytania.append((sql, params))
        nazwa, statusy = params
        assert nazwa == "crypto_signal_weighting_4h"
        pasujace = [w for w in self.wersje if w["status"] in statusy]
        self._wynik = max(pasujace, key=lambda w: w["version"], default=None)

    def fetchone(self):
        return self._wynik


def wersja(id_, numer, status, wagi=True):
    return {"id": id_, "version": numer, "status": status,
            "params": {"weights": {"range": {"momentum": 1.0}}} if wagi else {}}


# ------------------------------------------------------------ reguła trybu

@pytest.mark.parametrize("tryb", ["live", None, "paper", ""])
def test_kandydat_POZA_DEMO_to_odmowa(tryb):
    with pytest.raises(ValueError, match="tylko z --mode demo"):
        loop.kandydat_tylko_na_demo("candidate", tryb)


def test_aktywna_wersja_wolna_w_kazdym_trybie_a_nieznany_status_to_blad():
    for tryb in ("demo", "live", None):
        loop.kandydat_tylko_na_demo("active", tryb)
    with pytest.raises(ValueError, match="nieznany status"):
        loop.kandydat_tylko_na_demo("retired", "demo")


# ------------------------------------------------------------ funkcja ładująca

@pytest.mark.parametrize("tryb", ["live", None])
def test_active_model_ODMAWIA_kandydata_poza_demo_ZANIM_zapyta_baze(tryb):
    """Domyślne `mode=None` NIE jest demo: wywołanie, które zapomniało trybu,
    kończy się odmową, a nie kandydatem."""
    kur = KursorWersji([wersja(9, 171, "candidate")])
    with pytest.raises(ValueError, match="tylko z --mode demo"):
        loop.active_model(kur, "crypto", H, status="candidate", mode=tryb)
    assert kur.zapytania == []


def test_kandydat_to_NAJNOWSZA_wersja_candidate():
    kur = KursorWersji([wersja(1, 160, "active"), wersja(2, 165, "candidate"),
                        wersja(3, 171, "candidate"), wersja(4, 150, "retired")])
    m = loop.active_model(kur, "crypto", H, status="candidate", mode="demo")
    assert (m["version"], m["model_version_id"], m["status"]) == (171, 3, "candidate")


def test_kandydat_ustepuje_NOWSZEJ_aktywnej():
    """Po zatwierdzonej promocji najnowszy model JEST aktywny — demo nie może
    się wtedy cofnąć do starszego kandydata."""
    kur = KursorWersji([wersja(2, 165, "candidate"), wersja(5, 172, "active")])
    m = loop.active_model(kur, "crypto", H, status="candidate", mode="demo")
    assert (m["version"], m["status"]) == (172, "active")


def test_kandydat_nigdy_training_ani_retired():
    kur = KursorWersji([wersja(2, 165, "candidate"), wersja(6, 180, "training"),
                        wersja(7, 179, "retired")])
    m = loop.active_model(kur, "crypto", H, status="candidate", mode="demo")
    assert m["version"] == 165
    (_, (_, statusy)), = kur.zapytania
    assert sorted(statusy) == ["active", "candidate"]


def test_brak_kandydata_to_zalozenia_poczatkowe_bez_wersji():
    kur = KursorWersji([wersja(7, 179, "retired")])
    m = loop.active_model(kur, "crypto", H, status="candidate", mode="demo")
    assert m["version"] is None and m["model_version_id"] is None
    assert m["weights"] is None


def test_status_active_czyta_TYLKO_aktywna_takze_gdy_kandydat_nowszy():
    """Zachowanie live sprzed decyzji 07.10, bez zmian — i bez trybu."""
    kur = KursorWersji([wersja(1, 160, "active"), wersja(3, 171, "candidate")])
    m = loop.active_model(kur, "crypto", H)
    assert (m["version"], m["status"]) == (160, "active")
    (_, (_, statusy)), = kur.zapytania
    assert list(statusy) == ["active"]


def test_status_active_a_baza_zwraca_kandydata_to_WYJATEK():
    """Drugi bezpiecznik, na wyniku: gdyby ktoś poluzował warunek SQL,
    kandydat na live ma się wywrócić, a nie handlować."""
    class Zepsuty(KursorWersji):
        def execute(self, sql, params=None):
            self._wynik = wersja(3, 171, "candidate")

    with pytest.raises(RuntimeError, match="przy żądaniu 'active'"):
        loop.active_model(Zepsuty([]), "crypto", H, status="active", mode="live")


# ------------------------------------------------------------ run_once

def test_run_once_ODMAWIA_kandydata_na_live():
    class NieDotykac:
        def cursor(self):
            raise AssertionError("run_once dotknął bazy przed odmową")

    with pytest.raises(ValueError, match="tylko z --mode demo"):
        asyncio.run(loop.run_once(NieDotykac(), "crypto", "live", h=H,
                                  verbose=False, model_status="candidate"))


def test_run_once_kandydat_i_momentum_wykluczaja_sie():
    class NieDotykac:
        def cursor(self):
            raise AssertionError("run_once dotknął bazy przed odmową")

    with pytest.raises(ValueError, match="nie czyta modelu"):
        asyncio.run(loop.run_once(NieDotykac(), "crypto", "demo", h=H,
                                  verbose=False, strategia="momentum",
                                  model_status="candidate"))


def _model_z_bazy(wolania, id_=171):
    def active_model(cur, engine, h, status="active", mode=None):
        wolania.append((status, mode))
        loop.kandydat_tylko_na_demo(status, mode)
        return {"weights": {"range": {"momentum": 1.0}}, "enter": loop.ENTER_THRESHOLD,
                "exit": loop.EXIT_THRESHOLD, "bramki": loop.bramki_z_modelu(None),
                "min_conf": loop.MIN_CONFIDENCE, "version": 71,
                "model_version_id": id_, "status": status}
    return active_model


def test_decyzja_demo_NOSI_id_wersji_kandydata(swiat, monkeypatch):  # noqa: F811
    wolania, zapisane = [], []
    monkeypatch.setattr(loop, "active_model", _model_z_bazy(wolania))
    stary = loop.record_decision

    def decyzja(cur, engine, mode, iid, now, action, qty, *reszta, **kw):
        zapisane.append((mode, reszta[9]))
        return stary(cur, engine, mode, iid, now, action, qty, *reszta, **kw)

    monkeypatch.setattr(loop, "record_decision", decyzja)
    stats, _ = cykl(swiat, model_status="candidate", wymagaj_modelu=True)

    assert set(wolania) == {("candidate", "demo")}
    assert zapisane and all(z == ("demo", 171) for z in zapisane), zapisane
    assert (stats["model_status"], stats["model_version_id"]) == ("candidate", 171)


def test_LIVE_pyta_wylacznie_o_aktywna(swiat, monkeypatch):  # noqa: F811
    """Brak wycieku: ścieżka live (domyślny status) woła `active_model`
    WYŁĄCZNIE ze statusem `active`."""
    wolania = []
    monkeypatch.setattr(loop, "active_model", _model_z_bazy(wolania, id_=160))
    from tests.test_run_once import INSTRUMENTY, Kursor, Polaczenie
    conn = Polaczenie(Kursor([dict(i) for i in INSTRUMENTY]))
    stats = asyncio.run(loop.run_once(conn, "crypto", "live", h=H, verbose=False,
                                      wymagaj_modelu=True, limit=True))
    assert wolania and {s for s, _ in wolania} == {"active"}
    assert stats["model_status"] == "active"


def test_brak_kandydata_z_wymagaj_modelu_to_cykl_bez_decyzji(swiat, capsys):  # noqa: F811
    from tests.test_run_once import INSTRUMENTY, Kursor, Polaczenie
    conn = Polaczenie(Kursor([dict(i) for i in INSTRUMENTY]))
    stats = asyncio.run(loop.run_once(conn, "crypto", "demo", h=H, verbose=True,
                                      model_status="candidate", wymagaj_modelu=True))
    assert stats["bez_modelu"] and stats["model_status"] == "candidate"
    assert swiat.decyzje == []
    assert "BRAK MODELU-KANDYDATA" in capsys.readouterr().out


# ------------------------------------------------------------ przeładowanie

def test_zmiana_modelu_loguje_start_i_nowy_trening_a_noc_gieldy_nie():
    st, msg = loop.zmiana_modelu(None, {"model_status": "candidate",
                                        "model_version_id": 3, "model_wersja": 171})
    assert "wersja 171 (id 3)" in msg and "PRZEŁADOWANY" not in msg
    st2, msg = loop.zmiana_modelu(st, {"model_status": "candidate",
                                       "model_version_id": 3, "model_wersja": 171})
    assert st2 == st and msg is None
    # Bramka sesji: model nieczytany — to nie jest zmiana modelu.
    st3, msg = loop.zmiana_modelu(st, loop.nowe_stats(poza_sesja=True))
    assert st3 == st and msg is None
    st4, msg = loop.zmiana_modelu(st, {"model_status": "candidate",
                                       "model_version_id": 8, "model_wersja": 172})
    assert "MODEL PRZEŁADOWANY" in msg and "171 (id 3) -> wersja 172 (id 8)" in msg
    _, msg = loop.zmiana_modelu(st4, loop.nowe_stats(bez_modelu=True,
                                                     model_status="candidate"))
    assert "-> brak modelu" in msg


# ------------------------------------------------------------ start procesu

@pytest.mark.parametrize("argv,komunikat", [
    (["--model-status", "candidate", "--mode", "live"], "tylko z --mode demo"),
    (["--model-status", "candidate", "--strategia", "momentum"], "wykluczają się"),
])
def test_main_ODMAWIA_startu(monkeypatch, argv, komunikat):
    monkeypatch.setattr("sys.argv", ["loop.py", *argv])
    monkeypatch.setenv("DATABASE_URL", "postgresql://nie-wolno-sie-polaczyc")
    with pytest.raises(SystemExit, match=komunikat):
        asyncio.run(loop.main())


# ------------------------------------------------------------ chart

def _komendy():
    workers = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text())["workers"]
    return {n: " ".join(w.get("command", [])) for n, w in workers.items()}


def test_chart_kandydat_TYLKO_w_silnikach_demo():
    komendy = _komendy()
    z_kandydatem = {n for n, k in komendy.items() if "--model-status candidate" in k}
    assert z_kandydatem == {"engine", "engine-equity"}
    for n in z_kandydatem:
        k = komendy[n]
        assert "--mode demo" in k and "--mode live" not in k, n
        assert "--wymagaj-modelu" in k, n
        assert "--strategia" not in k, n
        # Kontrola przed startem czyta TO SAMO, czym handluje.
        assert k.count("--model-status candidate") == 2, n
    assert not any("--mode live" in k for k in komendy.values())
