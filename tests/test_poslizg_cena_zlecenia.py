"""Poślizg mierzony względem ceny, KTÓRĄ GIEŁDA DOSTAŁA.

TŁO

`poslizg.py` brał za cenę odniesienia `journal.planned_order.limit_price` —
cenę, na której stanął plan. Tymczasem dla KAŻDEGO wyjścia
`zlecenia.sprawdz_zlecenie` podmienia cenę limitu na `cena_makera` z księgi
DOCELOWEJ i dopiero ta cena idzie na giełdę oraz trafia do
`exec.order.limit_price`. Poślizg liczył się więc względem ceny, której giełda
nigdy nie zobaczyła.

DLACZEGO TO NIE JEST DROBIAZG

Rozjazd między księgą globalną (z niej powstaje plan, na niej trenuje się model)
a księgą podmiotu docelowego jest zmierzony i duży: w 16,8% świec przekracza
całą rundę kosztową (`gielda_exec.py`). Wyjście wypełnione CO DO GROSZA po
naszej cenie — czyli egzekucja doskonała — raportowało się jako +29 pb, przy
założeniu POSLIZG_MAKER = 0. Trzy takie wyjścia wystarczały, żeby przedział
ufności wykluczył stałą i moduł wydał werdykt „założenie poza przedziałem",
czyli formalne żądanie podniesienia stałej kosztowej — na podstawie własnego
błędu pomiaru. To jest ta sama klasa wady, przed którą ostrzega nagłówek
modułu przy rachunku papierowym: liczba wyglądająca na pomiar egzekucji,
a będąca pomiarem nas samych.

CENA WYSŁANA JEST TU LICZONA, A NIE WPISANA

Testy niżej wołają `sprawdz_zlecenie` — tę samą funkcję, która podmienia cenę
w produkcji. Gdyby podmiana zniknęła albo zmieniła regułę, ten plik przestaje
opisywać rzeczywistość w sposób WIDOCZNY, zamiast utrwalać liczbę przepisaną
z jednego przebiegu.
"""
from decimal import Decimal as D

from services.engine.koszty import POSLIZG_MAKER
from services.engine.poslizg import (
    NIEODROZNIALNE,
    ROZBIEZNE,
    TOZSAMOSC,
    ZAPYTANIE,
    cena_odniesienia,
    opis,
    pomiar,
    poslizg_wiersza,
    rozjazd_ksiag,
    statystyki,
)
from services.engine.zlecenia import Filtry, sprawdz_zlecenie

FILTRY = Filtry(krok_ilosci=D("0.001"), krok_ceny=D("0.01"),
                min_wartosc=D("1"), min_ilosc=None)

# Plan wyjscia stoi na cenie z ksiegi GLOBALNEJ; ksiega DOCELOWA ma 99.70,
# czyli 30 pb nizej - miedzy mediana (5,9 pb) a p90 (28,4 pb) rozjazdu
# zmierzonego w tym projekcie.
CENA_PLANU = D("100.00")
KSIEGA_DOCELOWA = D("99.70")


def _cena_wyslana() -> D:
    """Cena, którą `sprawdz_zlecenie` naprawdę wyśle dla tego wyjścia."""
    _ilosc, cena, powod = sprawdz_zlecenie(
        D("1"), CENA_PLANU, FILTRY, kupno=False, wyjscie=True,
        cena_odniesienia=KSIEGA_DOCELOWA, atr_pct=1.0)
    assert powod is None, f"zlecenie odrzucone przed wysłaniem: {powod}"
    return cena


def wiersz(**zmiana) -> dict:
    """Wypełnienie wyjścia z giełdy, wypełnione DOKŁADNIE po naszej cenie."""
    wyslana = _cena_wyslana()
    baza = {
        "rodzaj_zlecenia": "limit",
        "strona": "sell",
        "zrodlo": "gielda",
        "ulamek": "1",
        "rodzaj_wypelnienia": "pelne",
        "cena_zlecenia": wyslana,        # exec.order.limit_price
        "cena_planu": CENA_PLANU,        # journal.planned_order.limit_price
        "cena_decyzji": CENA_PLANU,      # journal.decision.target_price
        "cena_wykonania": wyslana,       # exec.fill.price
    }
    return {**baza, **zmiana}


