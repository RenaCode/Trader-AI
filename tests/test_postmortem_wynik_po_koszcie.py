"""Kanarek: dziennik po transakcjach liczy wynik PO KOSZCIE, jak krzywa kapitalu.

CO BYLO NIE TAK

`exec.position` trzyma wynik i prowizje w DWOCH kolumnach, a `realized_pnl`
jest wynikiem PRZED prowizja. Dowod arytmetyczny z produkcji, pozycja 33
(BTCUSDC, crypto/demo, oba wypelnienia z gieldy):

    wejscie   0.015319 BTC @ 85976.80  =  1317.0786 USDC
    wyjscie   0.015319 BTC @ 84046.00  =  1287.5007 USDC
    roznica                            =   -29.5779
    exec.position.realized_pnl         =   -29.5779   <- BRUTTO, co do groszy
    exec.position.fees_paid            =    +2.6046   (0.000015319 BTC * 85976.80
                                                       + 1.28750 USDC)

`loop.update_equity` czyta to poprawnie:

    SELECT COALESCE(sum(realized_pnl - fees_paid), 0) ...

`postmortem.statystyki` czytalo to odwrotnie: `sum(realized_pnl)` wchodzilo do
pola `wynik_netto`, a `wynik_brutto` powstawalo jako `suma + prowizje`. Nazwy
byly wiec zamienione, a liczba "brutto" niosla prowizje DWA RAZY.

DLACZEGO TO NIE OBJAWIALO SIE AWARIA

Oba pola sa liczbami o poprawnym typie i poprawnym znaku; myli sie tylko
o dokladnie jedna runde kosztowa, czyli o rzad wielkosci calej przewagi tej
strategii. Zmierzone na produkcji 2026-09-27, crypto/demo, 35 zamknietych
pozycji:

    postmortem `wynik_netto`    -147.1390   (to jest BRUTTO)
    postmortem `wynik_brutto`    -80.8593   (prowizja dodana zamiast odjeta)
    prawda (= realized_cum krzywej kapitalu)    -213.4187

Dziennik meldowal wiec 69% rzeczywistej straty i robil to w tekscie, ktory
idzie do promptu modelu jezykowego i na pierwsza pozycje panelu - czyli
w jedynym miejscu, w ktorym czlowiek czyta wynik okresu.

TEN TEST PADA NA KODZIE SPRZED POPRAWKI.
"""
from pathlib import Path

from services.engine.postmortem import opis_pomiaru, statystyki

KORZEN = Path(__file__).resolve().parents[1]
LOOP = KORZEN / "services/engine/loop.py"


def _poz(pid, pnl, fees):
    return {"id": pid, "symbol": "X", "side": "long", "qty": 1, "avg_price": 100,
            "realized_pnl": pnl, "fees_paid": fees, "opened_at": None,
            "closed_at": None, "godzin": 5.0, "regime": None,
            "confidence": None, "rationale": "", "sygnaly": None, "newsy": None}


def test_netto_to_wynik_MINUS_prowizja_a_nie_sam_wynik():
    """`realized_pnl` jest BRUTTO, wiec netto musi byc o prowizje MNIEJSZE."""
    s = statystyki([_poz(1, -0.01, 0.05), _poz(2, -0.01, 0.05)])
    assert s["wynik_brutto"] == -0.02, "brutto to suma realized_pnl"
    assert s["prowizje"] == 0.1
    assert s["wynik_netto"] == -0.12, "netto = brutto - prowizje"


def test_netto_nigdy_nie_jest_LEPSZE_od_brutto():
    """Prowizja jest nieujemna, wiec ten porzadek nie ma wyjatkow. Odwrocenie
    znakow jest przy zerowej prowizji niewidoczne - i tym bardziej trzeba go
    pilnowac przy prowizji dodatniej."""
    s = statystyki([_poz(1, 5.0, 0.4), _poz(2, -2.0, 0.6)])
    assert s["wynik_netto"] < s["wynik_brutto"]
    assert round(s["wynik_brutto"] - s["wynik_netto"], 10) == s["prowizje"]


def test_liczby_z_produkcji_zgadzaja_sie_z_krzywa_kapitalu():
    """Zmierzone 2026-09-27 na crypto/demo: 35 zamknietych pozycji.

    `exec.equity_point.realized_cum` = -213.418793658156, a to jest
    `sum(realized_pnl - fees_paid)`. Dziennik MUSI oddac te sama liczbe -
    inaczej sa dwie definicje wyniku handlu i czlowiek czyta lagodniejsza.
    """
    s = statystyki([_poz(1, -147.1390, 66.2797)])
    assert round(s["wynik_netto"], 4) == -213.4187
    assert round(s["wynik_brutto"], 4) == -147.1390


def test_opis_pomiaru_podaje_netto_po_koszcie():
    """Zdanie na poczatku `summary` jest tym, co czyta czlowiek."""
    s = statystyki([_poz(1, -0.01, 0.05), _poz(2, -0.01, 0.05)])
    assert "-0.1200" in opis_pomiaru(s)


def test_JEDNA_definicja_wyniku_z_krzywa_kapitalu():
    """Kanarek na ROZJAZD, nie na arytmetyke.

    Krzywa kapitalu i dziennik licza to samo pojecie dwoma kawalkami kodu.
    Gdy ktos zmieni jeden, ten test wskazuje drugi - a bez niego rozjazd
    objawilby sie tylko tym, ze dwa ekrany podaja rozne liczby.
    """
    assert "sum(realized_pnl - fees_paid)" in LOOP.read_text(encoding="utf-8"), (
        "loop.update_equity zmienilo definicje wyniku zrealizowanego - "
        "postmortem.statystyki musi pojsc za nia")
