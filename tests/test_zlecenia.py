"""Reguły giełdy przy składaniu zlecenia — i to, ile kosztuje ich złamanie."""
from decimal import Decimal as D

import pytest

from services.engine.zlecenia import (
    Filtry, cena_do_kroku, czy_zamykalna, do_kroku_w_dol, sprawdz_zlecenie,
)

PELNE = Filtry(krok_ilosci=D("0.001"), min_ilosc=D("0.001"),
               min_wartosc=D("5"), krok_ceny=D("0.01"))


def test_ilosc_zaokraglana_ZAWSZE_W_DOL():
    """W górę dałoby pozycję większą, niż zatwierdził risk kernel — najcichszy
    możliwy sposób złamania limitu, bo nie ma po nim żadnego śladu błędu."""
    assert do_kroku_w_dol(D("1.2349"), D("0.001")) == D("1.234")
    assert do_kroku_w_dol(D("1.2351"), D("0.001")) == D("1.235")
    i, _, powod = sprawdz_zlecenie(D("1.99999"), D("50000"), PELNE,
                                   kupno=True, wyjscie=False)
    assert powod is None
    assert i == D("1.999")


def test_cena_przyciagana_na_NASZA_korzysc_po_obu_stronach():
    """Przeciwny kierunek płaci o jeden krok więcej za każdym razem."""
    assert cena_do_kroku(D("100.007"), D("0.01"), kupno=True) == D("100.00")
    assert cena_do_kroku(D("100.003"), D("0.01"), kupno=False) == D("100.01")
    # Cena juz na siatce nie jest przesuwana w zadna strone.
    assert cena_do_kroku(D("100.00"), D("0.01"), kupno=False) == D("100.00")


def test_wyjscie_z_pozycji_krotkiej_to_KUPNO_a_nie_sprzedaz():
    """Sklejenie „wyjście = sprzedaż" jest prawdą tylko dla pozycji długiej.

    Wyprowadzenie strony z roli przyciągałoby limit w złą stronę przy każdym
    zamknięciu shorta — koszt mały, systematyczny i niewidoczny w wyniku.
    """
    _, c_kupno, _ = sprawdz_zlecenie(D("1"), D("100.003"), PELNE,
                                     kupno=True, wyjscie=True)
    _, c_sprzedaz, _ = sprawdz_zlecenie(D("1"), D("100.003"), PELNE,
                                        kupno=False, wyjscie=True)
    assert c_kupno == D("100.00")
    assert c_sprzedaz == D("100.01")


def test_BRAK_reguly_blokuje_tak_samo_jak_regula_zlamana():
    """„Giełda nie podała, więc pewnie dowolna precyzja" kończy się odrzuceniem
    pierwszego zlecenia na produkcji — w logu nie do odróżnienia od braku sygnału.

    Dziś dotyczy to 10 z 12 instrumentów dopuszczonych do handlu.
    """
    for brakujace in ("krok_ilosci", "krok_ceny", "min_wartosc"):
        f = Filtry(**{**{"krok_ilosci": D("0.001"), "krok_ceny": D("0.01"),
                         "min_wartosc": D("5")}, brakujace: None})
        _, _, powod = sprawdz_zlecenie(D("1"), D("100"), f, kupno=True, wyjscie=False)
        assert powod is not None and "nie podała reguł" in powod


def test_odmowa_WYJSCIA_jest_nazwana_inaczej_niz_odmowa_wejscia():
    """Odrzucone wejście kosztuje okazję. Odrzucone wyjście zostawia otwartą
    pozycję wbrew decyzji — i risk kernel liczy z niej ekspozycję."""
    _, _, wejscie = sprawdz_zlecenie(D("0.001"), D("100"), PELNE,
                                     kupno=True, wyjscie=False)
    _, _, wyjscie = sprawdz_zlecenie(D("0.001"), D("100"), PELNE,
                                     kupno=False, wyjscie=True)
    assert "wejście" in wejscie
    assert "WYJŚCIE" in wyjscie


def test_pozycja_ponizej_minimum_jest_NIE_DO_ZAMKNIECIA():
    """Najgorszy skutek wypełnienia częściowego: zlecenie na 100 weszło w 3,
    a reszty nie da się sprzedać. Nie boli, dopóki rynek nie spadnie."""
    assert czy_zamykalna(D("1"), D("50000"), PELNE) is True
    assert czy_zamykalna(D("0.00001"), D("50000"), PELNE) is False


def test_krok_zerowy_nie_przechodzi_po_cichu():
    """Zero jako krok przeszłoby każde sprawdzenie i wywróciło się dopiero
    przy dzieleniu — daleko od miejsca, w którym powstało."""
    with pytest.raises(ValueError):
        do_kroku_w_dol(D("1"), D("0"))


# ------------------------------------------------- uzupelnianie regul z gieldy