def test_podmiana_ceny_limitu_NAPRAWDE_zachodzi():
    """Warunek wstępny całego pliku, sprawdzony na produkcyjnej funkcji.

    Gdyby `sprawdz_zlecenie` przestało podmieniać cenę wyjścia, testy niżej
    mierzyłyby dwie równe liczby i zielenieły bez względu na poprawkę.
    """
    assert _cena_wyslana() != CENA_PLANU, (
        "cena wysłana równa planowanej — podmiana na cenę makera z księgi "
        "docelowej zniknęła, a z nią sens tego pliku")


def test_odniesieniem_jest_CENA_ZLECENIA_a_nie_cena_planu():
    """Zobowiązanie wobec giełdy dotyczy wyłącznie ceny, którą giełda dostała.
    Cena planu jest zapasem dla wierszy, które ceny zlecenia nie mają."""
    c, skad = cena_odniesienia(wiersz())
    assert (c, skad) == (_cena_wyslana(), "zlecenie")


def test_bez_ceny_zlecenia_odniesieniem_zostaje_PLAN():
    """Kanarek do poprzedniego: kolejność ma dokładać źródło, a nie zabierać.
    Zlecenia sprzed zapisu `exec.order.limit_price` i wiersze rachunku
    papierowego nadal muszą mieć od czego liczyć."""
    c, skad = cena_odniesienia(wiersz(cena_zlecenia=None))
    assert (c, skad) == (CENA_PLANU, "plan")


def test_IDEALNE_wykonanie_daje_poslizg_ZERO_a_nie_trzydziesci_pb():
    """SEDNO POPRAWKI.

    Giełda wypełniła co do grosza po cenie, na którą się zobowiązała: poślizg
    jest zerowy i taki ma być wynik. Przed poprawką wychodziło tu blisko +30 pb
    — czyli półtora rundy kosztowej wzięte z rozjazdu dwóch NASZYCH cen.
    """
    p, skad = poslizg_wiersza(wiersz())
    assert skad == "zlecenie"
    assert abs(p) < 1e-12, f"poślizg {p * 1e4:+.2f} pb przy wypełnieniu po naszej cenie"


def test_wypelnienie_GORSZE_od_ceny_zlecenia_NADAL_jest_poslizgiem():
    """Kanarek do poprzedniego. Poprawka ma przesunąć punkt odniesienia,
    a nie wyciszyć pomiar: sprzedaż poniżej ceny limitu to prawdziwy koszt
    i musi wyjść dodatnia."""
    wyslana = _cena_wyslana()
    p, _ = poslizg_wiersza(wiersz(cena_wykonania=wyslana - D("0.05")))
    assert p > 0
    assert abs(p - float(D("0.05") / wyslana)) < 1e-12


def test_trzy_IDEALNE_wyjscia_NIE_zadaja_podniesienia_stalej():
    """Werdykt `założenie poza przedziałem` jest w tym module formalnym
    żądaniem ruszenia stałej kosztowej. Przed poprawką wystarczały do niego
    trzy doskonale wykonane wyjścia — bo wszystkie trzy niosły ten sam
    rozjazd ksiąg, wpisany w poślizg."""
    s = statystyki([wiersz() for _ in range(3)])
    assert s["maker"]["n"] == 3
    assert s["maker"]["werdykt"] != ROZBIEZNE, (
        "moduł żąda podniesienia stałej na podstawie własnego błędu pomiaru")
    # WERDYKT ZAOSTRZONY 2026-09-24, wcześniej stało tu `== NIEODROZNIALNE`.
    #
    # Trzy wypełnienia co do grosza po cenie zlecenia to trzy zera policzone
    # jako (x − x)/x — przedział ufności ma zerową szerokość i obejmuje
    # założenie, więc `nieodróżnialne od założenia` wychodziło stąd zawsze,
    # niezależnie od tego, jaka była prawdziwa egzekucja. Czytało się to jako
    # „zmierzone i zgadza się", a było arytmetyką. To nie jest złagodzenie
    # dawnego warunku: `!= ROZBIEZNE` obowiązuje dalej, a do rodziny werdyktów
    # potwierdzających ta próba już nie należy. Patrz test_poslizg_tozsamosc.py.
    assert s["maker"]["werdykt"] == TOZSAMOSC
    assert s["maker"]["tozsamosciowych"] == 3
    # Przedzial nadal sie liczy i nadal obejmuje zalozenie - zmienil sie
    # WERDYKT, czyli to, co z tego przedzialu wolno wyczytac.
    lo, hi = s["maker"]["przedzial"]
    assert lo <= POSLIZG_MAKER <= hi


