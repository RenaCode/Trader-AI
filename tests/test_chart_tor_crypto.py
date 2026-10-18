"""Wylaczenie toru crypto jednym przelacznikiem - bez falszywych alarmow i bez
porzuconych pozycji (2026-10-18, sprzatanie po B-0 = STOP).

TRZY RZECZY, KTORYCH TE TESTY PILNUJA

1. `torCrypto.enabled: false` zdejmuje CALY potok crypto (Deploymenty, trening,
   post-mortem, odswiezanie instrumentow, probe kill-switcha, czujki toru)
   i NIE DOTYKA niczego, czego potrzebuje tor equity - w szczegolnosci skanera
   wiadomosci, z ktorego equity bierze ceche `news_sent`.

2. Proces wylaczony w chartcie NIE JEST dla czujek martwy. Jego wiersz w
   `risk.engine_heartbeat` / `risk.dozorca_heartbeat` / `risk.worker_heartbeat`
   zostaje ze statusem `running`, wiec bez listy z chartu (`--workery-z-chartu`)
   `trader-zywotnosc` wysylalby `engine.down` o silniku, dozorcy i trzech
   workerach, ktorych nikt nie uruchamia. KANAREK: te same wiersze bez listy
   MUSZA alarmowac - inaczej test bylby zaspokojony czujka, ktora milczy zawsze.

3. Warunek przelaczenia jest widoczny w logu silnika: linia podsumowania cyklu
   na torze zamknietym mowi "DOMYKANIE: ..." albo "TOR DOMKNIĘTY"
   (`loop.opis_domykania`), a liczby biora sie z bazy (`loop.SQL_DOMYKANIE`).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = KORZEN / "charts/trader"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))

# Deploymenty toru crypto - DOKLADNIE te. Equity i skaner wiadomosci poza lista.
POTOK_CRYPTO = {"market", "history", "features", "engine", "dozorca"}

helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


def _render(*ustaw: str) -> list[dict]:
    argv = ["helm", "template", "trader", str(CHART)]
    for u in ustaw:
        argv += ["--set", u]
    w = subprocess.run(argv, capture_output=True, text=True)
    assert w.returncode == 0, w.stderr
    return [d for d in yaml.safe_load_all(w.stdout) if d]


def _nazwy(docs, rodzaj):
    return {d["metadata"]["name"] for d in docs if d["kind"] == rodzaj}


def _kontener(docs, rodzaj, nazwa):
    d = next(d for d in docs if d["kind"] == rodzaj and d["metadata"]["name"] == nazwa)
    spec = d["spec"]
    pod = (spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
           or spec["template"]["spec"])
    return pod["containers"][0]


def _skrypt(docs, nazwa):
    return "\n".join(_kontener(docs, "CronJob", nazwa)["command"])


@pytest.fixture(scope="module")
def wlaczony():
    return _render()


@pytest.fixture(scope="module")
def wylaczony():
    return _render("torCrypto.enabled=false")


# ------------------------------------------------------------- stan w tym PR

def test_w_tym_PR_tor_crypto_NADAL_chodzi():
    """Silnik crypto moze miec otwarte pozycje i zlecenia na Bybit demo.

    Wylaczenie go razem z ta zmiana zostawiloby je bez wyjscia. Przelacznik
    przestawia sie osobnym commitem, po linii "TOR DOMKNIĘTY" w logu silnika.
    """
    assert VALUES["torCrypto"]["enabled"] is True
    oznaczone = {n for n, w in VALUES["workers"].items() if w.get("torCrypto")}
    assert oznaczone == POTOK_CRYPTO, oznaczone


def test_post_mortem_crypto_wylaczony_juz_teraz_a_equity_zostaje():
    """Post-mortem tylko CZYTA zamkniete transakcje i placi za LLM - dla toru
    zamknietego wynikiem hipotezy to koszt bez odbiorcy. Pozycji nie dotyka."""
    assert VALUES["postmortemCron"]["enabled"] is True
    assert VALUES["postmortemCron"]["silniki"] == ["equity"]


def test_skaner_wiadomosci_ZOSTAJE_bo_equity_bierze_z_niego_ceche():
    """`features-equity` liczy `news_sent` z `news.article_analysis`, a sygnal
    i rezim `news_shock` sa wspolne dla obu silnikow. Wylaczenie skanera
    zostawiloby equity z luka w cesze, ktorej model nie widzial w treningu."""
    from services.engine.signals import REGIMES, SIGNALS
    assert "news_shock" in SIGNALS and "news_shock" in REGIMES
    zrodlo = (KORZEN / "services/ingest/features.py").read_text(encoding="utf-8")
    assert '"news_sent"' in zrodlo and "compute_news(" in zrodlo
    news = VALUES["workers"]["news"]
    assert news["enabled"] is True and not news.get("torCrypto")
    assert "equity" in news["silnikiZTorow"]


# ------------------------------------------------------------- render chartu

@helm
def test_RENDER_domyslny_ma_caly_potok_crypto(wlaczony):
    dep = _nazwy(wlaczony, "Deployment")
    assert {f"trader-{n}" for n in POTOK_CRYPTO} <= dep
    assert "trader-proba-killswitcha" in _nazwy(wlaczony, "CronJob")
    news = _kontener(wlaczony, "Deployment", "trader-news")["command"]
    assert news[-2:] == ["--silniki", "crypto,equity"], news


@helm
def test_RENDER_wylaczony_tor_zdejmuje_potok_crypto(wylaczony):
    dep = _nazwy(wylaczony, "Deployment")
    assert not ({f"trader-{n}" for n in POTOK_CRYPTO} & dep), dep
    assert "trader-proba-killswitcha" not in _nazwy(wylaczony, "CronJob")
    news = _kontener(wylaczony, "Deployment", "trader-news")["command"]
    assert news[-2:] == ["--silniki", "equity"], news


@helm
def test_RENDER_tor_equity_identyczny_w_obu_stanach(wlaczony, wylaczony):
    """Przelacznik crypto nie moze zmienic ANI JEDNEGO procesu equity."""
    def equity(docs):
        return {d["metadata"]["name"]: d["spec"]["template"]["spec"]["containers"]
                for d in docs if d["kind"] == "Deployment"
                and d["metadata"]["name"].endswith("-equity")}
    a, b = equity(wlaczony), equity(wylaczony)
    assert set(a) == {"trader-market-equity", "trader-history-equity",
                      "trader-features-equity", "trader-engine-equity",
                      "trader-dozorca-equity", "trader-kapital-equity"}, set(a)
    assert a == b


@helm
def test_RENDER_trener_post_mortem_instrumenty_bez_crypto(wlaczony, wylaczony):
    tw, tx = _skrypt(wlaczony, "trader-trainer"), _skrypt(wylaczony, "trader-trainer")
    assert "--engine crypto" in tw and '--kind "spot_eu"' in tw
    assert "--engine crypto" not in tx and '--kind "spot_eu"' not in tx, tx
    assert "--engine equity" in tx and "--kod-trenera $RC_EQUITY" in tx

    for docs in (wlaczony, wylaczony):
        pm = _skrypt(docs, "trader-postmortem")
        assert '--engine "equity"' in pm and '--engine "crypto"' not in pm, pm

    iw = _skrypt(wlaczony, "trader-instrumenty")
    ix = _skrypt(wylaczony, "trader-instrumenty")
    assert '--engine "crypto"' in iw and '--engine "equity"' in iw
    assert '--engine "crypto"' not in ix and '--engine "equity"' in ix, ix


@helm
def test_RENDER_czujki_bez_toru_crypto_i_z_lista_procesow(wlaczony, wylaczony):
    zw, zx = _skrypt(wlaczony, "trader-zywotnosc"), _skrypt(wylaczony, "trader-zywotnosc")
    for wywolanie in ('--engine "crypto"', "uzgodnienie.py", "mozliwosci.py",
                      "proba-killswitcha"):
        assert wywolanie in zw, wywolanie
        assert wywolanie not in zx, wywolanie

    def lista(skrypt, co):
        linia = next(x for x in skrypt.splitlines() if f"--co {co} " in x)
        return set(linia.split("--workery-z-chartu ")[1].split()[0].strip('"').split(","))

    for co in ("zywotnosc", "workery"):
        w, x = lista(zw, co), lista(zx, co)
        assert POTOK_CRYPTO <= w, w
        assert not (POTOK_CRYPTO & x), x
        assert w - x == POTOK_CRYPTO
        assert {"news", "engine-equity", "dozorca-equity", "features-equity"} <= x


@helm
def test_RENDER_networkpolicy_bez_wylaczonych_workerow(wylaczony):
    db = next(d for d in wylaczony if d["kind"] == "NetworkPolicy"
              and d["metadata"]["name"] == "trader-db")
    tekst = yaml.safe_dump(db)
    assert "trader-engine-equity" in tekst
    assert "- trader-engine\n" not in tekst and "- trader-market\n" not in tekst, tekst


# --------------------------------------------------------- czujki pulsu (dozor)

def _stend(engine_rows, dozorca_rows, worker_rows=()):
    poszly = []
    teraz = datetime.now(timezone.utc)

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        if "notify.notification" in p:
            return []
        if "risk.limit_def" in p:
            return [{"engine": "crypto", "mode": "demo"},
                    {"engine": "equity", "mode": "demo"}]
        if "SELECT nazwa FROM risk.dozorca_heartbeat" in p:
            return [{"nazwa": r[0]} for r in dozorca_rows]
        if "risk.engine_heartbeat" in p:
            wiersze = [{"engine": e, "etykieta": e, "mode": "demo",
                        "status": "running", "last_beat_at": teraz - timedelta(minutes=m),
                        "wiek_min": m} for e, m in engine_rows]
            wiersze += [{"engine": None, "etykieta": n, "mode": "nadzor",
                         "status": "running", "last_beat_at": teraz - timedelta(minutes=m),
                         "wiek_min": m} for n, m in dozorca_rows]
            return wiersze
        if "risk.worker_heartbeat" in p:
            return [{"worker": w, "interwal_s": 300, "last_beat_at": teraz - timedelta(seconds=s),
                     "awarii_pod_rzad": 0, "ostatni_blad": None,
                     "wiek_s": s, "proba_wiek_s": s} for w, s in worker_rows]
        if "is_active" in p:
            return [{"id": 1}]
        raise AssertionError("atrapa nie zna zapytania: " + p)

    async def notify(user_id, kod, tytul, tresc, **kw):
        poszly.append((kod, tytul))
        return 1

    return fetch_all, notify, poszly


# Stan bazy dzien po `torCrypto.enabled: false`: wiersze crypto zostaly
# z `running` i pulsem sprzed trzech godzin, equity bije.
SILNIKI = [("crypto", 180), ("equity", 2)]
DOZORCY = [("dozorca-ryzyka:crypto:demo", 180), ("dozorca-ryzyka:equity:demo", 1)]
WORKERY = [("market", 10800), ("history", 10800), ("features", 10800),
           ("market-equity", 60), ("news", 120)]
Z_CHARTU = {"engine-equity", "dozorca-equity", "market-equity", "history-equity",
            "features-equity", "kapital-equity", "news"}


def test_KANAREK_bez_listy_z_chartu_wylaczony_tor_KRZYCZY():
    """Dokladnie stan, w ktorym czujka bez tej poprawki wysylala alarmy."""
    from services.engine.dozor import sprawdz_puls_workerow, sprawdz_zywotnosc
    fa, nt, poszly = _stend(SILNIKI, DOZORCY, WORKERY)
    assert asyncio.run(sprawdz_zywotnosc(fa, nt)) != 0
    assert {t for _, t in poszly} == {
        "Silnik crypto przestał odpowiadać",
        "Proces nadzoru dozorca-ryzyka:crypto:demo przestał odpowiadać"}, poszly
    fa, nt, poszly = _stend(SILNIKI, DOZORCY, WORKERY)
    assert asyncio.run(sprawdz_puls_workerow(fa, nt)) != 0
    assert {t for _, t in poszly} == {f"Worker {w} zamilkł"
                                      for w in ("market", "history", "features")}


def test_z_lista_z_chartu_wylaczony_tor_jest_cisza():
    from services.engine.dozor import sprawdz_puls_workerow, sprawdz_zywotnosc
    fa, nt, poszly = _stend(SILNIKI, DOZORCY, WORKERY)
    assert asyncio.run(sprawdz_zywotnosc(fa, nt, workery=Z_CHARTU)) == 0
    fa, nt, poszly2 = _stend(SILNIKI, DOZORCY, WORKERY)
    assert asyncio.run(sprawdz_puls_workerow(fa, nt, workery=Z_CHARTU)) == 0
    assert poszly == [] and poszly2 == []


def test_lista_z_chartu_NIE_wycisza_procesu_wlaczonego():
    """Filtr jest po NAZWIE procesu, nie po torze: martwy silnik equity przy
    wylaczonym crypto nadal jest awaria."""
    from services.engine.dozor import sprawdz_puls_workerow, sprawdz_zywotnosc
    fa, nt, poszly = _stend([("crypto", 180), ("equity", 60)], DOZORCY, WORKERY)
    assert asyncio.run(sprawdz_zywotnosc(fa, nt, workery=Z_CHARTU)) != 0
    assert [t for _, t in poszly] == ["Silnik equity przestał odpowiadać"]
    fa, nt, poszly = _stend(SILNIKI, DOZORCY, WORKERY + [("news", 10800)])
    asyncio.run(sprawdz_puls_workerow(fa, nt, workery=Z_CHARTU))
    assert [t for _, t in poszly] == ["Worker news zamilkł"], poszly


def test_brakujacy_dozorca_wylaczonego_toru_nie_jest_oczekiwany():
    from services.engine.dozor import DOZORCA_PULS, brakujacy_dozorcy
    pary = [{"engine": "crypto", "mode": "demo"}, {"engine": "equity", "mode": "demo"}]
    assert brakujacy_dozorcy(pary, []) == [f"{DOZORCA_PULS}:crypto:demo",
                                           f"{DOZORCA_PULS}:equity:demo"]
    assert brakujacy_dozorcy(pary, [], Z_CHARTU) == [f"{DOZORCA_PULS}:equity:demo"]


def test_nazwy_procesow_zgodne_z_kluczami_workers():
    """Mapowanie wiersz pulsu -> klucz `workers` musi trafiac w values.yaml,
    inaczej filtr nie pominie niczego (albo pominie nie to)."""
    from services.engine.dozor import proces_dozorcy, proces_pulsu
    for e in ("crypto", "equity"):
        assert proces_pulsu("engine", e) in VALUES["workers"]
        assert proces_dozorcy(f"dozorca-ryzyka:{e}:demo") in VALUES["workers"]
    assert proces_dozorcy("cos-innego:crypto:demo") is None


def test_flaga_z_chartu_przechodzi_przez_parser(monkeypatch):
    """Pusty napis to pusta lista (filtr wszystkiego), brak flagi - bez filtra."""
    import sys
    from services.engine import dozor
    zlapane = {}

    async def zywotnosc(fa, nt, sucho=False, prog_min=None, workery=None):
        zlapane["workery"] = workery
        return 0

    async def nic(*a, **kw):
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://nikt@localhost:1/nic")
    monkeypatch.setattr(dozor, "sprawdz_zywotnosc", zywotnosc)
    import services.api.db as api_db
    monkeypatch.setattr(api_db, "open_pool", nic)
    monkeypatch.setattr(api_db, "close_pool", nic)
    for argv, oczekiwane in ((["--co", "zywotnosc"], None),
                             (["--co", "zywotnosc", "--workery-z-chartu", "a,b"], {"a", "b"}),
                             (["--co", "zywotnosc", "--workery-z-chartu", ""], set())):
        monkeypatch.setattr(sys, "argv", ["dozor.py", *argv])
        assert asyncio.run(dozor.main()) == 0
        assert zlapane["workery"] == oczekiwane, argv


# ------------------------------------------------- warunek przelaczenia (loop)

def test_opis_domykania():
    from services.engine.loop import opis_domykania
    assert opis_domykania(None) == ""
    zero = opis_domykania({"pozycje": 0, "plany": 0, "zlecenia": 0})
    assert "TOR DOMKNIĘTY" in zero and "torCrypto.enabled: false" in zero
    for k in ("pozycje", "plany", "zlecenia"):
        stan = {"pozycje": 0, "plany": 0, "zlecenia": 0, k: 1}
        opis = opis_domykania(stan)
        assert "DOMYKANIE" in opis and "NIE wyłączać" in opis, (k, opis)
        assert "DOMKNIĘTY" not in opis


DB = os.environ.get("TEST_DB_URL")


@pytest.mark.skipif(not DB, reason="ustaw TEST_DB_URL na jednorazowa baze")
def test_stan_domykania_liczy_z_prawdziwej_bazy():
    """Atrapa nie wykona SQL-a: Postgres musi przyjac zapytanie, a otwarta
    pozycja musi zmienic liczbe (KANAREK: zapytanie zwracajace zawsze zero
    meldowaloby "TOR DOMKNIĘTY" przy pozycji na rachunku)."""
    import psycopg
    from psycopg.rows import dict_row
    from services.engine.loop import stan_domykania
    with psycopg.connect(DB, row_factory=dict_row) as c:
        cur = c.cursor()
        try:
            przed = stan_domykania(cur, "crypto", "demo")
            inny_przed = stan_domykania(cur, "equity", "demo")
            cur.execute("SELECT id FROM ref.instrument WHERE engine = 'crypto' LIMIT 1")
            iid = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO exec.position (engine, mode, instrument_id, side, qty,"
                " avg_price, opened_at) VALUES ('crypto','demo',%s,'long',1,1,now())",
                (iid,))
            po = stan_domykania(cur, "crypto", "demo")
            inny = stan_domykania(cur, "equity", "demo")
        finally:
            c.rollback()
    assert po["pozycje"] == przed["pozycje"] + 1
    assert inny == inny_przed, "pozycja crypto policzona na rachunku equity"
    assert set(po) == {"pozycje", "plany", "zlecenia"}
