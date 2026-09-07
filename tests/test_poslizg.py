"""Pomiar poślizgu: co ma zmierzyć i czego NIE WOLNO mu pokazać jako wyniku.

DLACZEGO TO JEST TESTOWANE ZANIM PADNIE PIERWSZE WYPEŁNIENIE

Konto demo handluje od dziś i nie ma jeszcze ani jednej transakcji. Narzędzie
do mierzenia poślizgu musi więc być sprawdzone PRZED pierwszym wypełnieniem —
po nim jest już za późno, bo właśnie tamta obserwacja jest tą, której nie chcemy
stracić na błędzie w liczeniu. Stąd czysta funkcja na listach wierszy: cały
rachunek da się sprawdzić bez bazy, bez giełdy i bez czekania.

DWA BŁĘDY, KTÓRE WYGLĄDAŁYBY JAK POPRAWNY WYNIK

  1. policzenie poślizgu z wierszy rachunku papierowego. Symulacja zapisuje cenę
     jako `cena * (1 ± POSLIZG_TAKER)`, więc pomiar oddałby DOKŁADNIE założoną
     stałą i wyglądał jak jej świetne potwierdzenie, będąc wyłącznie echem,
  2. zwrócenie zera przy pustej próbie. Zero poślizgu to znakomity wynik,
     brak pomiaru to brak wiedzy — a stała zostaje wtedy założeniem.
"""
from decimal import Decimal as D

from services.engine.egzekucja import rozjazd_z_ulamkow
from services.engine.koszty import POSLIZG_MAKER, POSLIZG_TAKER
from services.engine.poslizg import (
    NIE_ZMIERZONO,
    NIEODROZNIALNE,
    ROZBIEZNE,
    ZA_MALO,
    cena_odniesienia,
    opis,
    pomiar,
    poslizg_wiersza,
    rodzaj_zlecenia,
    statystyki,
    ulamki,
    werdykt,
)
from services.engine.statystyka import przedzial_sredniej


def wiersz(**zmiana):
    """Wypełnienie z giełdy: kupno zlecenie z limitem, wypełnione po limicie."""
    baza = {
        "rodzaj_zlecenia": "limit",
        "strona": "buy",
        "zrodlo": "gielda",
        "ulamek": "1",
        "rodzaj_wypelnienia": "pelne",
        "cena_planu": D("100"),
        "cena_decyzji": D("100"),
        "cena_wykonania": D("100"),
    }
    return {**baza, **zmiana}


# --- znak i cena odniesienia ------------------------------------------------

def test_kupno_drozej_niz_odniesienie_to_poslizg_DODATNI():
    """Dodatni zawsze znaczy „na naszą niekorzyść"."""
    p, _ = poslizg_wiersza(wiersz(cena_wykonania=D("100.05")))
    assert p > 0
    assert abs(p - 0.0005) < 1e-12


def test_sprzedaz_taniej_niz_odniesienie_TEZ_daje_poslizg_dodatni():
    """Bez tej normalizacji kupna i sprzedaże kasowałyby się w średniej przy
    dowolnie złej egzekucji — a to jest cały mierzony efekt."""
    p, _ = poslizg_wiersza(wiersz(strona="sell", cena_wykonania=D("99.95")))
    assert p > 0
    assert abs(p - 0.0005) < 1e-12


def test_sprzedaz_drozej_niz_odniesienie_to_poslizg_UJEMNY():
    """Wypełnienie po cenie lepszej niż limit jest zyskiem, nie kosztem."""
    p, _ = poslizg_wiersza(wiersz(strona="sell", cena_wykonania=D("100.05")))
    assert p < 0


def test_odniesieniem_dla_limitu_jest_CENA_PLANU_a_nie_decyzji():
    """Limit jest zobowiązaniem złożonym giełdzie i to względem niego mierzy się
    jakość wypełnienia. Cena decyzji bywa inna — plan powstaje z odsunięciem."""
    c, skad = cena_odniesienia(wiersz(cena_planu=D("99"), cena_decyzji=D("100")))
    assert (c, skad) == (D("99"), "plan")


