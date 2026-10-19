"""Poprawki z recenzji PR #42 (regula beta, D-3): B2, B3, P1, P3.

  * B2 - limity 97% z migracji 0100 zostaja w bazie, gdy ktos przywroci
    ensemble. Strategia inna niz beta na equity/demo przy `max_total_exposure`
    > 15 pct_equity ma WSTRZYMANE WEJSCIA (`wstrzymanie_ryzykiem`), zamiast
    grac 9,7% na pozycje i do 97% lacznie.
  * B3 - 0100 konczy sie blokiem `DO`, ktory wywraca migracje, gdy po
    UPDATE-ach limity equity/demo nie sa dokladnie tymi piecioma wartosciami.
  * P1 - czujka `dozor --co beta`: hold `historia_filtra_niepelna` dluzej niz
    jedna sesje to alarm, nie cicha gotowka.
  * P3 - tor zamkniety blokuje wejscia modelu, a nie alokacje beta.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from services.engine import dozor, loop, tory
from services.engine.risk_rules import DEFAULT_LIMITS
from tests import test_run_once as _petla
from tests import test_strategia_beta as _beta
from tests.atrapa_bazy import KORZEN, MIGRACJE
from tests.test_run_once import cykl, plany

swiat = _petla.swiat
beta = _beta.beta
H1 = _beta.H1

PLIK_0100 = KORZEN / "db/migrations/0100_strategia_beta.sql"
TEST_DB_URL = os.environ.get("TEST_DB_URL")

LIMITY_BETA = {
    **DEFAULT_LIMITS,
    "max_total_exposure": {"value": D("97"), "unit": "pct_equity", "action": "block"},
    "max_position_notional": {"value": D("97"), "unit": "pct_equity",
                              "action": "downsize"},
}


# ------------------------------------------------------------ B2

@pytest.mark.parametrize("strategia,engine,mode,limity,oczekiwane", [
    ("model", "equity", "demo", LIMITY_BETA, True),
    ("momentum", "equity", "demo", LIMITY_BETA, True),
    ("beta", "equity", "demo", LIMITY_BETA, False),
    ("model", "equity", "demo", DEFAULT_LIMITS, False),     # 15 - granica dozwolona
    ("model", "crypto", "demo", LIMITY_BETA, False),
    ("model", "equity", "live", LIMITY_BETA, False),
    ("model", "equity", "demo",
     {"max_total_exposure": {"value": D("15.01"), "unit": "pct_equity"}}, True),
])
def test_limity_beta_poza_beta(strategia, engine, mode, limity, oczekiwane):
    powod = loop.limity_beta_poza_beta(strategia, engine, mode, limity)
    assert bool(powod) is oczekiwane, powod
    if powod:
        assert "max_total_exposure" in powod and "0100" in powod


def test_ENSEMBLE_na_limitach_beta_WSTRZYMANY_a_nie_9_7_procent(swiat, monkeypatch):  # noqa: F811
    """KANAREK B2: bez straznika ensemble wchodzi z budzetem 97%/2 kapitalu."""
    monkeypatch.setattr(_petla, "DEFAULT_LIMITS", LIMITY_BETA)
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, cur = cykl(swiat, engine="equity")

    d = {x["iid"]: x for x in swiat.decyzje}
    assert d[1]["action"] == "hold" and d[1]["powod_holdu"] == "wstrzymanie_ryzykiem"
    assert d[1]["verdict"] == "block"
    assert plany(cur) == [] and stats["wejscia"] == 0


def test_ENSEMBLE_na_starych_limitach_WCHODZI(swiat):  # noqa: F811
    """Kontrola kanarka: te same dane przy 15% - wejscie idzie."""
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, cur = cykl(swiat, engine="equity")

    d = {x["iid"]: x for x in swiat.decyzje}
    assert d[1]["action"] == "enter_long" and stats["wejscia"] == 1


def test_BETA_na_limitach_beta_WCHODZI(beta, monkeypatch):  # noqa: F811
    monkeypatch.setattr(_petla, "DEFAULT_LIMITS", LIMITY_BETA)
    stats, cur = _beta.cykl_beta(beta)

    (d,) = beta.decyzje
    assert d["action"] == "enter_long" and stats["wejscia"] == 1


# ------------------------------------------------------------ P3

@pytest.fixture
def equity_zamkniete(monkeypatch):
    monkeypatch.setitem(tory.ZAMKNIETE, ("equity", "1h", "stock"), tory.Zamkniecie(
        hipoteza="H-13", data="2026-10-25", powod="test"))


def test_tor_equity_zamkniety_BETA_wchodzi(beta, equity_zamkniete):  # noqa: F811
    stats, cur = _beta.cykl_beta(beta, kind_kosztow="stock")

    (d,) = beta.decyzje
    assert d["action"] == "enter_long" and d["powod_holdu"] is None
    assert stats["tor_zamkniety"] == 0 and stats["wejscia"] == 1


def test_tor_equity_zamkniety_ENSEMBLE_hold_tor_zamkniety(swiat, equity_zamkniete):  # noqa: F811
    swiat.wyniki = {1: 0.6, 2: 0.05}
    stats, cur = cykl(swiat, engine="equity", h=H1, kind_kosztow="stock")

    d = {x["iid"]: x for x in swiat.decyzje}
    assert d[1]["action"] == "hold" and d[1]["powod_holdu"] == "tor_zamkniety"
    assert stats["tor_zamkniety"] == 1 and plany(cur) == []


def test_equity_NIE_jest_dzis_zamkniety():
    assert tory.zamkniecie("equity", "1h", "stock") is None


# ------------------------------------------------------------ B3

def _blok_weryfikacji() -> str:
    tresc = PLIK_0100.read_text(encoding="utf-8")
    (blok,) = re.findall(r"^DO \$\$.*?^\$\$;", tresc, flags=re.S | re.M)
    return blok


def test_0100_konczy_sie_WERYFIKACJA_pieciu_limitow():
    tresc = PLIK_0100.read_text(encoding="utf-8")
    blok = _blok_weryfikacji()
    assert tresc.index(blok) > tresc.rindex("UPDATE risk.limit_def")
    assert "RAISE EXCEPTION" in blok and "<> 5" in blok
    for oczekiwane in ("('max_total_exposure',    97,    'pct_equity')",
                       "('max_position_notional', 97,    'pct_equity')",
                       "('max_daily_loss',        10,    'pct')",
                       "('max_drawdown',          40,    'pct')",
                       "('min_equity',            60000, 'quote_ccy')"):
        assert oczekiwane in blok


@pytest.mark.skipif(not TEST_DB_URL, reason="ustaw TEST_DB_URL na jednorazową bazę")
def test_BAZA_0100_idempotentna_i_wywraca_sie_na_zlym_limicie():
    import psycopg
    from psycopg.rows import dict_row
    tresc = PLIK_0100.read_text(encoding="utf-8")
    blok = _blok_weryfikacji()
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row, autocommit=True) as conn:
        conn.execute(tresc)
        conn.execute(tresc)
        wiersze = conn.execute(
            "SELECT name, value, unit FROM risk.limit_def"
            " WHERE engine = 'equity' AND mode = 'demo' AND scope = 'engine'"
            "   AND name IN ('max_total_exposure', 'max_position_notional',"
            "                'max_daily_loss', 'max_drawdown', 'min_equity')"
            " ORDER BY name").fetchall()
        assert [(w["name"], D(w["value"]), w["unit"]) for w in wiersze] == [
            ("max_daily_loss", 10, "pct"), ("max_drawdown", 40, "pct"),
            ("max_position_notional", 97, "pct_equity"),
            ("max_total_exposure", 97, "pct_equity"), ("min_equity", 60000, "quote_ccy")]
        typ = conn.execute("SELECT always_on FROM notify.event_type"
                           " WHERE code = 'engine.beta_bez_historii'").fetchone()
        assert typ and typ["always_on"]

    psucia = [
        "UPDATE risk.limit_def SET value = 41 WHERE engine = 'equity'"
        " AND mode = 'demo' AND scope = 'engine' AND name = 'max_drawdown'",
        "UPDATE risk.limit_def SET unit = 'quote_ccy' WHERE engine = 'equity'"
        " AND mode = 'demo' AND scope = 'engine' AND name = 'max_total_exposure'",
        "DELETE FROM risk.limit_def WHERE engine = 'equity'"
        " AND mode = 'demo' AND scope = 'engine' AND name = 'min_equity'",
        "UPDATE risk.limit_def SET enabled = false WHERE engine = 'equity'"
        " AND mode = 'demo' AND scope = 'engine' AND name = 'max_daily_loss'",
    ]
    for psucie in psucia:
        with psycopg.connect(TEST_DB_URL) as conn:
            try:
                conn.execute(psucie)
                with pytest.raises(psycopg.errors.RaiseException, match="0100"):
                    conn.execute(blok)
            finally:
                conn.rollback()


# ------------------------------------------------------------ P1

class _Stend:
    def __init__(self, dni, juz=False):
        self.dni, self.juz = dni, juz
        self.wolania: list[tuple] = []
        self.zapytania: list[tuple] = []

    async def fetch_all(self, sql, params=None):
        self.zapytania.append((" ".join(sql.split()), params))
        if "journal.decision" in sql:
            return [{"dzien": d, "ile": 3,
                     "ostatnia": datetime(d.year, d.month, d.day, 15, tzinfo=timezone.utc)}
                    for d in self.dni]
        if "notify.notification" in sql:
            return [{"?column?": 1}] if self.juz else []
        if 'app."user"' in sql:
            return [{"id": 1}]
        return []

    async def notify(self, user_id, event_code, tytul, tresc, **kw):
        self.wolania.append((event_code, tytul, tresc, kw))
        return len(self.wolania)


def _uruchom(stend, **kw) -> int:
    return asyncio.run(dozor.sprawdz_beta_bez_historii(stend.fetch_all, stend.notify, **kw))


def test_czujka_beta_JEDNA_sesja_to_cisza():
    stend = _Stend([date(2026, 11, 2)])
    assert _uruchom(stend) == 0 and stend.wolania == []


def test_czujka_beta_brak_serii_to_cisza():
    stend = _Stend([])
    assert _uruchom(stend) == 0 and stend.wolania == []


def test_czujka_beta_DWIE_sesje_ALARM():
    stend = _Stend([date(2026, 11, 2), date(2026, 11, 3)])
    assert _uruchom(stend) == 1
    ((kod, tytul, tresc, kw),) = stend.wolania
    assert kod == "engine.beta_bez_historii" and kw["severity"] == "critical"
    assert "2 sesji" in tytul and "historia_filtra_niepelna" in tresc
    sql, params = stend.zapytania[0]
    assert params["strategia"] == "beta_filtr_trendu" and params["symbol"] == "SPY"
    assert params["powod"] == "historia_filtra_niepelna"
    assert "IS DISTINCT FROM" in sql, "seria musi sie zerowac po decyzji z innym powodem"


def test_czujka_beta_w_tlumieniu_zielona_ale_z_logiem(capsys):
    stend = _Stend([date(2026, 11, 2), date(2026, 11, 3)], juz=True)
    assert _uruchom(stend) == 0 and stend.wolania == []
    assert "TRWAŁY" in capsys.readouterr().out


def test_czujka_beta_powtorka_ROSNIE():
    assert dozor.odstep_przypomnienia_beta(2) == 24
    assert dozor.odstep_przypomnienia_beta(dozor.BETA_SERIA_DLUGA_SESJI) == 72
    stend = _Stend([date(2026, 11, d) for d in (2, 3, 4, 5, 6)], juz=True)
    _uruchom(stend)
    tlumienie = [p for s, p in stend.zapytania if "notify.notification" in s]
    assert tlumienie == [(72,)]


def test_czujka_beta_sucho_bez_powiadomienia():
    stend = _Stend([date(2026, 11, 2), date(2026, 11, 3)])
    assert _uruchom(stend, sucho=True) == 1 and stend.wolania == []


def test_czujka_beta_stale_zgodne_z_regula():
    from services.engine import strategia_beta as sb
    assert dozor.BETA_STRATEGIA == sb.NAZWA
    assert dozor.BETA_POWOD_HISTORIA == sb.POWOD_HISTORIA
    assert dozor.BETA_SYMBOL == sb.SYMBOL


def test_czujka_beta_typ_zdarzenia_w_migracji_i_w_chartcie():
    tresc = "\n".join(p.read_text(encoding="utf-8") for p in MIGRACJE)
    assert "'engine.beta_bez_historii'" in tresc
    szablon = (KORZEN / "charts/trader/templates/zywotnosc-cron.yaml").read_text()
    assert "dozor.py --co beta --engine equity --mode demo" in szablon
