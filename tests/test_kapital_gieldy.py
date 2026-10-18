"""Punkt krzywej kapitalu z ksiag gieldy - ta sama miara co krzywa krypto."""
from datetime import datetime, timezone
from decimal import Decimal as D

from services.engine.kapital_gieldy import zapisz_punkt
from tests.atrapa_bazy import Kursor

TERAZ = datetime(2026, 9, 22, 14, 30, 17, tzinfo=timezone.utc)


def test_punkt_z_konta_liczy_nav_i_realized_TA_SAMA_regula_co_krypto():
    # przeplywy: brak wierszy cash_flow -> start 100 000 jak seed_equity dla demo
    cur = Kursor([{"wplaty": 0, "units": 0}])
    w = zapisz_punkt(cur, "equity", "demo",
                     {"equity": D("100250.5"), "cash": D("60000"), "ekspozycja": D("40250.5")},
                     unrealized=D("250.5"), as_of=TERAZ)
    assert w["nav"] == D("100250.5") / D(100000)
    assert w["realized"] == D("0"), "equity - wplaty - unrealized = 0 przy samym niezrealizowanym"
    sql, params = next((s, p) for s, p in cur.sql if "INSERT INTO exec.equity_point" in s)
    # Bez przycinania do minuty: punkt z 14:30:17 przycięty do 14:30:00 był
    # o 17 s „starszy" przy progu wieku pomiaru (`risk.prog_wieku_pomiaru`).
    assert params[2] == TERAZ, "event_time przycięty — pomiar wygląda na starszy, niż jest"
    assert params[3] == D("100250.5") and params[7] == D("40250.5")
    assert "ON CONFLICT (engine, mode, event_time) DO NOTHING" in sql


def test_tryb_live_bez_przeplywow_NIE_zgaduje_kapitalu():
    import pytest
    cur = Kursor([{"wplaty": 0, "units": 0}])
    with pytest.raises(RuntimeError):
        zapisz_punkt(cur, "equity", "live", {"equity": D(1), "cash": D(1), "ekspozycja": D(0)},
                     unrealized=D(0), as_of=TERAZ)


# ---------------------------------------------------------------------------
# BRAK POMIARU NIEZREALIZOWANEGO P&L
#
# `cykl()` liczylo `unrealized = Decimal(0)` i podmienialo je tylko wtedy, gdy
# adapter mial `pozycje_szczegoly` - czyli BRAK POMIARU szedl dalej jako POMIAR
# ROWNY ZERU. Ma go wylacznie `AlpacaAdapter`; `BybitAdapter` nie ma, a
# `--engine` przyjmuje dowolny napis bez `choices=`, wiec `--engine crypto`
# dawalo adapter Bybita i ciche zero.
#
# Zero nie jest tu ostrozne, tylko klamliwe: `realized = equity - wplaty -
# unrealized`, wiec zerowe `unrealized` ksieguje caly zysk otwartych pozycji
# jako ZREALIZOWANY, a kafelek "Zrealizowany P&L" pokazuje pieniadze, ktorych
# nie ma i ktore rynek moze zabrac.

import asyncio  # noqa: E402

import pytest  # noqa: E402

from services.engine.kapital_gieldy import ZGADNIETA_ODCHYLKA_MAX, zmierz_unrealized  # noqa: E402


class AdapterBezPomiaru:
    """Jak BybitAdapter: `stan_konta` jest, `pozycje_szczegoly` NIE MA."""

    async def stan_konta(self):
        return {"equity": D("101200"), "cash": D("60000"), "ekspozycja": D("41200")}


class AdapterZPomiarem:
    """Jak AlpacaAdapter: pozycje z wycena gieldy."""

    async def pozycje_szczegoly(self):
        return [{"symbol": "AAPL", "unrealized_pl": D("900")},
                {"symbol": "MSFT", "unrealized_pl": D("300")}]


def test_adapter_BEZ_pomiaru_niezrealizowanego_PL_przerywa_zamiast_dac_zero():
    with pytest.raises(RuntimeError) as e:
        asyncio.run(zmierz_unrealized(AdapterBezPomiaru()))
    assert "AdapterBezPomiaru" in str(e.value)
    assert "niezrealizowan" in str(e.value)


def test_adapter_Z_pomiarem_sumuje_niezrealizowany_PL_pozycji():
    assert asyncio.run(zmierz_unrealized(AdapterZPomiarem())) == D("1200")


