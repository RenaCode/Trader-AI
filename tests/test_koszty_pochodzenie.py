"""`runda()` podaje jedną liczbę i nie mówi, czego w niej NIE MA.

CO WYPISUJE TRENER

    Egzekucja: perp, zlecenia z limitem, koszt rundy 0.040%

Czyta się to jako pełny rachunek kosztu obrotu. Nie jest pełny. Przy zleceniu
z limitem `runda()` liczy `2 × (maker + POSLIZG_MAKER)` = `2 × (0,0002 + 0)`
i milczy o trzech rzeczach naraz:

  * SPREAD. Symulacja wchodzi i wychodzi po `close` świecy, czyli po cenie
    ostatniej transakcji. Żeby dostać stawkę makera, trzeba stać po właściwej
    stronie arkusza — pół spreadu od środka, na każdej nodze. W bazie
    produkcyjnej leży milion migawek arkusza (`market.book_snapshot`) i cecha
    `book_spread_bps`; rachunek kosztu nie sięga do nich ani razu.
  * NIEWYPEŁNIENIE. Zero poślizgu makera nie jest pomiarem ani założeniem —
    jest definicją zlecenia z limitem, które wchodzi po cenie limitu albo
    lepiej. Prawdziwy koszt tej strony to zlecenia, które NIE WESZŁY, a tej
    liczby nie ma w żadnej próbie (patrz test_poslizg_ulamek_warunkowy.py).
  * FINANSOWANIE. Na kontrakcie wieczystym płacone co osiem godzin, niezależnie
    od wyniku pozycji. Nagłówek `koszty.py` sam to nazywa „luką do domknięcia".

RÓŻNICA MIĘDZY „ZERO" A „NIE WIADOMO" JEST TU CAŁĄ RZECZĄ. Składnik bez
zmierzonej wartości wliczony jako zero daje liczbę, która wygląda na pełny
koszt i jest tym mniejsza, im więcej rzeczy nie policzyliśmy.
"""
import pytest

from services.engine.koszty import (
    NIEZMIERZONE,
    POMIAR,
    TARYFA,
    ZALOZENIE,
    Skladnik,
    karta_rundy,
    koszty,
    opis_karty,
    runda,
)


# --- czego karta NIE WOLNO jej przemilczec ---------------------------------

def test_karta_limitu_NAZYWA_spread_jako_niezmierzony():
    k = karta_rundy("perp", maker=True)
    spread = next(s for s in k.skladniki if s.nazwa == "spread")
    assert spread.wartosc is None
    assert spread.zrodlo == NIEZMIERZONE
    assert "spread" in k.nieznane


def test_karta_limitu_NAZYWA_niewypelnienie_a_nie_poslizg_makera():
    """Zero poślizgu makera to definicja limitu, nie wynik. Kosztem strony
    makerskiej są zlecenia, które nie weszły — i to one mają być w karcie."""
    k = karta_rundy("spot", maker=True)
    nazwy = {s.nazwa for s in k.skladniki}
    assert "niewypelnienie" in nazwy
    assert "poslizg" not in nazwy, (
        "poślizg makera jest tożsamościowo zerowy — wpisany do karty "
        "wyglądałby jak zmierzony składnik o wartości zero")


def test_karta_rynkowa_MA_poslizg_i_nazywa_go_ZALOZENIEM():
    """`POSLIZG_TAKER = 4 pb` nie było mierzone ani razu: n=0."""
    k = karta_rundy("spot", maker=False)
    p = next(s for s in k.skladniki if s.nazwa == "poslizg")
    assert p.zrodlo == ZALOZENIE
    assert p.wartosc == pytest.approx(2 * 0.0004)


def test_perp_MA_finansowanie_w_karcie_a_spot_NIE_MA():
    """Kontrakt wieczysty płaci co osiem godzin; spot nie płaci nic."""
    assert "finansowanie" in {s.nazwa for s in karta_rundy("perp", maker=True).skladniki}
    assert "finansowanie" not in {s.nazwa for s in karta_rundy("spot", maker=True).skladniki}


def test_prowizja_JEST_pomiarem_bo_odczytana_z_gieldy():
    """Stawki pochodzą z `/v5/account/fee-rate` i z cennika giełdy, a cztery
    produkcyjne wypełnienia dają `fee/qty` = 0,001 co do cyfry."""
    p = next(s for s in karta_rundy("spot", maker=True).skladniki
             if s.nazwa == "prowizja")
    assert p.zrodlo in (POMIAR, TARYFA)
    assert p.wartosc == pytest.approx(2 * koszty("spot").maker)


# --- arytmetyka karty -------------------------------------------------------

def test_policzone_sumuje_TYLKO_znane_skladniki():
    k = karta_rundy("spot", maker=True)
    assert k.policzone == pytest.approx(2 * koszty("spot").maker)


