"""Czy host w ogole wystawia kontrakty wieczyste - pomiar, nie komentarz.

DLACZEGO TEN PLIK ISTNIEJE

Kierunek "odblokujmy krotka strone kontraktami perpetual na Bybicie, ta sama
gielda, te same klucze" stoi albo upada na jednej liczbie: ile perpetuali
wystawia host, na ktorym stoi konto. W projekcie byla ona zapisana w trzech
miejscach naraz - w docstringu `bybit.py`, w `mozliwosci.py` i w migracji 0018 -
zawsze jako ten sam pomiar z 2026-09-04:

    bybit.com   541 par spot, 399 par *USDT, 859 kontraktow wieczystych
    bybit.eu    140 par spot,   4 pary *USDT,   0 kontraktow wieczystych

Trzy kopie jednej liczby sprzed trzech tygodni to nie jest potwierdzenie -
to jest jedno zdanie przepisane trzy razy. Oferta gieldy zmienia sie bez
naszego udzialu, a ta akurat liczba rozstrzyga, czy caly kierunek prac jest
wykonalny.

CO TE TESTY SPRAWDZAJA, A CZEGO NIE

Nie pytaja gieldy - to robi `--kategorie` na zywym hoscie. Sprawdzaja REGULE
CZYTANIA odpowiedzi, bo to w niej siedzi rozroznienie, ktore latwo zgubic:
"zapytalismy i nie ma" to co innego niz "nie udalo sie zapytac", a zrownanie
ich zamienia awarie sieci w wniosek o ofercie gieldy.
"""
from services.exchanges.mozliwosci import KATEGORIE, ocena_perpetuali


def test_host_z_perpetualami_pozwala_na_krotka_strone():
    mozna, powod = ocena_perpetuali({"spot": 541, "linear": 859, "inverse": 20,
                                     "option": 0})
    assert mozna
    assert "879" in powod, "powód ma podać zmierzoną liczbę, a nie sam werdykt"


def test_host_BEZ_perpetualow_NIE_pozwala_na_krotka_strone():
    """Stan podmiotu unijnego, zmierzony na zywo 2026-09-24: spot 127,
    linear 0, inverse 0, option 0."""
    mozna, powod = ocena_perpetuali({"spot": 127, "linear": 0, "inverse": 0,
                                     "option": 0})
    assert not mozna
    assert "ANI JEDNEGO" in powod


def test_ZERO_to_inny_wynik_niz_BRAK_POMIARU():
    """SEDNO TEGO PLIKU.

    Host, ktory odpowiedzial "nie mam perpetuali", i host, ktorego nie udalo
    sie zapytac, daja ten sam werdykt `mozna=False` - i to jest poprawne,
    bo w obu przypadkach nie wolno zaczynac. Ale POWODY musza byc rozne:
    pierwszy zamyka temat ("potrzebna inna gielda"), drugi otwiera go od nowa
    ("zapytaj jeszcze raz"). Podjecie decyzji o zmianie gieldy na podstawie
    nieudanego zapytania byloby dokladnie tym bledem, ktory `mozliwosci.py`
    opisuje w swoim docstringu: wnioskiem o ofercie wyciagnietym z awarii.
    """
    _, powod_zero = ocena_perpetuali({"spot": 127, "linear": 0, "inverse": 0})
    _, powod_brak = ocena_perpetuali({"spot": 127})
    assert powod_zero != powod_brak
    assert "pomiaru po prostu nie ma" in powod_brak
    assert "pomiaru po prostu nie ma" not in powod_zero


def test_opcje_nie_licza_sie_jako_krotka_strona():
    """Opcja pozwala obstawic spadek, ale jest INNYM INSTRUMENTEM - ma premie,
    date wygasniecia i wlasny profil ryzyka. Zaliczenie jej do krotkiej nogi
    tej strategii byloby nazwaniem dwoch roznych rzeczy jedna nazwa."""
    mozna, _ = ocena_perpetuali({"spot": 127, "linear": 0, "inverse": 0,
                                 "option": 500})
    assert not mozna


def test_linear_jest_wsrod_sprawdzanych_kategorii():
    """Kanarek: gdyby `linear` wypadlo z listy, `--kategorie` meldowaloby
    'nie wiadomo' na hoscie, ktory perpetuale ma."""
    assert "linear" in KATEGORIE
    assert "spot" in KATEGORIE