def test_instrument_nieznany_gieldzie_to_BRAK_a_nie_zerowe_reguly():
    """Zero jako krok przeszłoby każde sprawdzenie i wywróciło się dopiero przy
    dzieleniu — daleko od miejsca, w którym powstało."""
    from services.ingest.instrumenty import rozbieznosci

    baza = [{"id": 7, "symbol": "NIEISTNIEJE", "lot_size": None, "tick_size": None,
             "min_notional": None, "tradeable": True, "min_ilosc_meta": None}]
    (z,) = rozbieznosci(baza, {})
    assert z["brak_u_gieldy"] is True
    assert "zmiany" not in z


def test_niekompletne_liczy_TYLKO_dopuszczone_do_handlu():
    """Instrument wyłączony z handlu bez reguł nikomu nie szkodzi; blokowanie
    egzekucji z jego powodu zablokowałoby ją na zawsze."""
    from services.ingest.instrumenty import niekompletne

    baza = [
        {"symbol": "GOTOWY", "lot_size": D("1"), "tick_size": D("1"),
         "min_notional": D("5"), "tradeable": True},
        {"symbol": "NIEPELNY", "lot_size": None, "tick_size": D("1"),
         "min_notional": D("5"), "tradeable": True},
        {"symbol": "WYLACZONY", "lot_size": None, "tick_size": None,
         "min_notional": None, "tradeable": False},
    ]
    assert niekompletne(baza) == ["NIEPELNY"]


def test_rozbieznosci_nie_nadpisuja_regul_wartoscia_pusta():
    """Giełda potrafi nie podać pola. Wpisanie None na istniejącą regułę
    skasowałoby wiedzę, którą już mamy."""
    from services.ingest.instrumenty import rozbieznosci

    baza = [{"id": 1, "symbol": "SOLUSDC", "lot_size": D("0.01"),
             "tick_size": D("0.001"), "min_notional": D("1"),
             "tradeable": True, "min_ilosc_meta": "0.01"}]
    g = {"SOLUSDC": {"krok_ilosci": None, "krok_ceny": None, "min_wartosc": None,
                     "min_ilosc": D("0.01"), "status": "Trading"}}
    assert rozbieznosci(baza, g) == []


def test_krok_z_bazy_NIE_wnosi_swojej_skali_do_ilosci():
    """`lot_size` wraca z NUMERIC jako Decimal("0.001000000000").

    Mnożenie przenosi te dwanaście miejsc do wyniku i giełda odrzuca zlecenie:
    „Order quantity has too many decimals" (Bybit 170137). Wartość jest przy tym
    POPRAWNA — błędny jest sam zapis, więc objaw nie wskazuje na przyczynę.

    Wykryte próbą na prawdziwej giełdzie; testy tego nie łapały, bo zapisywały
    krok jako `0.001`, bez skali kolumny.
    """
    z_bazy = Filtry(krok_ilosci=D("0.001000000000"), min_ilosc=D("0.001000000000"),
                    min_wartosc=D("5.00000000"), krok_ceny=D("0.010000000000"))
    ilosc, cena, powod = sprawdz_zlecenie(D("1.2349"), D("50000.007"), z_bazy,
                                          kupno=True, wyjscie=False)
    assert powod is None
    assert str(ilosc) == "1.234"
    assert str(cena) == "50000"
    assert ilosc == D("1.234")                 # wartosc bez zmian


def test_bez_ogona_nie_robi_z_calkowitych_postaci_wykladniczej():
    """`normalize()` samo daje `2000` → `2E+3`, co giełda też odrzuci."""
    from services.engine.zlecenia import bez_ogona

    assert str(bez_ogona(D("2000.000"))) == "2000"
    assert str(bez_ogona(D("1.234000000"))) == "1.234"
    assert str(bez_ogona(D("0.010000000"))) == "0.01"


def test_cena_w_gore_takze_bez_ogona():
    """Dodanie kroku po zaokrągleniu wnosi skalę kroku z powrotem."""
    krok = D("0.010000000000")
    assert str(cena_do_kroku(D("100.003"), krok, kupno=False)) == "100.01"


def test_nieznana_strona_zlecenia_KONCZY_SIE_BLEDEM():
    """Poprzednia wersja brzmiała `"Buy" if strona == "buy" else "Sell"`, więc
    każda wartość poza dosłownym „buy" — w tym „Buy" — zamieniała kupno
    w sprzedaż.

    Objaw nie wygląda na błąd: giełda przyjmuje takie zlecenie i wykonuje je,
    tylko w przeciwną stronę niż decyzja. W dzienniku wygląda to jak strategia,
    która zrobiła coś dziwnego.
    """
    from services.exchanges.bybit import _strona_bybita

    assert _strona_bybita("buy") == "Buy"
    assert _strona_bybita("Buy") == "Buy"
    assert _strona_bybita("sell") == "Sell"
    assert _strona_bybita("SELL") == "Sell"
    for zla in ("kup", "", "long", "b"):
        with pytest.raises(ValueError):
            _strona_bybita(zla)


