"""Wypełnienie częściowe — trzeci stan, którego symulacja nie zna."""
from decimal import Decimal as D

import pytest

from services.engine.egzekucja import co_z_wypelnieniem, rozjazd_z_symulacja
from services.engine.zlecenia import Filtry

F = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
           min_wartosc=D("5"), krok_ceny=D("0.01"))


def test_czesciowe_NIE_jest_traktowane_jak_pelne():
    """Pozycja w dzienniku większa niż faktyczna blokuje wejścia, których nic
    nie blokuje — a przy wyjściu sprzedaje ilość, której nie mamy."""
    r = co_z_wypelnieniem(D("4"), D("10"), D("100"), F)
    assert r.rodzaj == "czesciowe"
    assert r.ilosc == D("4")


def test_czesciowe_NIE_jest_traktowane_jak_brak():
    """Zapisanie „nie weszło" o czymś, co weszło, tworzy pozycję-widmo —
    naszą własną ręką."""
    r = co_z_wypelnieniem(D("4"), D("10"), D("100"), F)
    assert r.rodzaj != "brak"
    assert r.ilosc > 0
    assert r.anuluj_reszte is True


def test_wypelnienie_ponizej_minimum_jest_NAZWANE_a_nie_odrzucone():
    """Aktywo jest już nasze niezależnie od tego, co zapiszemy. Odrzucenie tego
    wypełnienia byłoby stworzeniem pozycji-widma, a nie uniknięciem jej."""
    r = co_z_wypelnieniem(D("0.02"), D("10"), D("100"), F)
    assert r.rodzaj == "nie_do_zamkniecia"
    assert r.ilosc == D("0.02")           # przyjmujemy, mimo ze klopotliwe
    assert "NIE DA SIĘ" in r.powod


def test_ulamek_mierzy_zawyzenie_backtestu():
    """Symulacja przyjmuje 1.0 dla każdego zlecenia, które dotknęło limitu.
    Średnia z ułamków faktycznych jest bezpośrednią miarą tego zawyżenia."""
    r = rozjazd_z_symulacja([
        co_z_wypelnieniem(D("10"), D("10"), D("100"), F),   # 1.0
        co_z_wypelnieniem(D("4"), D("10"), D("100"), F),    # 0.4
        co_z_wypelnieniem(D("0"), D("10"), D("100"), F),    # nie liczy sie
    ])
    assert r["n"] == 2
    assert r["sredni_ulamek"] == D("0.7")
    assert r["czesciowych"] == 1


def test_brak_wypelnien_nie_udaje_ulamka_zerowego():
    """None znaczy „nie zmierzono", zero znaczy „zmierzono i wyszło zero".
    Zero w tym miejscu wyglądałoby jak katastrofalna jakość egzekucji."""
    r = rozjazd_z_symulacja([co_z_wypelnieniem(D("0"), D("10"), D("100"), F)])
    assert r["n"] == 0
    assert r["sredni_ulamek"] is None


def test_nadwyzka_ponad_plan_nie_powieksza_pozycji():
    """Giełda nie powinna dać więcej, niż zamówiono, ale gdyby dała, pozycja
    ma zostać taka, jaką zatwierdził risk kernel."""
    r = co_z_wypelnieniem(D("12"), D("10"), D("100"), F)
    assert r.rodzaj == "pelne"
    assert r.ilosc == D("10")


def test_plan_o_zerowej_ilosci_nie_przechodzi_po_cichu():
    with pytest.raises(ValueError):
        co_z_wypelnieniem(D("0"), D("0"), D("100"), F)


# ------------------------------- prowizja pobrana w kupionym aktywie

def test_prowizja_w_aktywie_ZMNIEJSZA_posiadana_ilosc():
    """Zlecenie na 1,0 SOL przy makerze 0,1% daje nam 0,999 SOL — Bybit potrąca
    prowizję z otrzymanej ilości.

    Zapisanie 1,0 tworzy pozycję, KTÓREJ NIE DA SIĘ SPRZEDAĆ: sygnał wyjścia
    planuje sprzedaż całości, giełda odrzuca ją za brak salda, plan wygasa,
    następna świeca powtarza to samo. Nic tej pętli nie przerywa, bo każdy
    przebieg wygląda identycznie jak poprzedni.
    """
    from services.engine.egzekucja import rozlicz_prowizje

    ilosc, koszt = rozlicz_prowizje(D("1.0"), D("140"), D("0.001"),
                                    "SOL", "SOL", "USDC")
    assert ilosc == D("0.999")
    assert koszt == D("0.140")