def test_ZAPYTANIE_naprawde_pobiera_cene_zlecenia():
    """Sama kolejność w `cena_odniesienia` nie wystarczy i to jest tu istotne:
    klucza, którego SELECT nie pobiera, nie będzie w wierszu z bazy, więc
    funkcja cicho spadnie na cenę planu i wszystko wyżej dalej przechodzi.
    Ten test pilnuje drugiej połowy poprawki — tej po stronie zapytania."""
    zwarte = " ".join(ZAPYTANIE.split())
    assert "o.limit_price AS cena_zlecenia" in zwarte
    assert "p.limit_price AS cena_planu" in zwarte, (
        "cena planu wypadła z zapytania — a bez niej nie ma czym policzyć "
        "rozjazdu ksiąg ani od czego liczyć starszych wierszy")


# ----------------------------------------------- rozjazd ksiąg: osobna wielkość

def test_rozjazd_ksiag_jest_MIERZONY_ale_NIE_JAKO_poslizg():
    """Różnica `plan − cena wysłana` jest sensowną wielkością i nie ma powodu
    jej gubić. Ma jednak własną nazwę, bo odpowiada na inne pytanie: jak daleko
    księga docelowa stoi od tej, na której trenowany jest model. Wliczona
    w poślizg była żądaniem podniesienia stałej kosztowej."""
    r = rozjazd_ksiag([wiersz() for _ in range(3)])
    assert r["n"] == 3
    oczekiwany = abs(float((CENA_PLANU - _cena_wyslana()) / _cena_wyslana()))
    assert abs(r["mediana"] - oczekiwany) < 1e-12
    assert r["mediana"] > 0.002, "rozjazd rzędu 30 pb miał nie zniknąć z pomiaru"

    # ...i ani jeden punkt bazowy z niego nie wchodzi do poslizgu.
    s = statystyki([wiersz() for _ in range(3)])
    assert abs(s["maker"]["srednia"]) < 1e-12


def test_rozjazd_ksiag_NIE_liczy_wierszy_rachunku_papierowego():
    """Rachunek papierowy nie składa zlecenia na żadnej giełdzie, więc nie ma
    dwóch ksiąg do porównania. Policzony tu byłby zerem udającym pomiar."""
    assert rozjazd_ksiag([wiersz(zrodlo="internal_paper")])["n"] == 0


def test_opis_nazywa_rozjazd_ksiag_i_MOWI_ze_to_nie_poslizg():
    """Liczba rzędu 30 pb w raporcie o poślizgu zostanie przeczytana jako
    poślizg, jeśli nie napisać przy niej, czym jest."""
    linie = opis(pomiar([wiersz() for _ in range(3)]))
    rozjazd = [x for x in linie if "rozjazd ksiąg" in x]
    assert rozjazd, "rozjazd ksiąg policzony, ale niewypisany"
    assert "NIE poślizg" in rozjazd[0]


def test_brak_rozjazdu_nie_dokłada_linii_do_raportu():
    """Kanarek na hałas: przy pustym pomiarze raport ma nadal mówić wyłącznie
    o tym, czego nie zmierzono."""
    assert not [x for x in opis(pomiar([])) if "rozjazd ksiąg" in x]
