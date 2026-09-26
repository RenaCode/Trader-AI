"""Pasmo ceny i cena wyjścia — czyli co się dzieje, gdy limit rozjeżdża się z rynkiem.

DLACZEGO OSOBNY PLIK. Reguły giełdy z `test_zlecenia.py` sprawdzają KSZTAŁT
zlecenia (krok, minimum) i przechodzą także dla ceny bez związku z rynkiem. Tu
sprawdzamy dokładnie to, czego tamte nie widzą: czy cena, którą naprawdę
wyślemy, ma szansę się WYKONAĆ.

CO ZAWIODŁO WCZEŚNIEJ. Jedyny test przyciągania wyjścia (`test_gielda_exec.py`)
sprawdzał `cena_limitu < 200` przy księdze na 100 — przechodził tak samo dla
110,49 (czyli 10,5% nad rynkiem, zlecenie leżące do wygaśnięcia planu) jak dla
100,01. Asercja „mniej niż cena wyjściowa" nie odróżnia więc wersji użytecznej
od bezużytecznej; asercje tutaj mierzą odległość od RYNKU, nie od liczby, od
której zaczęliśmy.
"""
import math
from decimal import Decimal as D

import pytest

from services.engine.zlecenia import (
    ROZJAZD_GIELD, SUFIT_PASMA, Filtry, Oferty, cena_makera, ocena_pasma,
    prog_pasma, sprawdz_zlecenie,
)

PELNE = Filtry(krok_ilosci=D("0.001"), min_ilosc=D("0.001"),
               min_wartosc=D("5"), krok_ceny=D("0.01"))

# Ksiega docelowa (podmiot unijny) mowi 100. Wszystkie testy ceny wyjscia licza
# odleglosc wzgledem TEJ liczby, bo to ona jest cena, po ktorej ktos handluje.
KSIEGA = D("100")


# ----------------------------------------------------------------- prog_pasma

def test_prog_skaluje_sie_ZMIENNOSCIA_PARY_a_nie_stala_liczba():
    """Stały próg procentowy jest w tym projekcie znanym antywzorcem: 1% to szok
    dla pary stabilnej i szum dla SOL-a, więc jedna liczba jest albo krzykliwa,
    albo bezużyteczna.

    Sprawdzamy pełen wzór, nie samą monotoniczność — bo monotoniczność
    przechodzi także dla progu policzonego z byle czego.
    """
    assert prog_pasma(0.5) < prog_pasma(1.0) < prog_pasma(2.0)
    ruch = D(str(0.01 * math.sqrt(2) * 6))
    assert abs(prog_pasma(1.0) - (ROZJAZD_GIELD + ruch)) < D("0.000001")


def test_rozjazd_ksiag_wchodzi_jako_SKLADNIK_a_nie_podloga():
    """`max(rozjazd, ruch)` znikałby dokładnie tam, gdzie zmienność jest duża —
    a rozjazd ksiąg nie znika, gdy rynek się rusza; wtedy jest największy."""
    ruch = D(str(0.01 * math.sqrt(2) * 6))
    assert prog_pasma(1.0) > ruch                       # podloga dalaby rownosc
    assert abs((prog_pasma(1.0) - ruch) - ROZJAZD_GIELD) < D("0.000001")


def test_dluzsze_zycie_planu_rozszerza_pasmo_PIERWIASTKIEM_a_nie_liniowo():
    """Cena limitu może rozjechać się z rynkiem o tyle, ile rynek przechodzi
    przez czas życia planu — a to skaluje się pierwiastkiem z liczby świec."""
    jedna = prog_pasma(1.0, bary=1.0) - ROZJAZD_GIELD
    cztery = prog_pasma(1.0, bary=4.0) - ROZJAZD_GIELD
    assert abs(cztery - 2 * jedna) < D("0.000001")      # sqrt(4) = 2, nie 4


@pytest.mark.parametrize("atr", [None, 0.0, -1.0, float("nan")])
def test_nieznana_zmiennosc_daje_pasmo_AWARYJNE_a_nie_brak_pasma(atr):
    """„Nie wiem, jak bardzo ta para się rusza" nie jest powodem, żeby
    przepuścić cenę dziesięciokrotnie oderwaną od rynku. Wartość zepsuta (NaN)
    ani ujemna nie może przy tym wejść do wzoru jako liczba."""
    assert prog_pasma(atr) == SUFIT_PASMA
    assert ocena_pasma(D("120"), KSIEGA, atr).ze_zmiennosci is False


