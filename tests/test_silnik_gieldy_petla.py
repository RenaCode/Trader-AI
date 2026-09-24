"""Pętla decyzyjna silnika giełdowego: bramka sesji, bramka modelu, spójność chartu.

CZTERY WADY, KTÓRE TE TESTY PILNUJĄ — każda cicha, każda wyglądająca jak spokój:

  1. "NIE WIEM" WZIĘTE ZA "OTWARTE". Zegar giełdy, który nie odpowiedział, nie
     może dać domysłu "pewnie handlujemy" — to zamienia awarię odczytu
     w zlecenia składane po godzinach. `stan_sesji` ma trzeci stan i `None`
     NIE przechodzi przez `wolno_decydowac`.

  2. HANDEL NA ZAŁOŻENIACH POCZĄTKOWYCH. `active_model` przy braku aktywnej
     wersji cofa się do heurystyki i handluje dalej. Dla silnika, który dopiero
     rusza, krzywa kapitału mierzyłaby wtedy heurystykę, a wyglądała jak wynik
     modelu — i nikt by tego po fakcie nie odróżnił.

  3. KRYPTO PRZYKRYWAJĄCE EQUITY W CZUJCE ŚWIEŻOŚCI. Do 2026-09-24
     `sprawdz_swiezosc` brała `max(event_time)` z CAŁEJ tabeli. Krypto dopisuje
     świece co minutę, więc potok equity mógł stać tygodniami, a czujka
     meldowała "bez zastrzeżeń". Zmierzone na produkcji tego samego dnia.

  4. ROZJAZD LISTY HORYZONTÓW. Zdarzył się już raz (2026-09-04:
     `workers.features` kontra `trainerCron.horyzonty`) i kosztował 21 godzin
     martwych cech przy ZEROWYM liczniku restartów. Silnik equity dokłada
     trzecie miejsce z tą samą liczbą, więc rozjazd ma teraz trzy sposoby
     na zajście — i żaden nie objawia się awarią.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
import yaml

from services.engine.koszty import CENNIK, karta_rundy, koszty
from services.engine.sesja import StanSesji, stan_sesji

KORZEN = Path(__file__).resolve().parents[1]
CHART = KORZEN / "charts/trader/values.yaml"


# --------------------------------------------------------------- atrapa kursora
class KursorVenue:
    """Oddaje WYŁĄCZNIE kolumny, które wystąpiły w liście SELECT.

    Atrapa podająca cały wiersz niezależnie od zapytania czyni każdy brak
    kolumny niewykrywalnym: test przechodzi, bo dostał pole, którego prawdziwy
    `SELECT` nigdy by nie przyniósł. Ta klasa wycina odpowiedź do tego, o co
    kod faktycznie poprosił — i dzięki temu literówka w nazwie kolumny albo
    kolumna dopisana w teście, a nie w zapytaniu, kończy się `KeyError`,
    a nie zielonym testem.
    """

    def __init__(self, wiersz: dict | None):
        self._wiersz = wiersz
        self.zapytania: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.zapytania.append((sql, params))
        self._kolumny = re.search(r"SELECT (.+?) FROM", sql, re.S | re.I).group(1)

    def fetchone(self):
        if self._wiersz is None:
            return None
        return {k: v for k, v in self._wiersz.items() if k in self._kolumny}


class ZegarAtrapa:
    def __init__(self, *, otwarta=None, wyjatek=None):
        self._otwarta = otwarta
        self._wyjatek = wyjatek
        self.wolan = 0

    async def zegar(self):
        self.wolan += 1
        if self._wyjatek:
            raise self._wyjatek
        from services.exchanges.alpaca import Zegar
        from datetime import datetime, timezone
        t = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        return Zegar(teraz=t, otwarta=self._otwarta,
                     nastepne_otwarcie=t, nastepne_zamkniecie=t)


# ------------------------------------------------------------------ bramka sesji
def test_rynek_ciagly_nie_pyta_gieldy_o_godziny():
    """`is_24_7` kończy pytanie — i nie kosztuje krypto ani jednego zapytania."""
    cur = KursorVenue({"code": "bybit", "is_24_7": True})
    zegar = ZegarAtrapa(otwarta=False)   # gdyby ktoś go zapytał, powiedziałby NIE
    s = asyncio.run(stan_sesji(cur, "crypto", zegar))
    assert s.wolno_decydowac is True
    assert zegar.wolan == 0, "rynek ciągły nie ma prawa pytać giełdy o sesję"


@pytest.mark.parametrize("otwarta,oczekiwane", [(True, True), (False, False)])
def test_zegar_gieldy_rozstrzyga_sesje(otwarta, oczekiwane):
    cur = KursorVenue({"code": "alpaca", "is_24_7": False})
    s = asyncio.run(stan_sesji(cur, "equity", ZegarAtrapa(otwarta=otwarta)))
    assert s.otwarta is otwarta
    assert s.wolno_decydowac is oczekiwane


def test_zegar_ktory_nie_odpowiedzial_to_nie_jest_otwarcie():
    """WADA 1. Awaria odczytu nie może wyglądać jak zgoda na handel."""
    cur = KursorVenue({"code": "alpaca", "is_24_7": False})
    s = asyncio.run(stan_sesji(cur, "equity",
                               ZegarAtrapa(wyjatek=TimeoutError("brak odpowiedzi"))))
    assert s.otwarta is None
    assert s.wolno_decydowac is False
    assert "brak odpowiedzi" in (s.blad or "")


def test_brak_adaptera_dla_rynku_z_sesjami_to_nie_wiem():
    cur = KursorVenue({"code": "alpaca", "is_24_7": False})
    s = asyncio.run(stan_sesji(cur, "equity", None))
    assert s.otwarta is None and s.wolno_decydowac is False


def test_brak_gieldy_dla_silnika_to_nie_wiem():
    s = asyncio.run(stan_sesji(KursorVenue(None), "equity", ZegarAtrapa(otwarta=True)))
    assert s.otwarta is None and s.wolno_decydowac is False


def test_venue_bierze_sie_tylko_z_gield_z_adapterem():
    """Placeholder `ibkr` z seeda 0008 nie może wygrać z giełdą, na której stoi konto."""
    cur = KursorVenue({"code": "alpaca", "is_24_7": False})
    asyncio.run(stan_sesji(cur, "equity", ZegarAtrapa(otwarta=True)))
    sql, params = cur.zapytania[0]
    assert "code = ANY(" in sql
    from services.exchanges.fabryka import OBSLUGIWANE
    assert list(OBSLUGIWANE) in params


def test_opis_zawsze_mowi_dlaczego():
    """Linia w logu bez powodu jest nieodróżnialna od ciszy."""
    assert "NIEZNANY" in StanSesji(None, "zegar giełdy nie odpowiedział").opis()
    assert "POZA SESJĄ" in StanSesji(False, "zegar giełdy").opis()
    assert "otwarta" in StanSesji(True, "zegar giełdy").opis()


# ----------------------------------------------------------------- bramka modelu
def test_petla_equity_wymaga_modelu_a_krypto_nie():
    """WADA 2. Flaga jest decyzją wpisaną w chart, nie domysłem kodu."""
    workers = yaml.safe_load(CHART.read_text())["workers"]
    equity = " ".join(workers["engine-equity"]["command"])
    crypto = " ".join(workers["engine"]["command"])
    assert "--wymagaj-modelu" in equity
    assert "--wymagaj-modelu" not in crypto, (
        "włączenie tej reguły dla silnika chodzącego na produkcji od miesięcy "
        "byłoby zmianą jego zachowania przy okazji")


def test_run_once_oddaje_powod_blokady_a_nie_same_zera():
    """Cykl bez decyzji musi być odróżnialny od cyklu, który nic nie znalazł."""
    zrodlo = (KORZEN / "services/engine/loop.py").read_text()
    # Obie bramki kończą się pulsem — inaczej `dozor --co zywotnosc` uznałby
    # każdą noc za śmierć silnika equity i wysyłał alarm krytyczny o 22:00.
    for bramka in ("poza_sesja=True", "bez_modelu=True"):
        assert bramka in zrodlo, f"brak bramki {bramka}"
        blok = zrodlo.split(bramka)[1].split("return stats")[0]
        assert "zapisz_puls" in blok, f"bramka {bramka} nie bije pulsu"

    # Puls musi mieć PEŁNY kształt, także w cyklu przerwanym: `detail` jest
    # nadpisywany co przebieg, więc licznik pominięty w gałęzi bramki znikałby
    # z pulsu na całą noc — i "nic nie pominięto" stawałoby się nieodróżnialne
    # od "ta wersja silnika tego nie liczy".
    from services.engine.loop import nowe_stats
    pelny, przerwany = nowe_stats(), nowe_stats(poza_sesja=True)
    assert set(pelny) <= set(przerwany)
    assert all(przerwany[k] == 0 for k in pelny), "bramka nie ma prawa zmyślać liczników"


# -------------------------------------------------------------- cennik equity
def test_akcje_nie_placa_prowizji_bybita():
    """Zaślepka IBKR (0,05%) opisywała rachunek, którego w tym projekcie nie ma."""
    assert koszty("stock").maker == 0.0
    assert koszty("stock").taker == 0.0
    assert CENNIK["etf"] == CENNIK["stock"]


def test_zero_prowizji_nie_udaje_zerowego_kosztu_rundy():
    """Zero w cenniku to PROWIZJA, nie cały rachunek — karta musi to mówić."""
    k = karta_rundy("stock", maker=True)
    nazwy = [s.nazwa for s in k.skladniki]
    assert "oplaty regulacyjne" in nazwy
    assert not k.pelna, "karta bez zmierzonego spreadu nie ma prawa być pełna"
    assert "oplaty regulacyjne" in k.nieznane


def test_krypto_nie_dostalo_skladnika_regulacyjnego():
    assert "oplaty regulacyjne" not in [s.nazwa for s in karta_rundy("spot_eu").skladniki]


# ------------------------------------------------- spójność horyzontów w charcie
def _bar_z_komendy(cmd: list[str]) -> str:
    """WSZYSTKIE `--bar` z komendy, i muszą być zgodne.

    Pierwsza wersja brała `re.search`, czyli PIERWSZE wystąpienie — a komenda
    zawiera je dwa razy: raz w przebiegu kontrolnym (`--sprawdz`), raz w pętli.
    Rozjazd między nimi jest cichy w najgorszy możliwy sposób: kontrola
    sprawdzałaby horyzont, którym silnik nie handluje, i kończyła się zielono
    dokładnie wtedy, gdy nie sprawdziła niczego istotnego.
    """
    bary = {b.strip('"') for b in re.findall(r"--bar (\S+)", " ".join(cmd))}
    assert len(bary) == 1, (
        f"komenda podaje różne `--bar` ({sorted(bary)}) — przebieg kontrolny "
        f"sprawdzałby inny horyzont, niż obstawia pętla")
    return bary.pop()


def test_jeden_horyzont_equity_w_trzech_miejscach():
    """WADA 4. Trzy listy z tą samą liczbą, zero awarii przy rozjeździe."""
    v = yaml.safe_load(CHART.read_text())
    cechy = _bar_z_komendy(v["workers"]["features-equity"]["command"])
    petla = _bar_z_komendy(v["workers"]["engine-equity"]["command"])
    trener = v["trainerCron"]["equity"]["horyzonty"]
    assert cechy == petla, (
        f"pętla equity obstawia {petla}, a potok cech liczy {cechy} — "
        f"silnik nie znajdzie cech i ucichnie bez jednego błędu w logu")
    assert trener == [petla], (
        f"trener equity kalibruje {trener}, a pętla handluje {petla}")


def test_equity_liczy_sie_wlasnym_cennikiem():
    v = yaml.safe_load(CHART.read_text())
    assert v["trainerCron"]["equity"]["kind"] == "stock"
    assert "--kind stock" in " ".join(v["workers"]["engine-equity"]["command"])
    assert v["trainerCron"]["kind"] == "spot_eu", "tor krypto zostaje przy bybit.eu"


def test_equity_handluje_na_gieldzie_w_trybie_demo_jak_krypto():
    """„Oba silniki mają być takie same" — to jest ta część, która ma być taka sama."""
    v = yaml.safe_load(CHART.read_text())
    cmd = " ".join(v["workers"]["engine-equity"]["command"])
    assert "--egzekucja gielda" in cmd and "--mode demo" in cmd
    assert "--limit" in cmd
    assert "--sprawdz" in cmd, "przebieg kontrolny przed pętlą, tak jak w krypto"
