"""Poślizg liczony wobec ceny RÓWNEJ cenie wykonania nie jest pomiarem.

CO RAPORTOWAŁA CZUJKA

    poślizg maker (założenie +0.00 pb): n=4, mediana +0.00 pb,
    przedział 95% [+0.00 pb, +0.00 pb] — NIEODROZNIALNE OD ZALOZENIA

Czytane po ludzku: „zmierzyliśmy cztery wypełnienia i wyszło dokładnie tyle,
ile zakładaliśmy". Czyli: założenie potwierdzone pomiarem.

DLACZEGO TO NIE BYŁ POMIAR

Te cztery zera nie pochodzą z egzekucji, tylko z arytmetyki. Złożyły się na nie
dwie rzeczy naraz i każda z osobna wystarczy:

  1. Zlecenie z limitem wypełnia się PO CENIE LIMITU albo lepiej. Odniesieniem
     jest `exec.order.limit_price`, czyli ta sama cena. Różnica wychodzi zero
     nie dlatego, że egzekucja była idealna, tylko dlatego, że pytanie brzmiało
     „czy limit wypełnił się po cenie limitu".
  2. `loop.zapisz_wejscie` przy rozstrzyganiu NADPISUJE `exec.order.limit_price`
     ceną WYKONANIA (audyt silnika, znalezisko 2). Wtedy zero wychodzi nawet
     wtedy, gdy zlecenie przeszło przez spread jako taker po gorszej cenie.

W obu wypadkach `(x − x) / x = 0` i żadne z tych zer nie mówi nic o tym, ile
naprawdę kosztowała egzekucja. Werdykt „nieodróżnialne od założenia" jest tu
gorszy niż brak liczby: moduł ma trzy stany po to, żeby „nie wiem" nie
wyglądało jak „zmierzone".

CZEGO TEN PLIK NIE ZMIENIA

Samej funkcji `poslizg_wiersza`. Wypełnienie co do grosza po cenie zlecenia MA
mieć poślizg zero — to poprawna wartość pojedynczego wiersza i pilnuje jej
`test_poslizg_cena_zlecenia.py`. Zmienia się warstwa WERDYKTU: próba złożona
wyłącznie z takich wierszy nie jest próbą, z której wolno cokolwiek orzec
o stałej kosztowej.
"""
import pytest

from decimal import Decimal as D

from services.engine.poslizg import (
    NIE_ZMIERZONO,
    NIEODROZNIALNE,
    TOZSAMOSC,
    opis,
    pomiar,
    statystyki,
)


def wiersz(**zmiana):
    """Wypełnienie z giełdy, którego cena odniesienia = cena wykonania."""
    baza = {
        "rodzaj_zlecenia": "limit",
        "strona": "buy",
        "zrodlo": "gielda",
        "ulamek": "1",
        "rodzaj_wypelnienia": "pelne",
        "cena_zlecenia": D("100"),
        "cena_planu": D("100"),
        "cena_decyzji": D("100"),
        "cena_wykonania": D("100"),
    }
    return {**baza, **zmiana}


# Cztery wypelnienia gieldowe z produkcji (audyt uzgodnienia, 2026-09-24).
# Ceny prawdziwe; `cena_zlecenia` rowna cenie wykonania, bo tak je zapisal
# `zapisz_wejscie`.
PRODUKCJA = [
    wiersz(strona="buy", cena_zlecenia=D("117.24"), cena_wykonania=D("117.24")),
    wiersz(strona="buy", cena_zlecenia=D("85976.80"), cena_wykonania=D("85976.80")),
    wiersz(strona="buy", cena_zlecenia=D("2741.57"), cena_wykonania=D("2741.57")),
    wiersz(strona="sell", cena_zlecenia=D("2716.26"), cena_wykonania=D("2716.26")),
]


def test_cala_proba_tozsamosciowa_NIE_JEST_nieodroznialna_od_zalozenia():
    """Cztery zera z arytmetyki nie potwierdzają stałej kosztowej."""
    s = statystyki(PRODUKCJA)["maker"]
    assert s["werdykt"] == TOZSAMOSC
    assert s["werdykt"] != NIEODROZNIALNE


def test_liczba_wierszy_tozsamosciowych_JEST_WIDOCZNA():
    """Bez tej liczby nie da się odróżnić pomiaru od echa własnego zapisu."""
    s = statystyki(PRODUKCJA)["maker"]
    assert s["tozsamosciowych"] == 4
    assert s["n"] == 4


def test_opis_NIE_WOLNO_zeby_wypisal_nieodroznialne_dla_samych_tozsamosci():
    linie = " | ".join(opis(pomiar(PRODUKCJA)))
    assert "NIEODROZNIALNE" not in linie.upper()