def test_ogromny_ATR_nie_rozsuwa_pasma_bez_konca():
    """Powyżej ćwierci ceny pasmo przestaje cokolwiek wykluczać, a sam ATR
    przestaje być wiarygodny: skoro wyszedł tak duży, to prawdopodobnie
    policzono go z tych samych zepsutych świec, przed którymi pasmo broni."""
    assert prog_pasma(50.0) == SUFIT_PASMA
    assert prog_pasma(1.0, bary=0) == SUFIT_PASMA       # czas bez sensu = sufit


# ---------------------------------------------------------------- ocena_pasma

def test_brak_odniesienia_to_LUKA_a_nie_zgoda():
    """Wywołujący, który nie poda ceny z księgi docelowej, zostaje
    z zabezpieczeniem WYŁĄCZONYM. Zwrócenie „w pasmie" byłoby powiedzeniem
    nieprawdy o czymś, czego nie sprawdziliśmy."""
    assert ocena_pasma(D("100"), None, 1.0) is None
    assert ocena_pasma(D("100"), D("0"), 1.0) is None
    assert ocena_pasma(D("100"), D("-5"), 1.0) is None


def test_odchylenie_liczone_wzgledem_KSIEGI_DOCELOWEJ_w_obie_strony():
    """Odchylenie jest względne i symetryczne — inaczej ta sama rozbieżność
    byłaby raz alarmem, a raz normą, zależnie od kierunku."""
    assert ocena_pasma(D("110"), KSIEGA, 1.0).odchylenie == D("0.1")
    assert ocena_pasma(D("90"), KSIEGA, 1.0).odchylenie == D("0.1")
    p = ocena_pasma(D("110"), KSIEGA, 1.0)
    assert p.dolna < KSIEGA < p.gorna


def test_poza_pasmem_zaczyna_sie_DOPIERO_za_progiem():
    """Próg ma odróżniać cenę od AWARII DANYCH, więc równo na progu jeszcze nie
    alarmujemy. Gdyby granica była ostra w drugą stronę, alarm dzwoniłby przy
    rozjeździe, który sami zmierzyliśmy jako normalny."""
    prog = prog_pasma(1.0)
    assert ocena_pasma(KSIEGA * (1 + prog), KSIEGA, 1.0).poza is False
    assert ocena_pasma(KSIEGA * (1 + prog * 2), KSIEGA, 1.0).poza is True


def test_opis_mowi_SKAD_wzial_sie_prog():
    """Komunikat trafia do `exec.order.reject_reason` i to on jest jedynym
    śladem, po którym da się odróżnić „rynek uciekł" od „nie znamy ATR-u"."""
    assert "6×ATR" in ocena_pasma(D("200"), KSIEGA, 1.0).opis()
    assert "awaryjne" in ocena_pasma(D("200"), KSIEGA, None).opis()


# ---------------------------------------------------------------- cena_makera

def test_cena_makera_NIE_PRZECINA_SPREADU_po_zadnej_ze_stron():
    """Odniesienie to ostatnia transakcja, więc leży między najlepszym kupnem
    a najlepszą sprzedażą. Cena postawiona dokładnie na nim może już przecinać
    spread — a `PostOnly` takie zlecenie ODRZUCA, więc wyjścia nie ma."""
    assert cena_makera(D("100"), D("0.01"), kupno=False) > D("100")
    assert cena_makera(D("100"), D("0.01"), kupno=True) < D("100")
    # Dokladnie jeden krok, bo kazdy kolejny oddala zlecenie od rynku.
    assert cena_makera(D("100"), D("0.01"), kupno=False) == D("100.01")
    assert cena_makera(D("100"), D("0.01"), kupno=True) == D("99.99")


def test_cena_makera_z_odniesienia_POZA_SIATKA_zostaje_przy_rynku():
    """Cena spoza siatki jest już po naszej stronie po samym zaokrągleniu —
    dokładanie do tego pełnego kroku byłoby oddaleniem się od rynku za darmo."""
    assert cena_makera(D("100.005"), D("0.01"), kupno=False) == D("100.01")
    assert cena_makera(D("100.005"), D("0.01"), kupno=True) == D("100")


def test_cena_makera_nie_wnosi_skali_kolumny_z_bazy():
    """`tick_size` wraca z NUMERIC jako Decimal("0.010000000000"); giełda
    odrzuca zlecenie z taką liczbą miejsc mimo poprawnej wartości."""
    assert str(cena_makera(D("100"), D("0.010000000000"), kupno=False)) == "100.01"
    assert str(cena_makera(D("100"), D("0.010000000000"), kupno=True)) == "99.99"


# ------------------------------------------------- cena wyjscia w sprawdz_zlecenie

