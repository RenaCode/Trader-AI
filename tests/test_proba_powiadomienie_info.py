"""Próba pożarowa kill-switcha nie produkuje alarmów krytycznych.

CO BYŁO ZŁE (audyt Fable F-5.2, 2026-10-03)

Codzienna próba (05:20) kończyła się powiadomieniem `killswitch.triggered`
o severity `critical` — tym samym kodem i tą samą wagą co prawdziwe zadziałanie
bezpiecznika. W dzwonku leżało 15 nieprzeczytanych „krytycznych" z ośmiu dni
prób; prawdziwy kill-switch utonąłby wśród nich.

Teraz skutek próby idzie kodem `killswitch.proba` (migracja 0073) z severity
`info`. Ogniwo 4 próby nadal ma co zmierzyć — powiadomienie powstaje — ale nie
udaje alarmu.

CZEGO TO NIE ZMIENIA: zdarzenie próby jest nieodróżnialne od prawdziwego spadku
kapitału w tym samym oknie (migracja 0045). Dlatego zdarzenie, które przeżyje
okno próby, musi od razu dostać zwykły alarm krytyczny — powiadomienie
`killswitch.proba` nie może go tłumić jako „już zgłoszone".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.engine.dozor import sprawdz_ryzyko

KORZEN = Path(__file__).resolve().parents[1]
TERAZ = datetime(2026, 10, 3, 3, 30, tzinfo=timezone.utc)


class Atrapa:
    """Odtwarza `WHERE event_code ...` zapytania o historię zgłoszeń — bo tu
    właśnie siedzi pytanie, czy powiadomienie o próbie tłumi prawdziwy alarm."""

    def __init__(self, zdarzenie, zgloszone_kody=()):
        self.zdarzenie = zdarzenie
        self.zgloszone_kody = list(zgloszone_kody)

    async def fetch_all(self, sql, params=None):
        if "risk.killswitch_event" in sql:
            return [self.zdarzenie]
        if "risk.limit_def" in sql:
            return []
        if "risk.limit_usage" in sql:
            return [{"engine": "crypto", "mode": "demo", "wiek_s": 60,
                     "nigdy": None, "status": "running",
                     "puls_silnika_min": 1, "puls_dozorcy_min": 0}]
        if "notify.notification" in sql:
            if "risk.pomiar_zamilkl" in sql:
                return [{"od_zgloszenia_min": None}]
            pasuja = [k for k in self.zgloszone_kody if f"'{k}'" in sql]
            return [{"od_zgloszenia_min": 5 if pasuja else None}]
        if 'app."user"' in sql:
            return [{"id": 1}]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")


def _zdarzenie(proba: bool) -> dict:
    return {"id": 26, "engine": "crypto", "mode": "demo",
            "reason": "Kapitał 100063.09 spadł do progu 100563.00",
            "triggered_at": TERAZ, "wiek_min": 3,
            "proba_w_toku": proba,
            "proba_do": TERAZ + timedelta(minutes=20) if proba else None}


def _uruchom(atrapa):
    wyslane = []

    async def notify(user_id, code, tytul, tresc, **kw):
        wyslane.append({"code": code, **kw})

    kod = asyncio.run(sprawdz_ryzyko(atrapa.fetch_all, notify))
    return kod, wyslane


def test_proba_NIE_tworzy_powiadomienia_krytycznego():
    kod, wyslane = _uruchom(Atrapa(_zdarzenie(proba=True)))
    assert wyslane, "ogniwo 4 próby: powiadomienie MA powstać"
    assert [w["code"] for w in wyslane] == ["killswitch.proba"]
    assert all(w["severity"] == "info" for w in wyslane), (
        "skutek próby oznaczony jako critical — tonie w nim prawdziwy alarm")
    assert kod == 0


def test_prawdziwy_killswitch_nadal_krytyczny():
    kod, wyslane = _uruchom(Atrapa(_zdarzenie(proba=False)))
    assert [w["code"] for w in wyslane] == ["killswitch.triggered"]
    assert wyslane[0]["severity"] == "critical"
    assert kod == 1


def test_powiadomienie_o_probie_NIE_tlumi_alarmu_po_wygasnieciu_okna():
    """Zdarzenie, które przeżyło okno próby, mogło być prawdziwym spadkiem
    kapitału. Dostało w oknie tylko `info` — więc pierwszy przebieg po oknie
    musi wysłać `critical`, a nie uznać sprawy za zgłoszoną."""
    kod, wyslane = _uruchom(Atrapa(_zdarzenie(proba=False),
                                   zgloszone_kody=["killswitch.proba"]))
    assert [w["code"] for w in wyslane] == ["killswitch.triggered"]
    assert wyslane[0]["severity"] == "critical"
    assert kod == 1


def test_drugi_przebieg_w_oknie_proby_milczy():
    """Rytm w oknie próby liczy własne powiadomienia próby — inaczej czujka
    co 10 min dokładałaby kolejne `info` przez całe 28 minut okna."""
    kod, wyslane = _uruchom(Atrapa(_zdarzenie(proba=True),
                                   zgloszone_kody=["killswitch.proba"]))
    assert wyslane == []
    assert kod == 0


def test_ogniwo_4_proby_liczy_powiadomienie_o_probie():
    """`proba_killswitcha` szuka zgłoszenia własnego zdarzenia; po zmianie
    kodu musi szukać także pod `killswitch.proba`, inaczej każda próba
    oblewa ogniwo 4."""
    zrodlo = (KORZEN / "services/engine/proba_killswitcha.py").read_text(
        encoding="utf-8")
    assert "'killswitch.proba'" in zrodlo


def test_typ_zdarzenia_jest_w_migracji_jako_info():
    """`notify` rzuca `ValueError` na nieznany kod — typ musi być w bazie."""
    sql = "\n".join(p.read_text(encoding="utf-8")
                    for p in sorted((KORZEN / "db/migrations").glob("*.sql")))
    assert "'killswitch.proba'" in sql
    blok = sql[sql.index("'killswitch.proba'"):][:1200]
    assert "'info'" in blok