def test_opis_MOWI_ze_to_nie_jest_pomiar_poslizgu():
    """Człowiek ma z tej linii wyjść z „nie wiadomo", a nie z liczbą."""
    linie = " | ".join(opis(pomiar(PRODUKCJA)))
    assert "TO NIE JEST POMIAR POŚLIZGU" in linie
    # ZADNEJ LICZBY W PUNKTACH BAZOWYCH W TEJ LINII. To ona wygladala na wynik.
    maker = next(l for l in opis(pomiar(PRODUKCJA)) if l.startswith("poślizg maker"))
    assert "pb," not in maker.removeprefix("poślizg maker (założenie +0.00 pb)")


def test_jedno_wypelnienie_ROZNE_od_odniesienia_przywraca_pomiar():
    """Tożsamość to stan próby, nie trwała blokada modułu."""
    prawdziwe = PRODUKCJA + [
        wiersz(cena_zlecenia=D("100"), cena_wykonania=D("100.05")),
        wiersz(cena_zlecenia=D("100"), cena_wykonania=D("100.07")),
    ]
    s = statystyki(prawdziwe)["maker"]
    assert s["werdykt"] in (NIEODROZNIALNE, "zalozenie poza przedzialem")
    # ...ale udzial tozsamosci zostaje widoczny, bo ciagnie srednia do zera.
    assert s["tozsamosciowych"] == 4
    assert s["n"] == 6


def test_pusta_proba_NADAL_jest_NIE_ZMIERZONO_a_nie_tozsamoscia():
    """Dwa różne braki wiedzy mają dwie różne nazwy."""
    assert statystyki([])["taker"]["werdykt"] == NIE_ZMIERZONO


def test_taker_z_produkcji_to_wciaz_zero_obserwacji():
    """Cztery wypełnienia to cztery LIMITY. O poślizgu takera nie wiemy nic."""
    s = statystyki(PRODUKCJA)["taker"]
    assert s["n"] == 0
    assert s["werdykt"] == NIE_ZMIERZONO


# --- rozjazd ksiag: zera strukturalne nie moga rozcienczac mediany ---------
#
# PIEC PRODUKCYJNYCH WYPELNIEN, PELNY KOMPLET (2026-09-24):
#
#   45 SOLUSDC buy  117.24    <- brak wiersza planu
#   46 BTCUSDC buy  85976.80  <- plan 85976.80, rozjazd 0,00 pb
#   47 ETHUSDC buy  2741.57   <- plan 2741.57,  rozjazd 0,00 pb
#   48 ETHUSDC sell 2716.26   <- plan 2727.55,  rozjazd 41,56 pb
#   49 BTCUSDC sell 84046.00  <- plan 84001.10, rozjazd  5,34 pb
#
# Podmiana ceny na `cena_makera` zachodzi WYLACZNIE przy wyjsciu
# (`zlecenia.sprawdz_zlecenie`), wiec przy wejsciach plan RONWA SIE cenie
# wyslanej z zalozenia i ich zera nie sa pomiarem rozjazdu - sa jego
# definicyjnym brakiem. Wrzucone do wspolnej mediany sciagaja ja z 23,45 pb
# na 2,67 pb, czyli prawie dziesieciokrotnie, i robia to tym mocniej, im wiecej
# wejsc jest w probie.

def rozjazd_wiersz(strona, zlecenie, plan):
    return wiersz(strona=strona, cena_zlecenia=D(zlecenie),
                  cena_wykonania=D(zlecenie), cena_planu=D(plan))


PRODUKCJA_5 = [
    wiersz(strona="buy", cena_zlecenia=D("117.24"), cena_wykonania=D("117.24"),
           cena_planu=None),
    rozjazd_wiersz("buy", "85976.80", "85976.80"),
    rozjazd_wiersz("buy", "2741.57", "2741.57"),
    rozjazd_wiersz("sell", "2716.26", "2727.55"),
    rozjazd_wiersz("sell", "84046.00", "84001.10"),
]


def test_wszystkie_PIEC_produkcyjnych_wypelnien_to_tozsamosci():
    s = statystyki(PRODUKCJA_5)["maker"]
    assert (s["n"], s["tozsamosciowych"]) == (5, 5)
    assert s["werdykt"] == TOZSAMOSC


def test_rozjazd_ksiag_LICZY_ile_wierszy_jest_zerowych():
    """Bez tej liczby mediana 2,67 pb wygląda jak zmierzony rozjazd ksiąg,
    a jest średnią z dwóch pomiarów i dwóch definicyjnych zer."""
    from services.engine.poslizg import rozjazd_ksiag

    r = rozjazd_ksiag(PRODUKCJA_5)
    assert r["n"] == 4               # SOL wypada, nie ma wiersza planu
    assert r["zerowych"] == 2
    assert r["najwiekszy"] == pytest.approx(41.56e-4, rel=1e-3)


def test_opis_MOWI_ile_zer_jest_w_rozjezdzie_ksiag():
    linie = " | ".join(opis(pomiar(PRODUKCJA_5)))
    assert "z tego 2 zerowe" in linie