def test_WYJSCIE_dostaje_cene_z_ksiegi_a_NIE_krawedz_pasma():
    """SEDNO. Półszerokość pasma to 10,5% przy ATR 1%, więc sprzedaż wyjścia
    przyciągnięta do górnej krawędzi stoi 10,5% NAD rynkiem. Jako `PostOnly`
    jest przyjmowana (nie przecina spreadu) i po prostu LEŻY: po dwóch świecach
    plan wygasa, zlecenie idzie do anulowania, a pozycja zostaje otwarta —
    i następna świeca powtarza to samo. W spadku wyjście goni cenę, tracąc po
    cztery godziny za każdym razem, a w dzienniku wygląda to na porządek.

    Dlatego asercja mierzy odległość od RYNKU, a nie od ceny planu.
    """
    _, c, powod = sprawdz_zlecenie(D("1"), D("200"), PELNE, kupno=False,
                                   wyjscie=True, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0)
    assert powod is None                        # wyjscia sie nie odmawia
    assert c == D("100.01")
    assert abs(c - KSIEGA) / KSIEGA < D("0.001")            # przy rynku
    assert c > KSIEGA                                       # po stronie makera


def test_WYJSCIE_PONIZEJ_rynku_tez_jest_poprawiane():
    """Druga strona tego samego błędu: cena pod rynkiem przecina spread, więc
    `PostOnly` ODRZUCA złożenie. Przyciągnięcie do dolnej krawędzi pasma nic tu
    nie zmieniało, bo krawędź leży jeszcze niżej niż rynek."""
    _, c, powod = sprawdz_zlecenie(D("1"), D("50"), PELNE, kupno=False,
                                   wyjscie=True, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0)
    assert powod is None
    assert c == D("100.01")
    assert c > KSIEGA


def test_WYJSCIE_z_pozycji_krotkiej_stoi_po_DRUGIEJ_stronie_rynku():
    """Wyjściem z pozycji krótkiej jest KUPNO, więc cena wykonalna leży poniżej
    rynku. Wyprowadzenie strony z roli („wyjście to sprzedaż") postawiłoby
    zlecenie po złej stronie księgi i `PostOnly` odrzuciłby je co do jednego."""
    _, c, powod = sprawdz_zlecenie(D("1"), D("200"), PELNE, kupno=True,
                                   wyjscie=True, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0)
    assert powod is None
    assert c == D("99.99")
    assert c < KSIEGA


def test_WYJSCIE_W_PASMIE_tez_dostaje_cene_rynkowa():
    """Pasmo mierzy, czy cena jest SENSOWNA, a nie czy jest WYKONALNA. Wyjście
    8% nad rynkiem mieści się w paśmie przy ATR 1% i leży dokładnie tak samo
    bezczynnie jak to 10,5% nad rynkiem — interwencja tylko poza pasmem
    zostawiałaby więc otwartą pozycję w najczęstszym przypadku."""
    pasmo = ocena_pasma(D("108"), KSIEGA, 1.0)
    assert pasmo.poza is False                  # dla porzadku: to jest W pasmie
    _, c, powod = sprawdz_zlecenie(D("1"), D("108"), PELNE, kupno=False,
                                   wyjscie=True, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0)
    assert powod is None
    assert c == D("100.01")


def test_WYJSCIE_bez_ceny_z_ksiegi_zachowuje_cene_planu():
    """Brak odczytu księgi nie zatrzymuje handlu, ale też nie wymyśla ceny:
    bez odniesienia nie ma czym zastąpić planowej. Tak działa `czy_zamykalna`,
    które pyta o KSZTAŁT zlecenia, nie o dzisiejszy rynek."""
    _, c, powod = sprawdz_zlecenie(D("1"), D("200"), PELNE, kupno=False,
                                   wyjscie=True, cena_odniesienia=None,
                                   atr_pct=1.0)
    assert powod is None
    assert c == D("200")


def test_minimum_wartosci_liczone_z_ceny_KTORA_WYSYLAMY():
    """Zlecenie za 6 USDC po cenie planu jest warte 3 USDC po cenie rynkowej,
    czyli poniżej minimum giełdy. Sprawdzenie minimum przed podmianą ceny
    przepuściłoby je, a giełda odbiłaby je błędem parametru — który w logu
    wygląda jak brak sygnału."""
    _, c, powod = sprawdz_zlecenie(D("0.03"), D("200"), PELNE, kupno=False,
                                   wyjscie=True, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0)
    assert c == D("100.01")
    assert powod is not None and "poniżej minimum" in powod


# ----------------------------------------------------- wejscie zostaje przy odmowie

def test_WEJSCIE_poza_pasmem_nadal_ODMAWIA_zamiast_dostac_cene_rynkowa():
    """Wejściu wolno nie dojść do skutku: kosztuje okazję, czyli nic, czego
    byśmy już nie stracili, gdyby ceny nie było w ogóle. Podstawienie mu ceny
    rynkowej zamieniałoby awarię danych w otwartą pozycję — czyli robiło z
    zepsutego ticka decyzję inwestycyjną."""
    _, c, powod = sprawdz_zlecenie(D("1"), D("200"), PELNE, kupno=False,
                                   wyjscie=False, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0)
    assert powod is not None and "odbiega od księgi docelowej" in powod
    assert "wejście" in powod
    assert c == D("200")                        # ceny wejscia nikt nie ruszal


