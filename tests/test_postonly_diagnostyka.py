"""Diagnostyka odrzuceń PostOnly: zegar giełdy i DRUGI odczyt księgi po wysyłce.

Odrzucenie PostOnly zapada w silniku giełdy w chwili PRZYJĘCIA zlecenia, a my
dowiadujemy się o nim dopiero przy rozstrzyganiu, w następnym cyklu. W `raw`
był do 2026-10-03 tylko odczyt SPRZED wysyłki (`arkusz_wysylki`), więc dwie
przyczyny o przeciwnych lekarstwach dawały identyczny wiersz: „cena była zła
już przy liczeniu" i „rynek przeszedł przez naszą cenę w trakcie wysyłki".

Ten plik pilnuje trzech rzeczy:
  1. drugi odczyt idzie PO POST-cie, pod własnym kluczem, i NIGDY nie wywraca
     egzekucji - awaria odczytu to zapis informacji, nie wyjątek,
  2. cena i ilość zlecenia są te same co bez diagnostyki,
  3. przy rozstrzyganiu zapisuje się zegar giełdy (`createdTime`/`updatedTime`)
     z tej samej odpowiedzi `stan_zlecenia`, bez nowego zapytania.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal as D

from services.engine.gielda_exec import arkusz_po_wysylce
from services.exchanges.base import ExchangeError, StanZlecenia
from services.exchanges.bybit import _ms_gieldy, _powod_odmowy
from tests.test_gielda_exec import _rozstrzygnij
from tests.test_slad_arkusza_w_raw import Gielda, arkusz, blok, plan, zloz


def _dopiski_raw(cur) -> list[dict]:
    """Słowniki doklejane do `exec.order.raw` (`raw = raw || %s`) - z dokładnie
    tych danych, które poszłyby do bazy."""
    out = []
    for s, p in cur.sql:
        if "raw = raw ||" in s:
            out.extend(json.loads(x) for x in p if isinstance(x, str)
                       and x.startswith("{"))
    return out


class _DrugiOdczytPada(Gielda):
    """Pierwszy odczyt (przed wysyłką) działa, drugi (po POST-cie) pada
    wyjątkiem SPOZA rodziny `ExchangeError` - najgorszy przypadek."""

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        ksiega = await super().fetch_order_book(symbol, depth,
                                                ksiega_egzekucji=ksiega_egzekucji)
        if len(self.pytania_o_arkusz) > 1:
            raise RuntimeError("połączenie zerwane po wysyłce")
        return ksiega


def test_awaria_drugiego_odczytu_NIE_wywraca_egzekucji_i_zostaje_nazwana():
    g = _DrugiOdczytPada(ksiega=arkusz("14.092", "14.093"), odniesienie=D("14.092"))
    cur, ok, msg = zloz(plan(pid=64, symbol="LINKUSDC", strona="buy",
                             cena_planu="14.127", qty="81.112055"), gielda=g)
    assert ok is True, msg
    b = blok(cur)
    assert b["po_wysylce"]["bid"] is None
    assert "po wysyłce nie powiódł się" in b["po_wysylce"]["powod_braku"]
    # Slad sprzed wysyłki nietknięty.
    assert b["bid"] == "14.092" and b["powod_braku"] is None


def test_diagnostyka_NIE_zmienia_ceny_ani_ilosci_zlecenia():
    zwykla = Gielda(ksiega=arkusz("14.092", "14.093"), odniesienie=D("14.092"))
    psujaca = _DrugiOdczytPada(ksiega=arkusz("14.092", "14.093"),
                               odniesienie=D("14.092"))
    for g in (zwykla, psujaca):
        zloz(plan(pid=64, symbol="LINKUSDC", strona="buy", cena_planu="14.127",
                  qty="81.112055"), gielda=g)
    assert zwykla.zlozone == psujaca.zlozone
    assert len(zwykla.zlozone) == 1


def test_odczyt_po_wysylce_ZAWSZE_oddaje_slownik():
    class Pada:
        async def fetch_order_book(self, *a, **k):
            raise ExchangeError("404")

    class PadaInaczej:
        async def fetch_order_book(self, *a, **k):
            raise ValueError("zepsuta odpowiedź")

    for adapter in (Pada(), PadaInaczej()):
        slad = asyncio.run(arkusz_po_wysylce(adapter, "SOLUSDC", "spot"))
        assert slad["bid"] is None and slad["powod_braku"]
        json.dumps(slad)


def test_zegar_gieldy_parsowany_bez_wyjatkow():
    assert _ms_gieldy("1759490000123") == 1759490000123
    assert _ms_gieldy(None) is None and _ms_gieldy("") is None
    assert _ms_gieldy("nie-liczba") is None


def test_odrzucenie_PostOnly_zapisuje_zegar_gieldy_przy_rozstrzygnieciu():
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="rejected",
                        ilosc=D("10"), wypelniono=D("0"), srednia_cena=None,
                        prowizja=D("0"),
                        powod_odmowy=_powod_odmowy("EC_PostOnlyWillTakeLiquidity"),
                        utworzono_ms=1759490000000, zaktualizowano_ms=1759490000042)
    cur, _, rodzaj, _ = _rozstrzygnij(stan)
    assert rodzaj == "brak"
    dopiski = _dopiski_raw(cur)
    assert dopiski, cur.sql
    czas = dopiski[-1]["czas_gieldy"]
    assert czas["ms_od_przyjecia"] == 42
    assert czas["utworzono_at"].startswith("2025-10-03T11:13:20")
    assert dopiski[-1]["powod_odmowy_gieldy"] == "EC_PostOnlyWillTakeLiquidity"


def test_bez_zegara_gieldy_NIE_ma_bloku_czasu():
    """Adapter, który zegara nie podaje (Alpaca), nie dostaje zer udających pomiar."""
    stan = StanZlecenia(id_wlasne="tai-p42", id_gieldy="G1", status="rejected",
                        ilosc=D("10"), wypelniono=D("0"), srednia_cena=None,
                        prowizja=D("0"))
    cur, _, _, _ = _rozstrzygnij(stan)
    dopiski = _dopiski_raw(cur)
    assert dopiski and "czas_gieldy" not in dopiski[-1]


def test_absurdalny_znacznik_gieldy_nie_wywraca_rozstrzygniecia():
    from services.engine.gielda_exec import _iso_z_ms

    assert _iso_z_ms(None) is None
    assert _iso_z_ms(10**20) is None
    assert _iso_z_ms("x") is None
    assert _iso_z_ms(1_759_000_000_000).startswith("2025-")