def test_bez_planu_odniesieniem_jest_cena_decyzji():
    """Zlecenie rynkowe nie ma limitu; `target_price` jest jedyną ceną, na której
    decyzja była oparta — i dokładnie tym, co modeluje POSLIZG_TAKER."""
    c, skad = cena_odniesienia(wiersz(cena_planu=None))
    assert (c, skad) == (D("100"), "decyzja")


def test_brak_obu_cen_odniesienia_to_ODRZUCENIE_a_nie_zero():
    """Wiersz bez ceny odniesienia policzony jako zero ściągałby średnią do zera
    tym mocniej, im więcej zapisu jest zepsute — czyli awaria zapisu wyglądałaby
    jak doskonała egzekucja."""
    p, powod = poslizg_wiersza(wiersz(cena_planu=None, cena_decyzji=None))
    assert p is None
    assert "odniesienia" in powod


# --- pułapka: mierzenie własnego założenia ----------------------------------

def test_wiersz_z_rachunku_papierowego_NIE_JEST_MIERZONY():
    """NAJWAŻNIEJSZY TEST W TYM PLIKU.

    Symulacja zapisuje cenę wypełnienia jako `cena * (1 + POSLIZG_TAKER)`.
    Policzenie z tego poślizgu oddałoby dokładnie 4 punkty bazowe i wyglądałoby
    jak wzorowe potwierdzenie stałej — będąc wyłącznie jej odbiciem w lustrze.
    Taki „pomiar" jest gorszy niż brak pomiaru, bo zamyka pytanie.
    """
    papierowy = wiersz(zrodlo="internal_paper",
                       cena_wykonania=D("100") * (1 + D(str(POSLIZG_TAKER))))
    p, powod = poslizg_wiersza(papierowy)
    assert p is None
    assert "własne założenie" in powod

    s = statystyki([papierowy])
    assert s["maker"]["n"] == 0 and s["taker"]["n"] == 0
    assert sum(s["pominiete"].values()) == 1


def test_papierowy_ulamek_tez_nie_wchodzi_do_proby():
    """Rachunek papierowy wypełnia wszystko w całości, więc jego ułamki są
    stałą 1.0 przebraną za pomiar — dokładnie tym, co mamy sprawdzić."""
    assert ulamki([wiersz(zrodlo="internal_paper")])["n"] == 0


# --- brak pomiaru kontra zmierzone zero -------------------------------------

def test_pusta_proba_daje_None_a_NIE_zero():
    """None znaczy „nie zmierzono", zero znaczyłoby „zmierzono i poślizgu nie ma".
    Drugie jest znakomitym wynikiem, pierwsze jest brakiem wiedzy — i tylko przy
    pierwszym stała pozostaje założeniem."""
    s = statystyki([])
    for rodzaj in ("maker", "taker"):
        assert s[rodzaj]["n"] == 0
        assert s[rodzaj]["srednia"] is None
        assert s[rodzaj]["mediana"] is None
        assert s[rodzaj]["odchylenie"] is None
        assert s[rodzaj]["przedzial"] is None
        assert s[rodzaj]["werdykt"] == NIE_ZMIERZONO


def test_zmierzone_zero_wyglada_INACZEJ_niz_brak_pomiaru():
    """Kanarek na całe rozróżnienie: dwadzieścia wypełnień dokładnie po limicie
    to poślizg zerowy ZMIERZONY, i musi się różnić od pustej próby czymś więcej
    niż samą liczbą."""
    s = statystyki([wiersz() for _ in range(20)])
    assert s["maker"]["n"] == 20
    assert s["maker"]["srednia"] == 0.0
    assert s["maker"]["werdykt"] != NIE_ZMIERZONO


def test_opis_nazywa_brak_pomiaru_wprost():
    """Wynik ma być czytelny dla człowieka, a wypisana średnia przy pustej
    próbie czytałaby się jak potwierdzenie założenia z zera obserwacji.

    Wartość samego ZAŁOŻENIA wolno wypisać i jest wypisywana — po to, żeby
    było widać, czego pomiaru brakuje.
    """
    linie = opis(pomiar([]))
    assert any("NIE ZMIERZONO" in x for x in linie)
    # Zadna linia nie podaje zmierzonej statystyki, bo zadnej nie ma.
    assert not any("średnia" in x or "mediana" in x or "przedział" in x
                   for x in linie)