def test_prowizja_w_aktywie_JEST_PRZELICZANA_na_walute_kwotowana():
    """`update_equity` sumuje `fees_paid` jako liczby bez jednostki. Prowizja
    0,001 SOL wpisana wprost odejmowała od kapitału 0,001 zamiast 0,14 —
    zaniżenie stukrotne.

    Kierunek jest najgorszy z możliwych: ZAWYŻA kapitał, więc obsunięcie wychodzi
    mniejsze i kill-switch strzela później, niż powinien.
    """
    from services.engine.egzekucja import rozlicz_prowizje

    _, koszt = rozlicz_prowizje(D("1.0"), D("140"), D("0.001"),
                                "SOL", "SOL", "USDC")
    assert koszt == D("0.14")
    assert koszt != D("0.001")


def test_prowizja_w_walucie_kwotowanej_NIE_rusza_ilosci():
    """Przy sprzedaży prowizja idzie w walucie kwotowanej — odjęcie jej od
    ilości byłoby wtedy odejmowaniem jabłek od gruszek."""
    from services.engine.egzekucja import rozlicz_prowizje

    ilosc, koszt = rozlicz_prowizje(D("1.0"), D("140"), D("0.14"),
                                    "USDC", "SOL", "USDC")
    assert ilosc == D("1.0")
    assert koszt == D("0.14")


def test_NIEZNANA_waluta_prowizji_nie_zmienia_tego_co_uwazamy_za_posiadane():
    """Domysł może być potrzebny przy koszcie, ale nie przy ilości: pomyłka
    w drugą stronę stworzyłaby pozycję-widmo z niczego."""
    from services.engine.egzekucja import rozlicz_prowizje

    ilosc, _ = rozlicz_prowizje(D("1.0"), D("140"), D("0.14"),
                                None, "SOL", "USDC")
    assert ilosc == D("1.0")


# ---------------------------------------------------------------------------
# MODEL PESYMISTYCZNY W SYMULACJI (audyt Fable T1 / F-3.2, 2026-10-03)
#
# Model sprzed tej daty dawal limit po `close` swiecy decyzyjnej i cale 4 h
# nastepnej swiecy na wypelnienie - 0 niewypelnionych na 1 561 zlecen wobec
# ~65% wypelnialnosci na demo. Ponizej rynek zbudowany ze swiec 1h i panel 4h
# bedacy ich agregatem, tak jak `horizons.H4H.source_timeframe = "1h"`.

from datetime import datetime, timedelta, timezone  # noqa: E402

from services.engine.horizons import get as get_horizon  # noqa: E402
from services.engine.koszty import NIEZMIERZONE, POMIAR, karta_rundy, koszt_niewypelnienia  # noqa: E402
from services.engine.portfolio import (  # noqa: E402
    Egzekucja, indeks_swiec, simulate_portfolio, wycena_wysylki, z_polowa_spreadu,
)
from services.engine.signals import DEFAULT_WEIGHTS  # noqa: E402
from tests.test_portfolio import LIMITY_LUZNE, cechy_kupuj  # noqa: E402

H4 = get_horizon("4h")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rynek(n4h: int, krok_h: float, dolek: float):
    """(panel 4h, swiece 1h). Cena rosnie o `krok_h` na godzine; `low` kazdej
    godziny = min(otwarcie, zamkniecie) x (1 - dolek)."""
    swiece, p = [], 100.0
    for k in range(4 * n4h + 8):
        otw, p = p, p * (1 + krok_h)
        swiece.append({"t": T0 + timedelta(hours=k), "open": otw, "close": p,
                       "low": min(otw, p) * (1 - dolek),
                       "high": max(otw, p) * (1 + dolek)})
    panel = []
    for i in range(n4h):
        g = swiece[4 * i:4 * i + 4]
        panel.append({"t": g[0]["t"], "f": cechy_kupuj(), "price": g[-1]["close"],
                      "low": min(s["low"] for s in g), "high": max(s["high"] for s in g)})
    return panel, swiece