def test_WEJSCIE_W_PASMIE_TEZ_dostaje_cene_z_chwili_wysylki():
    """ODWRÓCENIE WCZEŚNIEJSZEGO ZAŁOŻENIA — i powód, dla którego to nie jest
    rozluźnienie reguły.

    Do 2026-09-26 stało tu „cena wejścia niesie decyzję modelu (kupuj, ale nie
    drożej niż), więc podmiana jej na rynkową kupowałaby po każdej cenie, jaką
    akurat pokazuje księga". Zdanie jest słuszne, a wniosek był odwrotny do
    skutku: cena z planu to zamknięcie świecy sprzed 71–82 minut, więc
    „nie drożej niż" odnosiło się do rynku, którego już nie było. Wszystkie
    trzy odrzucenia PostOnly w historii produkcji to wejścia z tą ceną (kupno
    SOLUSDC nad askiem), a oba wypełnienia miały limit po stronie pasywnej.
    Cena z planu nie chroniła więc przed złym wykonaniem — ona je uniemożliwiała.

    „NIE DROŻEJ NIŻ" ZOSTAJE, tylko nie w tej funkcji: pilnuje tego
    `ocena_zwloki` w `gielda_exec.zloz_plan`, z progiem 1,5×ATR pary. Tutaj
    sprawdzamy sam fakt podmiany — i to, że wychodzi ona PO STRONIE PASYWNEJ
    (kupno poniżej odniesienia), bo to jest warunek przyjęcia przez PostOnly.
    """
    _, c, powod = sprawdz_zlecenie(D("1"), D("103"), PELNE, kupno=True,
                                   wyjscie=False, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0)
    assert powod is None
    assert c == D("99.99")
    assert c < KSIEGA


def test_WEJSCIE_bierze_BID_z_arkusza_gdy_arkusz_jest():
    """ARKUSZ WYGRYWA Z OSTATNIĄ CENĄ, bo odpowiada na zadane pytanie.

    `lastPrice` leży *gdzieś* między bidem a askiem — ale nie musi: zmierzone
    na zleceniu 54 (SOLUSDC) było JEDENAŚCIE tików NAD askiem. Kupno liczone
    od niego stoi wtedy powyżej najlepszej sprzedaży, czyli przecina spread,
    czyli wraca odrzucone. Bid i ask są w arkuszu i tylko tam.
    """
    o = Oferty(bid=D("99.50"), ask=D("99.52"))
    _, c, powod = sprawdz_zlecenie(D("1"), D("103"), PELNE, kupno=True,
                                   wyjscie=False, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0, najlepsze=o)
    assert powod is None
    assert c == D("99.49")                  # bid - krok, nie 99,99 z lastPrice
    assert c < o.ask, "cena przecinająca spread wróciłaby jako odrzucona"


def test_SPRZEDAZ_bierze_ASK_a_nie_bid():
    """Strona ma znaczenie i pomyłka jest cicha: sprzedaż postawiona od bidu
    leży PONIŻEJ najlepszego kupna, czyli przecina spread i wraca odrzucona.
    Ten sam błąd w drugą stronę co przy kupnie od asku."""
    o = Oferty(bid=D("99.50"), ask=D("99.52"))
    _, c, powod = sprawdz_zlecenie(D("1"), D("97"), PELNE, kupno=False,
                                   wyjscie=False, cena_odniesienia=KSIEGA,
                                   atr_pct=1.0, najlepsze=o)
    assert powod is None
    assert c == D("99.53")
    assert c > o.bid


def test_ARKUSZ_ODWROCONY_nie_jest_arkuszem_ciasnym():
    """Ask poniżej bidu to odczyt ZEPSUTY, nie księga bez spreadu. Podstawienie
    go dawałoby cenę po złej stronie rynku przy zachowaniu wszystkich
    pozostałych reguł — czyli błąd wyglądający na poprawne zlecenie. Wracamy
    wtedy do ostatniej ceny, która jest odczytem innym i niezależnym."""
    o = Oferty(bid=D("99.52"), ask=D("99.50"))
    assert o.kompletne is False
    _, c, _ = sprawdz_zlecenie(D("1"), D("103"), PELNE, kupno=True,
                               wyjscie=False, cena_odniesienia=KSIEGA,
                               atr_pct=1.0, najlepsze=o)
    assert c == D("99.99")                  # z lastPrice, nie z zepsutego arkusza