def test_opis_mowi_ze_symulacja_zawyza_pozycje_gdy_ulamka_nie_ma():
    linie = opis(pomiar([]))
    assert any("1.0" in x and "NIE ZMIERZONO" in x for x in linie)


# --- rozdzielenie maker/taker ------------------------------------------------

def test_maker_i_taker_sa_LICZONE_OSOBNO():
    """Obie stałe dotyczą czegoś innego: limit nie przechodzi przez spread
    (założenie zero), rynkowe przechodzi zawsze (założenie 4 pb). Wspólna
    średnia nie dałaby się zestawić z żadną z nich."""
    s = statystyki([
        wiersz(rodzaj_zlecenia="limit", cena_wykonania=D("100")),
        wiersz(rodzaj_zlecenia="market", cena_wykonania=D("100.06")),
    ])
    assert s["maker"]["n"] == 1 and s["taker"]["n"] == 1
    assert s["maker"]["zalozenie"] == POSLIZG_MAKER
    assert s["taker"]["zalozenie"] == POSLIZG_TAKER


def test_zalozenie_jest_importowane_a_nie_przepisane():
    """Przepisana kopia stałej rozjechałaby się przy pierwszej jej zmianie,
    a pomiar porównywałby się wtedy z nieaktualnym założeniem i milczał."""
    s = statystyki([])
    assert s["taker"]["zalozenie"] is POSLIZG_TAKER or \
        s["taker"]["zalozenie"] == POSLIZG_TAKER


def test_nieznany_typ_zlecenia_NIE_JEST_zgadywany():
    """Zaliczenie go na chybił trafił do makera porównałoby wynik z założeniem
    zero i wyglądało na katastrofę egzekucji."""
    assert rodzaj_zlecenia("stop") == "nieznany"
    s = statystyki([wiersz(rodzaj_zlecenia="stop", cena_wykonania=D("100.06"))])
    assert s["maker"]["n"] == 0 and s["taker"]["n"] == 0
    assert sum(s["pominiete"].values()) == 1


# --- rozrzut i próg ----------------------------------------------------------

def test_jedno_wypelnienie_NIE_JEST_pomiarem_poslizgu():
    """Ma cenę i ma poślizg, ale nie ma rozrzutu. Przedział o zerowej szerokości
    wyglądałby jak pomiar idealnie precyzyjny."""
    s = statystyki([wiersz(cena_wykonania=D("100.05"))])
    assert s["maker"]["n"] == 1
    assert s["maker"]["odchylenie"] is None
    assert s["maker"]["przedzial"] is None
    assert s["maker"]["werdykt"] == ZA_MALO


def test_srednia_zawsze_przychodzi_z_n_i_z_rozrzutem():
    """Średnia bez liczby obserwacji przy trzech wypełnieniach wygląda dokładnie
    tak samo jak przy trzystu, a prowadzi do innej decyzji."""
    s = statystyki([wiersz(cena_wykonania=D(c)) for c in ("100.04", "100.06",
                                                          "100.05")])
    assert s["maker"]["n"] == 3
    assert s["maker"]["odchylenie"] is not None
    assert s["maker"]["przedzial"] is not None
    linia = [x for x in opis(pomiar([])) if "maker" in x][0]
    assert "n=" in linia


def test_maly_rozjazd_przy_malej_probie_NIE_podwaza_zalozenia():
    """Kanarek na cały sens progu.

    Trzy wypełnienia — 5, 6 i 7 punktów bazowych — dają średnią 6 pb wobec
    założonych 4, czyli półtora raza tyle. Sama ta liczba wyglądałaby jak
    odkrycie i skłoniła do podmiany stałej na podstawie trzech obserwacji.

    Ta próba jest dobrana tak, żeby łapać OBIE części progu naraz. Rozkład
    normalny dałby tu przedział [4,9; 7,1] pb, czyli wykluczyłby założenie
    i ogłosił pomiar. Rozkład t przy dwóch stopniach swobody daje
    [3,5; 8,5] pb — założenie mieści się w środku i nie ma czego odrzucać.
    Trzy obserwacje po prostu nie wystarczają, a różnica między tymi dwiema
    odpowiedziami to różnica między „zmierzone" a „jeszcze nie wiadomo".
    """
    s = statystyki([wiersz(rodzaj_zlecenia="market", cena_wykonania=D(c))
                    for c in ("100.05", "100.06", "100.07")])
    assert s["taker"]["n"] == 3
    assert s["taker"]["srednia"] > POSLIZG_TAKER
    assert s["taker"]["werdykt"] == NIEODROZNIALNE
    lo, hi = s["taker"]["przedzial"]
    assert lo <= POSLIZG_TAKER <= hi


