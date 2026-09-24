"""Spread zmierzony na księdze, na której NIE SKŁADAMY ZLECEŃ, nie jest pomiarem.

CO SIĘ STAŁO

Poprosiłem o pomiar spreadu i dostałem go — `market.book_snapshot`, 7 dni,
mediany od 0,012 pb (BTC) do 1,304 pb (BNB). Liczby są prawdziwe. Opisują inny
rynek.

`db/migrations/0018_bybit_eu.sql` mówi wprost: dane publiczne — świece
i arkusz — pochodzą z hosta GLOBALNEGO (`PUBLIC_HOST`), a zlecenia podpisane
idą na `bybit.eu`. Powód podany w samej migracji: podmiot unijny ma dla BTCUSDT
rzędu **0,1% globalnego wolumenu**, więc „jego świece opisywałyby inny rynek".

Czyli spread, który zmierzyliśmy, jest spreadem księgi, w której nasze zlecenie
nigdy nie stanie. Przy 0,1% wolumenu spread rzeczywisty jest niemal na pewno
znacznie szerszy — ale o ile, tego nikt nie wie.

DLACZEGO TO NIE MOŻE PO PROSTU WEJŚĆ DO RACHUNKU JAKO `POMIAR`

Bo wtedy karta kosztu ogłosiłaby się PEŁNĄ, mając nadal nieznany jeden
składnik. To dokładnie ta sama klasa błędu, którą cały ten moduł tropi: liczba
prawdziwa, tylko o czym innym — a w rachunku wyglądająca jak odpowiedź na
zadane pytanie.

CO ZATEM ROBIMY Z TĄ LICZBĄ

Traktujemy ją jako PODŁOGĘ. Rynek mniej płynny nie ma węższego spreadu niż
bardziej płynny, więc spread globalny jest dolnym ograniczeniem naszego. Dolne
oszacowanie kosztu robi się dzięki temu ciaśniejsze — a pytanie „ile naprawdę"
zostaje otwarte i widoczne.
"""
import pytest

from services.engine.koszty import (
    INNY_RYNEK,
    NIEZMIERZONE,
    POMIAR,
    karta_rundy,
    opis_karty,
)


def test_spread_z_obcej_ksiegi_NIE_JEST_pomiarem():
    s = next(x for x in karta_rundy("spot", maker=True, spread_bps=0.802,
                                    spread_nasza_ksiega=False).skladniki
             if x.nazwa == "spread")
    assert s.zrodlo == INNY_RYNEK
    assert s.zrodlo != POMIAR


def test_spread_z_obcej_ksiegi_WCHODZI_do_sumy_jako_podloga():
    """Rynek mniej płynny nie ma węższego spreadu, więc liczba jest dolnym
    ograniczeniem i zacieśnia dolne oszacowanie kosztu."""
    bez = karta_rundy("spot", maker=True)
    z_obca = karta_rundy("spot", maker=True, spread_bps=0.802,
                         spread_nasza_ksiega=False)
    assert z_obca.policzone > bez.policzone
    assert z_obca.policzone == pytest.approx(2 * 0.0010 + 0.802 / 1e4)


def test_karta_ze_spreadem_z_OBCEJ_ksiegi_NADAL_NIE_JEST_pelna():
    """Sedno. Liczba jest, a pytanie zostaje otwarte."""
    k = karta_rundy("spot", maker=True, spread_bps=0.802,
                    niewypelnienie=0.0005, spread_nasza_ksiega=False)
    assert k.pelna is False
    assert "spread" in k.nieznane


def test_ten_sam_spread_z_NASZEJ_ksiegi_domyka_karte():
    """Kanarek do poprzedniego: różni je wyłącznie księga, nie wartość."""
    k = karta_rundy("spot", maker=True, spread_bps=0.802,
                    niewypelnienie=0.0005, spread_nasza_ksiega=True)
    assert k.pelna is True
    assert k.nieznane == ()


def test_opis_NAZYWA_obca_ksiege_i_mowi_ze_to_PODLOGA():
    linia = next(l for l in opis_karty(
        karta_rundy("spot", maker=True, spread_bps=0.802,
                    spread_nasza_ksiega=False)) if "spread" in l)
    assert "CO NAJMNIEJ" in linia.upper() or "podłog" in linia.lower()
    assert INNY_RYNEK in linia


def test_brak_spreadu_NADAL_mowi_NIE_ZMIERZONO():
    """Trzeci stan zostaje trzecim stanem: nie ma liczby to nie to samo, co
    jest liczba, ale z cudzej księgi."""
    s = next(x for x in karta_rundy("spot", maker=True).skladniki
             if x.nazwa == "spread")
    assert s.zrodlo == NIEZMIERZONE
    assert s.wartosc is None


def test_flaga_bez_wartosci_NIE_ZMIENIA_niczego():
    """`spread_nasza_ksiega` opisuje liczbę, której nie ma — więc nie ma czego
    opisywać i stan zostaje `NIE ZMIERZONO`, a nie `pomiar innej ksiegi`."""
    s = next(x for x in karta_rundy("spot", maker=True,
                                    spread_nasza_ksiega=False).skladniki
             if x.nazwa == "spread")
    assert s.zrodlo == NIEZMIERZONE
