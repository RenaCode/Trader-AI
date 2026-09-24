"""Ułamek wypełnienia liczony z `exec.fill` nie może zmierzyć NIEWYPEŁNIENIA.

CO RAPORTOWAŁA CZUJKA

    ułamek wypełnienia: n=4, średnio 1.000 (symulacja przyjmuje 1.000),
    odch. 0.000, przedział 95% [1.000, 1.000]; częściowych 0

Czyta się to jako „symulacja zakłada 1,000 i pomiar to potwierdza". Nie
potwierdza i potwierdzić nie może.

DLACZEGO

`exec.fill` ma wiersz WYŁĄCZNIE dla zlecenia, które się wypełniło. Zlecenie
złożone i niewypełnione nie zostawia tam śladu, więc próba jest z definicji
warunkowana na „coś weszło". Pytanie, na które backtest potrzebuje odpowiedzi,
brzmi inaczej: ILE ZE ZŁOŻONYCH ZLECEŃ weszło w ogóle. Tej liczby w tej próbie
nie ma i żadna jej wielkość jej nie przywróci — przy tysiącu wypełnień średnia
dalej wynosiłaby 1,000, a symulacja dalej byłaby nieodróżnialna od prawdy.

Docstring `ulamki()` mówi o tym warunkowaniu wprost („próba jest z natury
warunkowana na »coś weszło«"), ale WIERSZ RAPORTU tego nie mówi — a czytany
jest wiersz raportu. To ta sama odległość między wiedzą modułu a tym, co widzi
człowiek, co przy poślizgu makera.

GDZIE JEST PRAWDZIWA LICZBA

W `exec.order.status`. Zlecenie wysłane ma tam wiersz niezależnie od tego, czy
weszło. Stąd druga, osobna próba: `wypelnialnosc()` — ile ze złożonych zleceń
się wypełniło. To ona, a nie średnia z `exec.fill`, odpowiada na pytanie
backtestu.
"""
from services.engine.poslizg import opis, pomiar, wypelnialnosc


def fill(**zmiana):
    baza = {"rodzaj_zlecenia": "limit", "strona": "buy", "zrodlo": "gielda",
            "ulamek": "1", "rodzaj_wypelnienia": "pelne",
            "cena_zlecenia": "100", "cena_wykonania": "100"}
    return {**baza, **zmiana}


def test_opis_MOWI_ze_ulamek_z_fill_nie_widzi_niewypelnien():
    """Bez tego zdania „średnio 1.000" czyta się jak potwierdzenie założenia."""
    linie = " | ".join(opis(pomiar([fill() for _ in range(4)])))
    assert "nie zawiera NIEWYPEŁNIEŃ" in linie


# --- wypelnialnosc: proba, w ktorej niewypelnienie W OGOLE MOZE wystapic ----

def test_niewypelnione_zlecenie_JEST_w_probie():
    w = wypelnialnosc([{"status": "filled"}, {"status": "filled"},
                       {"status": "cancelled"}, {"status": "expired"}])
    assert w["wyslanych"] == 4
    assert w["wypelnionych"] == 2
    assert w["niewypelnionych"] == 2
    assert abs(w["udzial"] - 0.5) < 1e-12


def test_zlecenie_JESZCZE_NIEROZSTRZYGNIETE_nie_liczy_sie_do_zadnej_strony():
    """`new` leży w arkuszu, `unknown` to zerwana odpowiedź giełdy. Żadne
    z nich nie jest jeszcze wynikiem — zaliczenie ich do niewypełnionych
    zaniżałoby wypełnialność o wszystko, co akurat jest w locie."""
    w = wypelnialnosc([{"status": "filled"}, {"status": "new"},
                       {"status": "unknown"}])
    assert w["wyslanych"] == 3
    assert w["nierozstrzygnietych"] == 2
    assert w["niewypelnionych"] == 0
    assert abs(w["udzial"] - 1.0) < 1e-12


def test_czesciowe_liczy_sie_OSOBNO_a_nie_jako_pelne():
    """Częściowe wejście to inna wielkość pozycji niż zakładana — i inna
    niż brak wejścia. Zsumowanie go z pełnym gubi dokładnie tę różnicę."""
    w = wypelnialnosc([{"status": "filled"}, {"status": "partial"},
                       {"status": "rejected"}])
    assert (w["wypelnionych"], w["czesciowych"], w["niewypelnionych"]) == (1, 1, 1)


def test_pusta_proba_daje_None_a_NIE_jedynki():
    """Brak zleceń to brak wiedzy. Udział 1,0 przy zerze obserwacji byłby
    najgorszą z możliwych odpowiedzi: dokładnie tym, co zakłada symulacja."""
    w = wypelnialnosc([])
    assert w["wyslanych"] == 0
    assert w["udzial"] is None


def test_nieznany_status_NIE_JEST_zgadywany():
    """Status spoza znanego zbioru nie trafia ani do wypełnionych, ani do
    niewypełnionych — wpada do osobnego licznika z nazwą."""
    w = wypelnialnosc([{"status": "filled"}, {"status": "dziwny"}])
    assert w["wypelnionych"] == 1
    assert w["niewypelnionych"] == 0
    assert w["nieznany_status"] == {"dziwny": 1}
    # Udzial liczony z rozstrzygnietych, nie z wszystkich - inaczej jeden
    # wiersz o nieznanym statusie zanizalby wypelnialnosc jako niewypelnienie.
    assert abs(w["udzial"] - 1.0) < 1e-12


def test_opis_wypisuje_wypelnialnosc_gdy_JEST_co_wypisac():
    linie = " | ".join(opis(pomiar([fill()]) | {
        "wypelnialnosc": wypelnialnosc([{"status": "filled"},
                                        {"status": "cancelled"}])}))
    assert "2 złożonych" in linie or "2 zleceń" in linie
    assert "50" in linie


def test_opis_MOWI_NIE_ZMIERZONO_gdy_zlecen_nie_ma():
    linie = " | ".join(opis(pomiar([fill()]) | {"wypelnialnosc": wypelnialnosc([])}))
    assert "NIE ZMIERZONO" in linie.upper()