def _symuluj(panel, swiece, egz):
    return simulate_portfolio(
        {"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0}, {"AAA": (0.05, 0.02)}, H4, 0.05,
        LIMITY_LUZNE, kind="spot", egzekucja=egz,
        swiece_wykonania={"AAA": indeks_swiec(swiece)})


def test_wycena_wysylki_interpoluje_godzine_w_ktorej_wypada_wysylka():
    """Wysylka 20 min po zamknieciu swiecy decyzyjnej = zamkniecie poprzedniej
    godziny + 1/3 drogi do zamkniecia biezacej; rozstrzyga godzina PO niej."""
    panel, swiece = _rynek(5, 0.003, 0.0)
    idx = indeks_swiec(swiece)
    row = panel[1]                          # swieca 04:00-08:00, decyzja 08:00
    cena, godzina = wycena_wysylki(row, H4.bar_minutes, 20, idx)
    poprzednia = idx[T0 + timedelta(hours=7)]["close"]   # zamkniecie o 08:00
    biezaca = idx[T0 + timedelta(hours=8)]["close"]      # zamkniecie o 09:00
    assert cena == pytest.approx(poprzednia + (biezaca - poprzednia) / 3)
    assert godzina["t"] == T0 + timedelta(hours=9)


def test_wycena_bez_opoznienia_to_cena_zamkniecia_swiecy_decyzyjnej():
    panel, swiece = _rynek(5, 0.003, 0.0)
    cena, godzina = wycena_wysylki(panel[1], H4.bar_minutes, 0, indeks_swiec(swiece))
    assert cena == panel[1]["price"]
    assert godzina["t"] == T0 + timedelta(hours=8)   # pierwsza godzina po decyzji


def test_wycena_bez_swiecy_1h_zwraca_None_zamiast_zgadywac():
    panel, swiece = _rynek(5, 0.003, 0.0)
    dziura = [s for s in swiece if s["t"] != T0 + timedelta(hours=9)]
    assert wycena_wysylki(panel[1], H4.bar_minutes, 20, indeks_swiec(dziura)) is None


def test_pol_spreadu_po_wlasciwej_stronie_arkusza():
    assert z_polowa_spreadu(100.0, 10.0, kupno=True) == pytest.approx(99.95)
    assert z_polowa_spreadu(100.0, 10.0, kupno=False) == pytest.approx(100.05)


def test_a_kupno_z_limitem_ponizej_low_nastepnej_godziny_nie_wchodzi():
    """(a) Rynek ucieka 0,5%/h, dolki godzin tylko 1 pb ponizej otwarcia.
    Model sprzed zmiany wypelnia (cala nastepna swieca 4h zaczyna sie od
    ceny limitu), pesymistyczny NIE: limit z ceny 20 min po decyzji minus pol
    spreadu lezy ponizej `low` godziny po wysylce."""
    panel, swiece = _rynek(40, 0.005, 0.0001)
    dzis = _symuluj(panel, swiece, Egzekucja(limit=True))
    assert dzis.wypelnione_wejscia > 0
    pes = _symuluj(panel, swiece, Egzekucja(
        limit=True, opoznienie_min=20, pesymistyczna=True,
        spread_per_instrument={"AAA": 10.0}))
    assert pes.wypelnione_wejscia == 0
    assert pes.niewypelnione_wejscia > 0
    assert not pes.trades
    # Ruch, ktory przepadl, jest zapisany - rynek rosl, wiec dodatni.
    assert len(pes.utracone_ruchy) == pes.niewypelnione_wejscia
    assert all(r > 0 for r in pes.utracone_ruchy)


def test_a_spread_instrumentu_sam_potrafi_zablokowac_wypelnienie():
    """Te same swiece, ten sam czas wysylki: przy spreadzie 0 limit dotyka
    dolka godziny, przy 10 pb (pol = 5 pb ponizej ceny) juz nie."""
    panel, swiece = _rynek(40, 0.0, 0.0003)
    bez = _symuluj(panel, swiece, Egzekucja(limit=True, pesymistyczna=True,
                                            spread_per_instrument={"AAA": 0.0}))
    z = _symuluj(panel, swiece, Egzekucja(limit=True, pesymistyczna=True,
                                          spread_per_instrument={"AAA": 10.0}))
    assert bez.wypelnione_wejscia > 0
    assert z.wypelnione_wejscia == 0


def test_b_opoznienie_0_i_spread_0_daje_wynik_identyczny_jak_dzis():
    """(b) Regresja: wartosci domyslne nowych pol (takze jawnie podany spread
    zerowy i swiece 1h podane, choc niepotrzebne) to model sprzed zmiany co do
    transakcji i krzywej kapitalu."""
    panel, swiece = _rynek(40, 0.002, 0.004)
    stary = simulate_portfolio(
        {"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0}, {"AAA": (0.05, 0.02)}, H4, 0.05,
        LIMITY_LUZNE, kind="spot", egzekucja=Egzekucja(limit=True))
    nowy = _symuluj(panel, swiece, Egzekucja(limit=True, opoznienie_min=0,
                                             spread_per_instrument={"AAA": 0.0},
                                             pesymistyczna=False))
    assert stary.trades
    assert nowy.trades == stary.trades
    assert nowy.equity_curve == stary.equity_curve
    assert (nowy.wypelnione_wejscia, nowy.niewypelnione_wejscia) == \
        (stary.wypelnione_wejscia, stary.niewypelnione_wejscia)


def test_model_z_opoznieniem_bez_swiec_1h_odmawia_zamiast_cicho_wrocic_do_close():
    panel, _ = _rynek(40, 0.002, 0.004)
    with pytest.raises(ValueError, match="świec 1h"):
        simulate_portfolio(
            {"AAA": panel}, DEFAULT_WEIGHTS, {"AAA": 2.0}, {"AAA": (0.05, 0.02)}, H4,
            0.05, LIMITY_LUZNE, kind="spot",
            egzekucja=Egzekucja(limit=True, opoznienie_min=20))


def test_model_pesymistyczny_nie_dotyczy_zlecen_rynkowych():
    panel, swiece = _rynek(40, 0.002, 0.004)
    with pytest.raises(ValueError, match="limitem"):
        _symuluj(panel, swiece, Egzekucja(limit=False, pesymistyczna=True))


def test_dziura_w_swiecach_1h_liczy_sie_jako_niewypelnienie_i_osobno():
    panel, swiece = _rynek(40, 0.0, 0.01)
    res = _symuluj(panel, [], Egzekucja(limit=True, pesymistyczna=True))
    assert res.wypelnione_wejscia == 0
    assert res.brak_swiec_wykonania == res.niewypelnione_wejscia > 0


def test_c_karta_ma_niewypelnienie_jako_POMIAR_poza_doplata():
    """(c) W modelu pesymistycznym niewypelnienia sa juz w zwrotach symulacji:
    skladnik jest zmierzony, ale do doplaty nie wchodzi (inaczej strata
    liczylaby sie dwa razy)."""
    koszt = koszt_niewypelnienia(0.7, [0.01, 0.03])
    assert koszt == pytest.approx(0.3 * 0.02)
    k = karta_rundy("spot_eu", True, spread_bps=5.0, niewypelnienie=koszt,
                    niewypelnienie_w_symulacji=True)
    s = next(s for s in k.skladniki if s.nazwa == "niewypelnienie")
    assert s.zrodlo == POMIAR
    assert s.wartosc == pytest.approx(koszt)
    assert s.w_symulacji is True
    poza = karta_rundy("spot_eu", True, spread_bps=5.0, niewypelnienie=koszt)
    assert k.doplata_poza_symulacja == pytest.approx(poza.doplata_poza_symulacja - koszt)
    # Bez liczby - nadal NIEZMIERZONE, nie zero.
    pusta = karta_rundy("spot_eu", True, spread_bps=5.0, niewypelnienie=None,
                        niewypelnienie_w_symulacji=True)
    assert next(s for s in pusta.skladniki
                if s.nazwa == "niewypelnienie").zrodlo == NIEZMIERZONE


def test_koszt_niewypelnienia_zachowuje_znak_i_odroznia_brak_od_zera():
    assert koszt_niewypelnienia(0.5, [-0.02]) == pytest.approx(-0.01)
    assert koszt_niewypelnienia(None, [0.01]) is None
    assert koszt_niewypelnienia(1.0, []) == 0.0
    assert koszt_niewypelnienia(0.8, []) is None