def test_reguly_zlecen_bierzemy_z_hosta_PODMIOTU_a_nie_globalnego():
    """Dane rynkowe biorą się z podmiotu globalnego (jego księga posłużyła do
    trenowania modelu). REGUŁY ZLECEŃ są czym innym: obowiązują tam, gdzie
    zlecenie ląduje.

    Zmierzone 2026-09-07 na ADAUSDC — `basePrecision` 0.01 globalnie, 0.1
    u podmiotu unijnego. Czytanie hosta globalnego kończyło się odbiciem
    każdego zlecenia komunikatem „Order quantity has too many decimals", który
    wskazuje na nasze zaokrąglanie zamiast na źródło reguły.
    """
    import asyncio

    from services.exchanges.bybit import BybitAdapter

    a = BybitAdapter.__new__(BybitAdapter)
    a._category = "spot"
    a._host = "https://api-demo.bybit.eu"
    uzyte = {}

    async def _get(path, params=None, *, private=True, host=None):
        uzyte.update(path=path, host=host, private=private)
        return {"list": []}

    a._get = _get
    asyncio.run(a.filtry_instrumentow("spot"))
    assert uzyte["host"] == "https://api-demo.bybit.eu"
    assert uzyte["private"] is False        # endpoint publiczny, bez klucza


def test_zlecenia_ida_jako_PostOnly_a_nie_GTC():
    """Limit przecinający spread jest wypełniany natychmiast jako taker —
    u podmiotu unijnego po 0,250% zamiast 0,100%. Po cichu: w dzienniku wygląda
    jak zwykłe wypełnienie.

    Zdarza się to często, bo cenę wyliczamy z księgi globalnej, a zlecenie ląduje
    w unijnej. Zmierzone 2026-09-07 na 1600 świecach 4h: w 16,8% świec rozjazd
    zamknięć przekracza CAŁĄ rundę kosztową.

    Symulacja zakłada makera bez poślizgu, więc PostOnly nie zmienia strategii —
    urzeczywistnia jej założenie. Odrzucenie jest przy tym widoczne dla czujki
    zdrowia egzekucji; cichy taker nie jest widoczny dla niczego.
    """
    from pathlib import Path

    zrodlo = Path("services/exchanges/bybit.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    # DWA CIALA ZAPYTANIA OD 2026-09-27, wiec `split(...)[1]` przestal znaczyc
    # "cialo zlecenia". Pierwsza galezia jest RYNKOWA (likwidacja po
    # kill-switchu), druga - z limitem. Test bierze je po ZAWARTOSCI, a nie po
    # kolejnosci: numer galezi zmienilby sie przy kazdej edycji adaptera, a
    # wtedy to twierdzenie milczaloby o tej galezi, o ktora naprawde chodzi.
    ciala = kod.split("/v5/order/create")[1:]
    assert len(ciala) == 2, (
        "adapter Bybita ma inną liczbę ścieżek składania zlecenia niż dwie "
        "(rynkowa + z limitem) — ten test przestał wiedzieć, co mierzy")
    ciala = [c.split("})")[0] for c in ciala]
    z_limitem = [c for c in ciala if '"orderType": "Limit"' in c]
    rynkowe = [c for c in ciala if '"orderType": "Market"' in c]
    assert len(z_limitem) == 1 and len(rynkowe) == 1, ciala

    # SCIEZKA NORMALNEGO HANDLU: maker zostaje makerem.
    assert '"timeInForce": "PostOnly"' in z_limitem[0]
    assert '"timeInForce": "GTC"' not in z_limitem[0]

    # SCIEZKA RYNKOWA: ANI PostOnly, ANI ceny.
    #
    # `PostOnly` na zleceniu rynkowym jest sprzecznoscia, ktora gielda odrzuca,
    # wiec likwidacja po kill-switchu nie wyszlaby wcale - i wygladaloby to na
    # dzialajacy bezpiecznik. `price` na zleceniu rynkowym z kolei nie jest
    # bledem gieldy, tylko naszym: cena wyladowalaby w `exec.order.limit_price`
    # i pomiar poslizgu wzialby ja za cene odniesienia, choc gielda jej nie
    # dostala.
    assert '"timeInForce"' not in rynkowe[0], (
        "zlecenie rynkowe z `timeInForce` — PostOnly jest tu sprzecznością, "
        "którą giełda odrzuca, a odrzucona likwidacja wygląda jak działający "
        "bezpiecznik")
    assert '"price"' not in rynkowe[0], (
        "zlecenie rynkowe z ceną — trafiłaby do `exec.order.limit_price` "
        "i pomiar poślizgu wziąłby ją za cenę odniesienia")
    # JEDNOSTKA ILOSCI JAWNIE. Na spocie Bybit liczy `qty` zlecenia rynkowego
    # KUPNA w walucie kwotowanej, a sprzedazy w aktywie bazowym - odkup pozycji
    # krotkiej na 1000 sztuk zamowilby 1000 USDC. Bez bledu i bez odmowy.
    assert '"marketUnit": "baseCoin"' in rynkowe[0], (
        "zlecenie rynkowe bez jawnej jednostki ilości — na spocie Bybita `qty` "
        "przy kupnie znaczy KWOTĘ, a przy sprzedaży ILOŚĆ")