def test_wyrazny_rozjazd_przy_wiekszej_probie_JEST_zglaszany():
    """Dopiero gdy przedział wyklucza stałą, jest o czym mówić — i dopiero wtedy
    jest czym ją zastąpić."""
    ceny = ["100.058", "100.060", "100.062"] * 7
    s = statystyki([wiersz(rodzaj_zlecenia="market", cena_wykonania=D(c))
                    for c in ceny])
    assert s["taker"]["n"] == 21
    assert s["taker"]["werdykt"] == ROZBIEZNE
    lo, hi = s["taker"]["przedzial"]
    assert not (lo <= POSLIZG_TAKER <= hi)


def test_werdykt_jest_czysta_funkcja_progu():
    assert werdykt(0, None, 0.0004) == NIE_ZMIERZONO
    assert werdykt(1, None, 0.0004) == ZA_MALO
    assert werdykt(5, (0.0002, 0.0008), 0.0004) == NIEODROZNIALNE
    assert werdykt(5, (0.0005, 0.0009), 0.0004) == ROZBIEZNE


def test_przedzial_przy_malej_probie_uzywa_t_a_nie_1_96():
    """Przy trzech obserwacjach rozkład normalny daje przedział ponad dwa razy
    za wąski — czyli mówi „zmierzone i różne od założenia" o czymś, czego jeszcze
    nie widać."""
    xs = [0.0, 1.0, 2.0]        # n=3, sd=1, sem=0,5774, df=2 -> t=4,303
    lo, hi = przedzial_sredniej(xs)
    polowa = (hi - lo) / 2
    assert abs(polowa - 4.303 * 0.5773502691896258) < 1e-9
    # Rozklad normalny dalby 1,96 * sem = 1,13 - ponad dwa razy waziej.
    assert polowa > 2 * 1.96 * 0.5773502691896258
    assert przedzial_sredniej([1.0]) is None


# --- ułamek wypełnienia ------------------------------------------------------

def test_ulamek_liczony_TA_SAMA_funkcja_co_w_cyklu():
    """Druga implementacja tej samej średniej rozjechałaby się przy pierwszej
    zmianie, i to niewidocznie — bo obie wyglądałyby poprawnie."""
    z_bazy = ulamki([wiersz(ulamek="1", rodzaj_wypelnienia="pelne"),
                     wiersz(ulamek="0.4", rodzaj_wypelnienia="czesciowe")])
    z_cyklu = rozjazd_z_ulamkow([("pelne", D("1")), ("czesciowe", D("0.4"))])
    assert z_bazy["sredni_ulamek"] == z_cyklu["sredni_ulamek"] == D("0.7")
    assert z_bazy["czesciowych"] == 1


def test_brak_ulamkow_nie_udaje_zera():
    """Zero wyglądałoby jak „nic nigdy nie wchodzi", czyli katastrofalna jakość
    egzekucji zamiast braku pomiaru."""
    u = ulamki([])
    assert u["n"] == 0
    assert u["sredni_ulamek"] is None
    assert u["przedzial"] is None


def test_ulamek_tez_ma_rozrzut_a_nie_sama_srednia():
    u = ulamki([wiersz(ulamek=x, rodzaj_wypelnienia="czesciowe")
                for x in ("0.4", "0.6", "0.5")])
    assert u["n"] == 3
    assert u["odchylenie"] is not None
    assert u["przedzial"] is not None


def test_pomiar_laczy_oba_wyniki():
    p = pomiar([wiersz()])
    assert set(p) >= {"maker", "taker", "ulamek", "pominiete", "odniesienia"}
    assert p["ulamek"]["n"] == 1