def test_karta_z_niezmierzonym_skladnikiem_NIE_JEST_pelna():
    assert karta_rundy("spot", maker=True).pelna is False


def test_karta_staje_sie_pelna_dopiero_po_podaniu_WSZYSTKICH_pomiarow():
    # Od 2026-10-03 karta spot ma tez skladnik `pyl`; pelna jest dopiero z nim.
    # Tego samego dnia limit dostal `wyjscie takerem` (audyt F-4.3).
    k = karta_rundy("spot", maker=True, spread_bps=3.0, niewypelnienie=0.0005,
                    pyl=3e-5, wyjscia_takerem=0.1)
    assert k.pelna is True
    assert k.nieznane == ()
    # 2 x 0,10% prowizji + 3 pb spreadu + 5 pb niewypelnienia + 0,3 pb pylu
    # + 10% wyjsc x (taker - maker) - na "spot" obie stawki 0,10%, wiec zero.
    assert k.policzone == pytest.approx(2 * 0.0010 + 3.0 / 1e4 + 0.0005 + 3e-5)


def test_podany_spread_przestaje_byc_niezmierzony():
    s = next(x for x in karta_rundy("perp", maker=True, spread_bps=2.5).skladniki
             if x.nazwa == "spread")
    assert s.zrodlo == POMIAR
    assert s.wartosc == pytest.approx(2.5 / 1e4)


def test_runda_ROWNA_SIE_policzonej_czesci_karty_a_nie_calosci():
    """Kanarek na dokładnie tę pomyłkę, o którą tu chodzi: `runda()` jest
    DOLNYM OSZACOWANIEM kosztu, nie kosztem."""
    for kind in ("spot", "spot_eu", "perp"):
        for maker in (True, False):
            assert runda(kind, maker) == pytest.approx(
                karta_rundy(kind, maker).policzone)


# --- wypisanie --------------------------------------------------------------

def test_opis_karty_NIE_WYPISUJE_zera_przy_skladniku_niezmierzonym():
    linie = opis_karty(karta_rundy("perp", maker=True))
    spread = next(l for l in linie if "spread" in l)
    assert "0.00" not in spread
    assert "NIE ZMIERZONO" in spread.upper()


def test_opis_karty_MOWI_ze_suma_jest_NIEPELNA():
    k = karta_rundy("perp", maker=True)
    linie = " | ".join(opis_karty(k))
    assert "CO NAJMNIEJ" in linie.upper()
    # NIEZNANE TO NIE TO SAMO CO „NIE JEST STALA RUNDY".
    #
    # Finansowanie jest naliczane (od 2026-09-24 w `simulate_portfolio`, z cechy
    # `funding_rate`) — po prostu zalezy od czasu trzymania, wiec stalej rundy
    # dla niego nie ma. Wpisane do `nieznane` oglaszaloby luke, ktora zostala
    # zamknieta, i kazaloby szukac czegos, czego nie brakuje.
    assert set(k.nieznane) == {"niewypelnienie", "spread", "wyjscie takerem"}
    assert "finansowanie" in linie


def test_opis_pelnej_karty_NIE_STRASZY_niepelnoscia():
    linie = " | ".join(opis_karty(
        karta_rundy("spot", maker=True, spread_bps=3.0, niewypelnienie=0.0005,
                    pyl=3e-5, wyjscia_takerem=0.1)))
    assert "CO NAJMNIEJ" not in linie.upper()


def test_skladnik_jest_niezmienny():
    """Karta jest raportem, nie buforem roboczym."""
    s = Skladnik("x", 1.0, POMIAR)
    with pytest.raises(Exception):
        s.wartosc = 2.0


def test_karta_limitu_NAZYWA_wyjscie_takerem_i_liczy_je_ze_STAWEK_toru():
    """Wyjście po dwóch odrzuceniach PostOnly idzie GTC po drugiej stronie
    spreadu (audyt F-4.3) i płaci takera na jednej nodze. Bez udziału z
    produkcji składnik jest NIEZMIERZONY, nie zerem; z udziałem - udział razy
    (taker - maker) tego toru. Na spot_eu taker 0,25% wobec makera 0,10%."""
    bez = next(s for s in karta_rundy("spot_eu", maker=True).skladniki
               if s.nazwa == "wyjscie takerem")
    assert bez.wartosc is None and bez.zrodlo == NIEZMIERZONE

    z = next(s for s in karta_rundy("spot_eu", maker=True,
                                    wyjscia_takerem=0.2).skladniki
             if s.nazwa == "wyjscie takerem")
    assert z.zrodlo == POMIAR
    assert z.wartosc == pytest.approx(0.2 * (0.0025 - 0.0010))
    assert z.w_symulacji is False

    # Egzekucja RYNKOWA placi takera na obu nogach juz w `prowizja`.
    assert "wyjscie takerem" not in {
        s.nazwa for s in karta_rundy("spot_eu", maker=False).skladniki}