def test_zero_bylo_KSIEGOWANIEM_zysku_otwartych_pozycji_jako_zrealizowanego():
    # Ta sama sytuacja policzona dwa razy: raz z pomiarem, raz z cichym zerem
    # sprzed poprawki. Roznica NIE jest w kolumnie `unrealized` - jest
    # w `realized_cum`, czyli w liczbie, ktora widzi wlasciciel.
    stan = {"equity": D("101200"), "cash": D("60000"), "ekspozycja": D("41200")}
    z_pomiarem = zapisz_punkt(Kursor([{"wplaty": 0, "units": 0}]), "equity", "demo",
                              stan, unrealized=D("1200"), as_of=TERAZ)
    z_cichym_zerem = zapisz_punkt(Kursor([{"wplaty": 0, "units": 0}]), "equity", "demo",
                                  stan, unrealized=D(0), as_of=TERAZ)
    assert z_pomiarem["realized"] == D("0")
    assert z_cichym_zerem["realized"] == D("1200"), \
        "tyle wlasnie pokazywal kafelek 'Zrealizowany P&L' przy otwartej pozycji"


# ---------------------------------------------------------------------------
# ZGADNIETA WPLATA WOBEC KSIAG GIELDY
#
# Bez wierszy w `exec.cash_flow` bierzemy START_EQUITY = 100 000, czyli kapital
# startowy SYMULACJI krypto, i twierdzimy, ze tyle ma konto paper Alpaki.
# Saldo startowe konta paper ustawia sie przy zakladaniu i zmienia przy kazdym
# resecie (`/v2/account/configurations`), niezaleznie od tego repozytorium -
# wiec zgadniecie musi byc sprawdzone, a nie przyjete na slowo.

def test_reset_konta_paper_w_DOL_przerywa_zamiast_zapisac_pol_rachunku_straty():
    # Ktos resetuje konto paper na 50 000 (typowe po testach egzekucji).
    # Sprzed poprawki: realized = 50 000 - 100 000 = -50 000 i kafelek
    # "Zrealizowany P&L" pokazywal pol rachunku straty, ktorej nikt nie poniosl.
    cur = Kursor([{"wplaty": 0, "units": 0}])
    with pytest.raises(RuntimeError) as e:
        zapisz_punkt(cur, "equity", "demo",
                     {"equity": D("50000"), "cash": D("50000"), "ekspozycja": D(0)},
                     unrealized=D(0), as_of=TERAZ)
    assert "exec.cash_flow" in str(e.value)
    assert not any("INSERT INTO exec.equity_point" in s for s, _ in cur.sql), \
        "punkt krzywej NIE moze trafic do bazy, gdy zgadnieta wplata nie trzyma"


def test_reset_konta_paper_w_GORE_tez_przerywa():
    # Odwrotny reset (200 000) pokazywal 100 000 fikcyjnego zysku.
    with pytest.raises(RuntimeError):
        zapisz_punkt(Kursor([{"wplaty": 0, "units": 0}]), "equity", "demo",
                     {"equity": D("200000"), "cash": D("200000"), "ekspozycja": D(0)},
                     unrealized=D(0), as_of=TERAZ)


def test_zwykly_wynik_handlu_w_granicach_progu_zapisuje_sie_normalnie():
    # Prog ma odrozniac "zgadujemy zle" od "handel poszedl zle". Kill-switch
    # stoi na min_equity = 95 000, czyli 5% ponizej zgadnietej wplaty, wiec
    # wynik miesci sie w progu z ogromnym zapasem.
    granica = D(100000) * ZGADNIETA_ODCHYLKA_MAX
    for equity in (D(100000) + granica - D(1), D(100000) - granica + D(1),
                   D("103500"), D("96500")):
        w = zapisz_punkt(Kursor([{"wplaty": 0, "units": 0}]), "equity", "demo",
                         {"equity": equity, "cash": equity, "ekspozycja": D(0)},
                         unrealized=D(0), as_of=TERAZ)
        assert w["realized"] == equity - D(100000)


def test_prog_NIE_dotyczy_kont_z_zapisanym_przeplywem():
    # Gdy wplata jest w `exec.cash_flow`, niczego nie zgadujemy - i wtedy
    # dowolnie duzy `realized` jest pomiarem, a nie objawem zlego zgadniecia.
    cur = Kursor([{"wplaty": D("50000"), "units": D("50000")}])
    w = zapisz_punkt(cur, "equity", "demo",
                     {"equity": D("120000"), "cash": D("120000"), "ekspozycja": D(0)},
                     unrealized=D(0), as_of=TERAZ)
    assert w["realized"] == D("70000")
    assert any("INSERT INTO exec.equity_point" in s for s, _ in cur.sql)
