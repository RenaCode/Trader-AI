"""Symulacja PORTFELA - jedna kasa, wspolne limity, ten sam risk kernel.

Poprzednia symulacja liczyla kazdy instrument osobno i sumowala transakcje na
koncu. Bylo to wygodne i falszywe z trzech powodow:

  1. Limit lacznej ekspozycji i limit czestotliwosci zlecen sa wlasnosciami
     KONTA, nie instrumentu. Dwanascie niezaleznych symulacji nigdy ich nie
     naruszy, bo kazda widzi tylko siebie.
  2. Kill-switch zatrzymuje CALY silnik. W symulacji per instrument nie ma
     czego zatrzymac - strata na SOL nie wstrzymywala handlu na BTC, choc
     w produkcji wstrzymalaby.
  3. Obsuniecie kapitalu liczone per instrument jest mniejsze niz obsuniecie
     portfela, bo instrumenty spadaja jednoczesnie (korelacja krypto jest
     wysoka). Pierwsza iteracja raportowala -37% wlasnie dlatego, ze nikt tego
     obsuniecia nie zatrzymywal.

Rezultat tamtego podejscia: backtest pokazywal obsuniecie 37% przy
skonfigurowanym kill-switchu na 10%. Opisywal wiec strategie, ktorej ten system
nie wykonalby - i to jest gorsze niz brak backtestu, bo wyglada na wynik.

Reguly limitow sa wspolne z produkcja (services.engine.risk_rules). Ten modul
dostarcza im tylko stan trzymany w pamieci zamiast w bazie.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal

from services.engine.horizons import Horizon
from services.engine.koszty import (
    GODZINY_FINANSOWANIA, POSLIZG_MAKER, POSLIZG_TAKER, koszty,
    stawka_finansowania_na_ulamek, zwroty_po_doplacie,
)
from services.engine.risk_rules import (
    START_EQUITY, wielkosc_pozycji,
    DEFAULT_LIMITS, RiskState, evaluate_account, evaluate_limits, position_budget,
)
from services.engine.bramki import Bramki, bramki_z_pary
from services.engine.signals import classify_regime, combine, precompute

# Parametry egzekucji. Prowizja i poslizg pochodza z services/engine/koszty.py,
# bo zaleza od RODZAJU instrumentu i typu zlecenia - wczesniej byla tu jedna
# stala 0,055%, czyli stawka taker kontraktow wieczystych, stosowana takze do
# instrumentow spotowych, gdzie Bybit liczy 0,1%.
# START_EQUITY, MIN_CONFIDENCE i udzial_budzetu przychodza z risk_rules -
# patrz komentarz przy nich. Tu byly zdefiniowane po raz drugi.
# Bazowa wielkosc zlecenia wyprowadzana z limitu ekspozycji i liczby
# instrumentow - patrz risk_rules.position_budget().

MIN_SD = 1e-4


@dataclass
class Position:
    side: int                 # +1 long, -1 short
    qty: float
    entry: float
    conf: float
    opened_at: datetime
    # Finansowanie zaplacone (dodatnie) albo zainkasowane (ujemne) przez TE
    # pozycje, w walucie. Trzymane per pozycja, a nie tylko w sumie globalnej,
    # bo musi wejsc do `ret` TEJ transakcji - Sharpe liczy sie ze zwrotow, wiec
    # koszt widoczny wylacznie w krzywej kapitalu zostawia werdykt okna liczony
    # przed kosztem. Prowizja przerabiala dokladnie te droge.
    finansowanie: float = 0.0
    # POKRYCIE WAG W CHWILI WEJSCIA - mianownik, przez ktory `signals.combine`
    # podzielilo wynik. 1,0 znaczy "odezwaly sie wszystkie sygnaly, ktore maja
    # wage"; 0,05 znaczy "odezwal sie jeden o wadze 0,05, a jego glos zostal
    # wzmocniony dwudziestokrotnie". Trzymane per pozycja, bo pytanie brzmi:
    # czy transakcje z niskiego pokrycia zarabiaja tyle samo, co te z pelnego.
    # Na produkcji rezimu i pokrycia rozplesc sie NIE DA (cale niskie pokrycie
    # jest w `range`), wiec odpowiedzieć moze tylko symulacja.
    pokrycie: float = 1.0
    # INDEKS SWIECY DECYZJI WEJSCIA w panelu TEGO instrumentu - do liczby swiec
    # trzymania (`swiec` przy transakcji, H-7). Indeks, nie czas: na gieldzie
    # akcji noc i weekend to kilkanascie godzin bez swiec, a etykieta IC
    # (`train.ic_weights`) liczy horyzont w swiecach panelu.
    idx_wejscia: int = -1

    @property
    def finansowanie_w_zwrocie(self) -> float:
        """Finansowanie przeliczone na UŁAMEK notionalu wejscia.

        ZEROWY NOTIONAL JEST MOZLIWY I NIE JEST BLEDEM: przy progu wejscia 0
        sygnal o sile 0 daje `udzial_budzetu(0) == 0`, wiec pozycja powstaje
        pusta (tak robi `test_zwrot_z_transakcji_jest_po_prowizji_a_nie_przed`).
        Finansowanie od zerowej pozycji tez wynosi zero - ale samo dzielenie
        wywracaloby caly przebieg.
        """
        baza = self.qty * self.entry
        return self.finansowanie / baza if baza else 0.0


@dataclass
class Egzekucja:
    """Jak skladamy zlecenia - i co za to placimy.

    RYNKOWA: przechodzimy przez spread, placimy prowizje taker i poslizg,
    ale wypelnienie jest pewne.

    Z LIMITEM: skladamy zlecenie po cenie zamkniecia swiecy decyzyjnej. Nie ma
    poslizgu i placimy tansza prowizje maker, ale wypelnienie NIE JEST PEWNE -
    zachodzi tylko wtedy, gdy nastepna swieca dotknie naszej ceny.

    To rozroznienie decyduje o wszystkim. Na kontrakcie wieczystym koszt rundy
    wynosi 0,190% przy zleceniu rynkowym i 0,040% przy limicie - siedmiokrotna
    roznica przy przewadze rzedu 0,12% na transakcje.

    Ale model wypelnien musi byc UCZCIWY, inaczej backtest zaklamie w druga
    strone niz dotad. Zlecenie z limitem wypelnia sie wtedy, gdy cena przyszla
    do nas - czyli czesto wtedy, gdy rynek idzie PRZECIWKO nam. Ruchy, ktore
    uciekaja natychmiast, sa dla nas stracone, a to wlasnie one bywaja
    najbardziej zyskowne. Ta selekcja jest w modelu zawarta z definicji: nie
    zgadujemy prawdopodobienstwa wypelnienia, tylko sprawdzamy, czy nastepna
    swieca faktycznie dotknela naszej ceny.

    MODEL PESYMISTYCZNY (2026-10-03, audyt Fable F-3.2 / R1). Model wyzej mial
    w sobie TRZY zalozenia, ktore produkcja obalila: limit po `close` swiecy
    decyzyjnej (produkcja: bid - 1 tick w chwili WYSYLKI, 5-88 min pozniej),
    okno wypelnienia cale 4 h nastepnej swiecy (dla kupna pod rynkiem
    spelnione prawie zawsze - 0 niewypelnionych na 1 561) i brak spreadu
    instrumentu przy cenie. Trzy pola nizej zdejmuja je osobno:

      * `opoznienie_min` - limit liczony z ceny W CHWILI WYSYLKI
        (`koniec swiecy decyzyjnej + opoznienie`), odczytanej ze swiec 1h:
        interpolacja liniowa miedzy zamknieciem poprzedniej godziny a
        zamknieciem godziny, w ktorej wysylka wypada. Przy danych godzinowych
        to jest wartosc oczekiwana ceny w tej chwili (most Browna), a nie
        zgadywanie kierunku. 0 = cena zamkniecia swiecy decyzyjnej, czyli
        dzisiejszy model co do bitu.
      * `spread_per_instrument` - PELNY spread w pb per symbol (to, co oddaje
        `spread.Pomiar.per_symbol`). Limit kupna = cena - pol spreadu, limit
        sprzedazy = cena + pol spreadu: tyle, ile daje PostOnly na bid/ask.
        Brak symbolu w slowniku = spread 0 dla niego, wypisany przez
        wolajacego.
      * `pesymistyczna` - okno wypelnienia to JEDNA godzina po tej, w ktorej
        zlecenie wyslano (`low` kolejnej 1h <= limit), a nie cala nastepna
        swieca 4h. Zlecenie, ktore nie weszlo w godzine, przepada: wejscie nie
        powstaje, wyjscie czeka nastepnej decyzji (jak dotad).

    Wszystkie trzy wymagaja zlecen z limitem - zlecenie rynkowe nie czeka na
    wypelnienie - oraz swiec 1h (`simulate_portfolio(..., swiece_wykonania=)`).
    Przy wartosciach domyslnych symulacja idzie DOKLADNIE ta sama sciezka co
    przed zmiana (test regresji w `tests/test_wypelnienia.py`).

    SPREAD W KARCIE ZOSTAJE. Wejscie po `cena - pol spreadu` plus doplata
    karty (`pol spreadu` na noge) daje wejscie po cenie odniesienia - czyli
    spread nie jest ani zarabiany, ani placony dwa razy, a jedynym skutkiem
    spreadu w modelu jest TRUDNIEJSZE wypelnienie. To swiadomie ostrozne:
    maker na bid formalnie zarabia pol spreadu, ale tylko na zleceniach, ktore
    weszly - a to te, przy ktorych rynek poszedl przeciw nam.

    PRZEBICIE (2026-10-11, H-10). Dotkniecie ceny nie jest wypelnieniem.
    PostOnly na bid staje NA KONCU kolejki po tej cenie, a swieca 1h pochodzi
    z ksiegi globalnej (~1000x wolumen unijnej) - `low` rowny naszemu limitowi
    mowi, ze ktos gdzies handlowal po tej cenie, nie ze nasza kolejka doszla do
    nas. Model z samym dotknieciem dal 76-79% wypelnien wobec 65,6% na demo
    (42/64, E-1), czyli wiecej, niz H-5 dopuszcza (10 pp).

      * `przebicie_bps` - o ile `low` (kupno) / `high` (sprzedaz) musi PRZEJSC
        za limit, zeby WEJSCIE sie wypelnilo. 0 = samo dotkniecie, czyli model
        sprzed zmiany co do bitu. Liczba nie jest wybierana recznie: kalibruje
        ja `kalibruj_przebicie` tak, zeby wypelnialnosc WEJSC w symulacji
        odtwarzala wypelnialnosc zmierzona na demo. Kalibracja patrzy wylacznie
        na to, CZY zlecenie weszlo, nigdy na zwrot transakcji.
      * DOTYCZY TYLKO WEJSC. Kryterium H-5 i pomiar demo dotycza wejsc;
        wyjscie na produkcji po dwoch odrzuceniach PostOnly idzie takerem
        (`gielda_exec.PROBY_POSTONLY_WYJSCIA`), wiec jego wypelnialnosc jest
        inna wielkoscia. Zaostrzenie wyjsc bez pomiaru byloby zgadywaniem.
    """
    limit: bool = False
    opoznienie_min: int = 0
    spread_per_instrument: dict[str, float] | None = None
    pesymistyczna: bool = False
    przebicie_bps: float = 0.0
    # Skad wzielo sie `przebicie_bps`: wypelnialnosc wejsc demo, do ktorej je
    # skalibrowano (`train.kalibracja_wypelnien`). None = bez kalibracji.
    # Symulacja tego pola nie czyta - to opis miary dla `params` i dozoru.
    wypelnialnosc_docelowa: float | None = None

    @property
    def zmienia_cene_lub_okno(self) -> bool:
        """Czy ten wariant odchodzi od modelu sprzed 2026-10-03."""
        return bool(self.opoznienie_min or self.pesymistyczna or self.przebicie_bps
                    or any((self.spread_per_instrument or {}).values()))


@dataclass
class PortfolioResult:
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    killswitch_at: datetime | None = None
    killswitch_reason: str | None = None
    blocked: int = 0          # ile wejsc odrzucil risk kernel
    downsized: int = 0        # ile obcial
    max_exposure: float = 0.0  # najwieksza jednoczesna ekspozycja
    # SREDNIA ekspozycja po wszystkich swiecach okna - nie szczyt.
    #
    # Szczyt mowi, jak zle moglo byc w najgorszym momencie. O tym, ile
    # kapitalu faktycznie pracowalo, nie mowi nic - a bez tego zwrotu
    # z okna nie da sie porownac z niczym, co angazuje inna czesc konta.
    avg_exposure: float = 0.0
    # EKSPOZYCJA PRZENOSZONA NA NASTEPNY PRZEDZIAL - liczona PO wejsciach
    # swiecy, nie przed nimi (2026-10-03, audyt Fable F-3.5 / audyt 2 R-d).
    #
    # `avg_exposure` mierzy portfel po wyjsciach i PRZED wejsciami tej samej
    # swiecy. Pozycja otwarta w swiecy `a` i zamknieta decyzja w `b` niesie
    # ruch ceny przez `b - a` przedzialow, a do sredniej wchodzi `b - a - 1`
    # razy: JEDNA SWIECA MNIEJ na kazda transakcje, takze na domknieciu
    # wymuszonym koncem okna. Przy medianie trzymania 4 swiece to ok. 25%
    # zanizenia - a porownanie „przy tej samej ekspozycji" mnozy koszyk przez
    # te liczbe, wiec zanizona ekspozycja to slabsze odniesienie, czyli
    # obciazenie NA KORZYSC strategii. Ta miara liczy kazdy przedzial, przez
    # ktory pozycja niesie cene, dokladnie raz - tak jak kup-i-trzymaj liczy
    # caly swoj przedzial.
    #
    # `avg_exposure` zostaje bez zmian: na nim stoi tozsamosc rozkladu
    # (`srednio_pozycji x sredni_notional`) i dotychczasowe wydruki.
    avg_exposure_przenoszona: float = 0.0
    # ROZKLAD SREDNIEJ EKSPOZYCJI NA CZYNNIKI.
    #
    # Sama srednia mowi, ze kapital nie pracuje - nie mowi DLACZEGO. A odpowiedzi
    # sa trzy, prowadza do trzech roznych zmian i nie da sie ich rozroznic bez
    # rozbicia liczby:
    #
    #     ekspozycja = srednio_pozycji x sredni_notional / kapital
    #     srednio_pozycji = udzial_swiec_z_pozycja x pozycji_gdy_w_rynku
    #
    # Portfel poza rynkiem przez 90% czasu i portfel trzymajacy jedna male
    # pozycje bez przerwy daja te sama srednia i wymagaja czegos zupelnie innego.
    udzial_swiec_z_pozycja: float = 0.0   # ile swiec ma choc jedna pozycje
    srednio_pozycji: float = 0.0          # srednia po WSZYSTKICH swiecach
    pozycji_gdy_w_rynku: float = 0.0      # srednia po swiecach Z pozycja
    sredni_notional: float = 0.0          # sredni rozmiar jednej pozycji
    base_notional: float = 0.0  # budzet na pozycje wyliczony z limitu
    niewypelnione_wejscia: int = 0
    niewypelnione_wyjscia: int = 0
    # MIANOWNIK WYPELNIALNOSCI. Same niewypelnione nie mowia nic bez liczby
    # zlecen, ktore weszly - a „0 niewypelnionych" przy zleceniach z limitem
    # bylo dokladnie tym zdaniem, ktore produkcja (65%) obalila.
    wypelnione_wejscia: int = 0
    wypelnione_wyjscia: int = 0
    # GLEBOKOSC KAZDEGO WEJSCIA Z LIMITEM w modelu z godzin (`glebokosc_bps`),
    # takze tych, ktore nie weszly - material na `kalibruj_przebicie`. Bez
    # wejsc, ktorym zabraklo swiecy 1h (te sa w `brak_swiec_wykonania`).
    glebokosci_wejsc: list[float] = field(default_factory=list)
    # Ruch, ktory przepadl przez niewypelnione WEJSCIE: zwrot ze strony
    # pozycji od ceny limitu do ceny `UTRACONY_RUCH_SWIEC` swiec pozniej.
    # Material na skladnik `niewypelnienie` karty (`koszty.koszt_niewypelnienia`).
    utracone_ruchy: list[float] = field(default_factory=list)
    # Zlecenia, dla ktorych brakowalo swiecy 1h potrzebnej do wyceny albo do
    # sprawdzenia wypelnienia. Licza sie jako NIEWYPELNIONE (ostroznie) i sa
    # wypisane osobno, zeby dziura w danych nie udawala rynku.
    brak_swiec_wykonania: int = 0
    pominiete_shorty: int = 0
    # Ile razy sygnal ponad progiem NIE MIAL PROGU - czyli wypadl w rezimie
    # bez kalibracji. Kanarek na ciche zniknięcie rezimu z handlu.
    pominiete_bez_progu: int = 0
    # Stawki faktycznie zastosowane - do sprawdzenia z zewnatrz, ze symulacja
    # policzyla te koszty, ktore deklaruje.
    prowizja: float = 0.0
    poslizg: float = 0.0
    # FINANSOWANIE, W WALUCIE, ZE ZNAKIEM. Dodatnie = zaplacone, ujemne =
    # zainkasowane (krotka noga przy dodatniej stawce). Osobno od `fees_paid`,
    # choc wchodzi do tej samej krzywej: prowizja zalezy od liczby zlecen,
    # finansowanie od czasu trzymania. Zsumowane w jednej liczbie przestaloby
    # sie dac rozdzielic, a to wlasnie ten rozdzial rozstrzyga, czy strona
    # krotka na perpetualu ma sens.
    finansowanie_zaplacone: float = 0.0
    # Ile wyplat finansowania przeszlo przez otwarte pozycje w calym oknie.
    # Kanarek na ciche zero: koszt 0,00 przy 0 wyplatach znaczy "nie bylo za
    # co placic", a przy 240 wyplatach - "stawka byla zerowa". To sa dwie
    # rozne rzeczy i jedna liczba ich nie rozroznia.
    wyplat_finansowania: int = 0
    # Wyplaty, ktore przeszly przez otwarta pozycje, a stawki dla nich NIE
    # ZNAMY - dziura w `market.funding_rate` w srodku okna. Nie doliczamy ich
    # (nie ma czego), ale nie wolno ich tez zgubic: koszt policzony na oknie
    # z dziura jest ZANIZONY o nieznana wielkosc, a bez tego licznika wyglada
    # tak samo jak koszt policzony na komplecie danych.
    wyplat_bez_stawki: int = 0


def precompute_panels(panels: dict[str, list[dict]],
                      atr_thresholds: dict[str, float | None],
                      h: Horizon) -> dict[str, list[tuple]]:
    """Rezim i komplet sygnalow dla kazdej swiecy - policzone RAZ.

    Ani rezim, ani pojedyncze sygnaly nie zaleza od wag; zalezy od nich dopiero
    ich zsumowanie. Trener sprawdza kilkadziesiat ukladow wag na tym samym oknie,
    wiec bez tej pamieci podrecznej liczyl komplet sygnalow od nowa dla kazdej
    proby - przy 24 instrumentach, 40 oknach i 60 probach to kilkaset milionow
    zbednych wywolan.

    Zwraca {symbol: [(rezim, sygnaly) na swiece]} w kolejnosci wierszy panelu.
    """
    cache: dict[str, list[tuple]] = {}
    for sym, rows in panels.items():
        atr_p80 = atr_thresholds.get(sym)
        cache[sym] = [
            (classify_regime(r["f"], atr_p80)[0], precompute(r["f"], h))
            for r in rows
        ]
    return cache


def wyplaty_finansowania(od: datetime, do: datetime) -> int:
    """Ile razy gielda nalicza finansowanie w przedziale (od, do].

    PRZEDZIAL OTWARTY Z LEWEJ. Wyplata dokladnie w chwili wejscia w pozycje
    nas nie dotyczy - placa ja ci, ktorzy pozycje juz mieli. Gdyby przedzial
    byl domkniety, kazda pozycja otwarta o pelnej godzinie finansowania placila
    by raz wiecej, niz zaplaci na gieldzie.

    Liczymy GODZINY ZEGAROWE UTC z `GODZINY_FINANSOWANIA`, a nie krok stalej
    dlugosci od `od`. Chwile wyplaty sa wlasnoscia gieldy i nie przesuwaja sie
    razem z tym, kiedy my weszlismy.
    """
    if do <= od:
        return 0
    # Ruszamy od poczatku doby wejscia i idziemy po kolejnych chwilach wyplaty.
    # Doba to trzy punkty, wiec nawet okno wieloletnie to pojedyncze tysiace
    # krokow na pozycje - a pozycje w tej symulacji zyja godzinami.
    dzien = od.replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    while dzien <= do:
        for g in GODZINY_FINANSOWANIA:
            chwila = dzien.replace(hour=g)
            if od < chwila <= do:
                n += 1
        dzien += timedelta(days=1)
    return n


def _wypelnione(limit: float, swieca: dict, kupno: bool) -> bool:
    """Czy zlecenie z limitem doczekalo sie wypelnienia w tej swiecy.

    Kupno po cenie L wypelnia sie, gdy rynek zszedl do L (low <= L).
    Sprzedaz po L - gdy rynek doszedl do L (high >= L).

    To jest caly model selekcji negatywnej i jest w nim zawarty z definicji:
    wypelniamy sie wtedy, gdy cena PRZYSZLA DO NAS, czyli czesto wtedy, gdy
    rynek idzie przeciwko nam. Ruchy, ktore uciekaja natychmiast, sa dla nas
    stracone - a to wlasnie one bywaja najbardziej zyskowne. Nie zgadujemy tu
    zadnego prawdopodobienstwa; sprawdzamy fakt z danych swiecy.
    """
    return swieca["low"] <= limit if kupno else swieca["high"] >= limit


def glebokosc_bps(limit: float, swieca: dict, kupno: bool) -> float:
    """O ile swieca PRZESZLA za limit, w pb ceny limitu. Ujemna = nie dotknela.

    Kupno: (limit - low) / limit. Sprzedaz: (high - limit) / limit. Ta sama
    relacja co `_wypelnione`: `glebokosc >= 0` wtedy i tylko wtedy, gdy swieca
    dotknela limitu (z dokladnoscia do zaokraglenia przy rownosci).
    """
    if kupno:
        return (limit - swieca["low"]) / limit * 1e4
    return (swieca["high"] - limit) / limit * 1e4


def kalibruj_przebicie(glebokosci: list[float], prob: int,
                       wypelnialnosc_docelowa: float) -> float:
    """Najmniejsze przebicie (pb) >= 0, przy ktorym wypelnialnosc wejsc
    `#(glebokosc >= przebicie) / prob` nie przekracza docelowej.

    `glebokosci` - po jednej na wejscie, dla ktorego BYLY swiece do sprawdzenia
    (`PortfolioResult.glebokosci_wejsc`); `prob` - wszystkie rozstrzygniete
    wejscia, razem z tymi bez swiecy 1h (te sa niewypelnione przy kazdym
    przebiciu, wiec wchodza do mianownika, a nie do rozkladu).

    TYLKO ZAOSTRZA. Gdy symulacja juz wypelnia nie wiecej niz demo, wynik to 0:
    model nie moze wypelniac latwiej niz „swieca dotknela limitu", bo to
    jest warunek konieczny wypelnienia na kazdej gieldzie. Luzowanie modelu,
    zeby dogonic wyzsza wypelnialnosc, byloby zgadywaniem kolejki w druga strone.

    KALIBRACJA DOTYCZY WYPELNIALNOSCI, NIE WYNIKU. Funkcja nie widzi zwrotow
    transakcji. Gdyby widziala, „przebicie dobrane tak, zeby demo sie zgadzalo"
    byloby strojeniem strategii z dodatkowym krokiem.

    Przy remisach w rozkladzie dokladne trafienie bywa niemozliwe - wynik
    jest wtedy pierwsza wartoscia, ktora schodzi PONIZEJ celu (ostroznie).
    """
    if prob <= 0 or not glebokosci:
        return 0.0
    if not 0.0 < wypelnialnosc_docelowa <= 1.0:
        raise ValueError(f"wypełnialność docelowa poza (0, 1]: {wypelnialnosc_docelowa}")
    if sum(1 for g in glebokosci if g >= 0) / prob <= wypelnialnosc_docelowa:
        return 0.0
    malejaco = sorted(glebokosci, reverse=True)
    k = int(wypelnialnosc_docelowa * prob)   # tyle wejsc wolno wypelnic
    if k >= len(malejaco):
        return 0.0
    granica = malejaco[k]                     # pierwsza, ktora ma odpasc
    if k > 0 and malejaco[k - 1] > granica:
        return max(0.0, (malejaco[k - 1] + granica) / 2)
    return max(0.0, math.nextafter(granica, math.inf))


# Rozdzielczosc swiec, z ktorych model pesymistyczny czyta cene wysylki
# i wypelnienie. Panel 4h jest agregatem z dokladnie tych swiec
# (`horizons.H4H.source_timeframe = "1h"`).
KROK_WYKONANIA = timedelta(hours=1)

# Na ile swiec horyzontu mierzymy ruch, ktory przepadl przez niewypelnione
# wejscie. Ta sama liczba co horyzont IC wag (`train.ic_weights`,
# `horyzont_swiec=3`) - ruch, ktory wagi probuja przewidziec.
UTRACONY_RUCH_SWIEC = 3


def indeks_swiec(swiece: list[dict]) -> dict[datetime, dict]:
    """{poczatek swiecy 1h: swieca} - do odczytu po czasie w symulacji."""
    return {s["t"]: s for s in swiece}


def _poczatek_godziny_wysylki(t_wys: datetime) -> datetime:
    """Poczatek swiecy 1h, W KTOREJ wypada wysylka - przedzial (start, koniec].

    Domkniety z prawej: wysylka dokladnie o pelnej godzinie (opoznienie 0)
    nalezy do godziny, ktora sie WLASNIE zamknela, czyli do ostatniej godziny
    swiecy decyzyjnej. Dzieki temu opoznienie 0 daje cene zamkniecia swiecy
    decyzyjnej, a nie zamkniecie godziny pozniejszej.
    """
    pelna = t_wys.replace(minute=0, second=0, microsecond=0)
    return pelna - KROK_WYKONANIA if pelna == t_wys else pelna


def wycena_wysylki(row: dict, bar_minutes: int, opoznienie_min: int,
                   swiece: dict[datetime, dict] | None
                   ) -> tuple[float, dict] | None:
    """(cena w chwili wysylki, godzina PO wysylce) - bez spreadu.

    `row` to wiersz panelu ze swieca DECYZYJNA (znacznik = jej poczatek), wiec
    decyzja zapada w `row.t + bar_minutes`, a zlecenie wychodzi `opoznienie_min`
    pozniej. Cena w chwili wysylki - patrz docstring `Egzekucja`. Druga
    pozycja to swieca 1h, ktora w modelu pesymistycznym rozstrzyga wypelnienie.

    None, gdy brakuje swiecy potrzebnej do ktoregokolwiek kroku - wolajacy
    liczy to jako niewypelnienie i osobno jako dziure w danych.
    """
    if swiece is None:
        return None
    t_wys = row["t"] + timedelta(minutes=bar_minutes + opoznienie_min)
    start = _poczatek_godziny_wysylki(t_wys)
    biezaca = swiece.get(start)
    nastepna = swiece.get(start + KROK_WYKONANIA)
    if biezaca is None or nastepna is None:
        return None
    if opoznienie_min == 0:
        return row["price"], nastepna
    poprzednia = swiece.get(start - KROK_WYKONANIA)
    otwarcie = (poprzednia["close"] if poprzednia is not None
                else biezaca.get("open"))
    if otwarcie is None:
        return None
    waga = (t_wys - start) / KROK_WYKONANIA
    return otwarcie + waga * (biezaca["close"] - otwarcie), nastepna


def z_polowa_spreadu(cena: float, spread_bps: float, kupno: bool) -> float:
    """Cena PostOnly po wlasciwej stronie arkusza: kupno pol spreadu pod cena,
    sprzedaz pol spreadu nad nia. `spread_bps` to PELNY spread (jak w cesze
    `book_spread_bps`), wiec na noge idzie polowa."""
    pol = spread_bps / 2 / 1e4
    return cena * (1 - pol) if kupno else cena * (1 + pol)


def _sizing(score: float, price: float, baza: float) -> tuple[float, float]:
    """Wielkosc zlecenia z FUNKCJI WSPOLNEJ z petla decyzyjna, nie z jej kopii.

    Od 2026-10-03 (U-3) takze z jej zaokraglen - patrz `risk_rules.wielkosc_pozycji`.
    """
    return wielkosc_pozycji(baza, score, price)


def simulate_portfolio(panels: dict[str, list[dict]],
                       weights: dict[str, dict[str, float]],
                       atr_thresholds: dict[str, float | None],
                       gates: "Bramki | dict[str, tuple[float, float] | Bramki]",
                       h: Horizon,
                       min_conf: float,
                       limits: dict | None = None,
                       instrument_ids: dict[str, int] | None = None,
                       cache: dict[str, list[tuple]] | None = None,
                       kind: str = "spot",
                       egzekucja: Egzekucja | None = None,
                       zakladane_pozycje: int | None = None,
                       poslizg: float | None = None,
                       swiece_wykonania: dict[str, dict[datetime, dict]] | None = None,
                       ) -> PortfolioResult:
    """Jedno konto, wszystkie instrumenty, wspolne limity.

    Wejscie i wyjscie po cenie NASTEPNEJ swiecy - decyzja z zamkniecia swiecy T
    nie moze zostac wykonana po cenie tej samej swiecy.

    `swiece_wykonania` - {symbol: indeks_swiec(swiece 1h)}; wymagane, gdy
    `egzekucja` ma opoznienie albo tryb pesymistyczny (patrz `Egzekucja`).
    """
    limits = limits or DEFAULT_LIMITS
    # PROGI SA PER REZIM. Jeden obiekt `Bramki` na okno albo - dla wolaczy,
    # ktorzy podaja progi sami - para liczb, ktora `bramki_z_pary` rozklada na
    # wszystkie rezimy. Nie ma tu wariantu "jedna liczba dla wszystkiego" bez
    # nazwy: `Bramki.powod` zawsze mowi, skad prog pochodzi.
    bram = (dict.fromkeys(panels, gates) if isinstance(gates, Bramki)
            else {s: (g if isinstance(g, Bramki) else bramki_z_pary(*g))
                  for s, g in gates.items()})
    egz = egzekucja or Egzekucja()
    if egz.zmienia_cene_lub_okno and not egz.limit:
        raise ValueError("opóźnienie, spread per instrument i tryb pesymistyczny "
                         "dotyczą zleceń z limitem — zlecenie rynkowe nie czeka "
                         "na wypełnienie")
    z_godzin = bool(egz.opoznienie_min or egz.pesymistyczna)
    if z_godzin and swiece_wykonania is None:
        # Bez swiec 1h model z opoznieniem nie ma skad wziac ceny wysylki.
        # Cichy powrot do `close` swiecy decyzyjnej dawalby wynik, ktory
        # wyglada na policzony z opoznieniem, a jest dzisiejszym modelem.
        raise ValueError("model z opóźnieniem albo pesymistyczny wymaga świec 1h "
                         "(`swiece_wykonania`)")
    spready = egz.spread_per_instrument or {}

    def zlecenie_z_limitem(sym: str, row: dict, nxt_row: dict,
                           kupno: bool, wejscie: bool = False) -> float | None:
        """Cena limitu, jesli zlecenie WESZLO; None, jesli nie.

        Przy wartosciach domyslnych `Egzekucja` to jest dokladnie model sprzed
        2026-10-03: limit = zamkniecie swiecy decyzyjnej, wypelnienie, gdy
        nastepna swieca go dotknela. `wejscie` - przebicie (H-10) i zapis
        glebokosci dotycza tylko wejsc (patrz `Egzekucja`).
        """
        if not egz.zmienia_cene_lub_okno:
            limit_px = row["price"]
            return limit_px if _wypelnione(limit_px, nxt_row, kupno) else None
        if z_godzin:
            w = wycena_wysylki(row, h.bar_minutes, egz.opoznienie_min,
                               swiece_wykonania.get(sym))
            if w is None:
                res.brak_swiec_wykonania += 1
                return None
            cena, godzina = w
            swieca = godzina if egz.pesymistyczna else nxt_row
        else:
            cena, swieca = row["price"], nxt_row
        limit_px = z_polowa_spreadu(cena, spready.get(sym, 0.0), kupno)
        if not wejscie:
            return limit_px if _wypelnione(limit_px, swieca, kupno) else None
        glebokosc = glebokosc_bps(limit_px, swieca, kupno)
        res.glebokosci_wejsc.append(glebokosc)
        return limit_px if glebokosc >= egz.przebicie_bps else None

    k = koszty(kind)
    fee = k.maker if egz.limit else k.taker
    # POSLIZG: ZALOZENIE, KTOREGO DA SIE DOTKNAC Z ZEWNATRZ.
    #
    # `POSLIZG_MAKER` i `POSLIZG_TAKER` sa ZALOZENIAMI, nie pomiarami, i trzeba
    # to powiedziec wprost: na 2026-09-24 poslizg ma ZERO mierzalnych obserwacji.
    # Wszystkie piec wypelnien gieldowych na produkcji ma `exec.order.limit_price`
    # rowne cenie wypelnienia co do grosza - nie dlatego, ze poslizgu nie bylo,
    # tylko dlatego, ze do wdrozenia z 2026-09-23 17:58 kolumna "cena wysłana"
    # byla nadpisywana cena wykonania. Pieciu obserwacji poslizgu nie ma; jest
    # ich zero.
    #
    # Wynik policzony na jednej wartosci takiego zalozenia wyglada jak pomiar
    # i nie da sie go obronic - to ta sama wada co dzisiejsze Sharpe 0,0452.
    # Ten parametr istnieje po to, zeby wolajacy mogl PRZEJECHAC zakres i pokazac,
    # jak wynik od niego zalezy, zamiast wybrac jedna liczbe i o niej zamilczec.
    # `None` zostawia zalozenie z `koszty.py`, czyli zachowanie dotychczasowe.
    slip = (POSLIZG_MAKER if egz.limit else POSLIZG_TAKER) if poslizg is None \
        else poslizg
    # Na instrumencie, ktorego nie da sie shortowac (spot bez pozyczki marzowej),
    # symulacja NIE MOZE otwierac krotkich pozycji - inaczej mierzy strategie,
    # ktorej nie da sie wykonac. W dzienniku produkcyjnym 8 z 12 pozycji to byly
    # wlasnie krotkie sprzedaze na spocie.
    wolno_short = k.krotka_pozycja
    ids = instrument_ids or {s: i for i, s in enumerate(sorted(panels), start=1)}
    # `zakladane_pozycje` nadpisuje liczbe instrumentow w dzielniku budzetu.
    #
    # Domyslnie budzet to limit lacznej ekspozycji podzielony przez liczbe
    # obserwowanych instrumentow - zalozenie, ze zapelnia sie WSZYSTKIE. Pomiar
    # z 2026-09-07 mowi, ze zapelnia sie 2,57 z 10, wiec budzet jest liczony pod
    # portfel, ktory nigdy nie powstaje. To pole sluzy do zmierzenia, co sie
    # stanie przy innym zalozeniu; limit lacznej ekspozycji i tak jest
    # sprawdzany osobno w kazdej swiecy, wiec przekroczyc go sie nie da.
    # KAPITAL PODAJEMY JAWNIE. Symulacja startuje z START_EQUITY, wiec liczba
    # jest ta sama, ktora `position_budget` bralo wczesniej z domyslu - ale
    # domysl znaczyl "kapital nieznany, zgadujemy", a ten argument znaczy
    # "kapital tego przebiegu wynosi tyle". Roznica jest cala trescia naprawy
    # z 2026-09-09: to wlasnie po tym domysle produkcja liczyla budzet od
    # 100 000 na rachunku 10 000.
    #
    # BUDZET STOI W MIEJSCU PRZEZ CALY PRZEBIEG, choc `equity` nizej sie zmienia.
    # Tak bylo od zawsze i tak zostaje: przeliczanie go co swiece zmienialoby
    # zmierzony wynik (wielkosc pozycji rosnie po zyskach), a ta zmiana ma
    # naprawic produkcje, nie przesunac linie odniesienia.
    budzet = position_budget(limits, zakladane_pozycje or len(panels),
                             Decimal(str(START_EQUITY)))
    if budzet is None:
        # Symulacja bez budzetu nie ma czego mierzyc. Cicha stala dawalaby
        # wynik wygladajacy na zmierzony przy limitach, ktorych w ogole nie ma.
        raise ValueError(
            "nie da się wyprowadzić budżetu na pozycję: brak limitu "
            "max_total_exposure albo puste uniwersum instrumentów")
    baza = float(budzet)
    # Bez podanej pamieci podrecznej liczymy ja tutaj - wygodne przy pojedynczym
    # wywolaniu, kosztowne przy dziesiatkach prob na tym samym oknie.
    cache = cache if cache is not None else precompute_panels(panels, atr_thresholds, h)

    # Wspolna os czasu. Instrumenty moga miec rozne pokrycie, wiec bierzemy
    # sume ich znacznikow, a nie przeciecie - inaczej jeden krotszy szereg
    # obcinalby cala symulacje.
    by_time: dict[datetime, dict[str, dict]] = {}
    for sym, rows in panels.items():
        for i, r in enumerate(rows):
            by_time.setdefault(r["t"], {})[sym] = {"row": r, "idx": i}
    timeline = sorted(by_time)
    if len(timeline) < 3:
        return PortfolioResult()

    # FINANSOWANIE JEST TRZECIA POZYCJA RACHUNKU, obok prowizji i poslizgu -
    # i jedyna, ktora zalezy od CZASU TRZYMANIA. Naliczamy je wylacznie tam,
    # gdzie gielda je pobiera (`koszty.Koszty.finansowanie`), z cechy, ktora
    # potok juz liczy.
    #
    # BRAK CECHY PRZERYWA, a nie znaczy zero - sprawdzone raz, przed petla,
    # zeby blad pokazal sie w pierwszej sekundzie, a nie po godzinie liczenia.
    # `compute_funding` nie zapisuje ANI JEDNEGO wiersza dla instrumentu bez
    # `meta.funding_from` albo bez historii w `market.funding_rate`, wiec panel
    # bez tej cechy wyglada identycznie jak panel ze stawka zerowa. Wariant
    # perp policzony na takim panelu mial by zerowy koszt finansowania
    # i wygladal na zmierzony.
    if k.finansowanie:
        for sym, rows in panels.items():
            if not any("funding_rate" in r["f"] for r in rows):
                raise ValueError(
                    f"panel {sym} nie ma ani jednej stawki finansowania "
                    f"(cecha `funding_rate`), a rodzaj instrumentu '{kind}' "
                    "je płaci — symulacja bez tego kosztu zaniżyłaby koszt "
                    "strony długiej i zawyżyła wynik; uzupełnij "
                    "market.funding_rate i przelicz cechy")

    positions: dict[str, Position] = {}
    max_exposure = 0.0
    suma_ekspozycji = 0.0
    suma_przenoszona = 0.0
    swiec_ekspozycji = 0
    suma_pozycji = 0
    swiec_z_pozycja = 0
    realized = 0.0
    fees_paid = 0.0
    finansowanie_razem = 0.0
    wyplat_razem = 0
    res = PortfolioResult()
    order_times: list[datetime] = []
    killswitch: dict | None = None
    peak = START_EQUITY
    equity_hist: list[tuple[datetime, float]] = []

    # Ostatnia ZNANA stawka finansowania per instrument. Osobno od panelu, bo
    # instrument moze nie miec swiecy w danej chwili (rozne pokrycie szeregow),
    # a pozycja i tak wtedy placi - gielda nie pyta, czy mamy dane.
    ostatnia_stawka: dict[str, float] = {}

    for ti, t in enumerate(timeline[:-1]):
        present = by_time[t]

        # --- finansowanie za przedzial (poprzednia swieca, t] ---------------
        #
        # PRZED wycena, bo obniza kapital, ktorym mierzy sie limity tej swiecy.
        # Naliczone po stronie: dodatnia stawka znaczy "dlugie placa krotkim",
        # wiec koszt to `+side * stawka * notional` - krotka noga INKASUJE.
        # Ten znak jest cala trescia porownania spot/perp: gdyby obie strony
        # placily jednakowo, wariant perp wygladalby gorzej, niz jest.
        #
        # Stawke bierzemy z BIEZACEJ swiecy. Cecha w punkcie `t` to ostatnia
        # stawka ogloszona przed `t` (`features.compute_funding`), a wyplata
        # o godzinie `t` idzie wlasnie po niej - wiec nie zagladamy w przyszlosc.
        for sym, cur in present.items():
            stawka = cur["row"]["f"].get("funding_rate")
            if stawka is not None:
                ostatnia_stawka[sym] = stawka
        if k.finansowanie and ti > 0 and positions:
            n_wyplat = wyplaty_finansowania(timeline[ti - 1], t)
            if n_wyplat:
                for sym, pos in positions.items():
                    stawka = ostatnia_stawka.get(sym)
                    if stawka is None:
                        res.wyplat_bez_stawki += n_wyplat
                        continue
                    cur = present.get(sym)
                    px = cur["row"]["price"] if cur else pos.entry
                    # Od NOTIONALU BIEZACEGO, nie od ceny wejscia: gielda
                    # nalicza finansowanie od wartosci pozycji teraz. Ta sama
                    # regula co przy ekspozycji kilka linii nizej.
                    koszt = (pos.side * stawka_finansowania_na_ulamek(stawka)
                             * pos.qty * px * n_wyplat)
                    pos.finansowanie += koszt
                    finansowanie_razem += koszt
                    wyplat_razem += n_wyplat
                    # DO `fees_paid`, A NIE JAKO OSOBNY SKLADNIK KRZYWEJ.
                    #
                    # Wzor na kapital (`START_EQUITY + realized - fees_paid +
                    # unreal`) wystepuje w tej funkcji w trzech miejscach.
                    # Czwarty skladnik trzeba by dopisac do kazdego z nich,
                    # a pominiety w jednym dawalby kapital rozny od sumy
                    # transakcji - cicho i tylko na czesci sciezek. Ten projekt
                    # ma juz jedna taka historie (prowizja odejmowana od krzywej,
                    # ale nie od `ret`). `finansowanie_razem` zostaje WYLACZNIE
                    # do raportowania, bo rozdzial prowizji od finansowania jest
                    # tym, co rozstrzyga o stronie krotkiej.
                    fees_paid += koszt

        # --- wycena biezaca -------------------------------------------------
        unreal = 0.0
        for sym, pos in positions.items():
            cur = present.get(sym)
            px = cur["row"]["price"] if cur else pos.entry
            unreal += pos.side * (px - pos.entry) * pos.qty
        equity = START_EQUITY + realized - fees_paid + unreal
        peak = max(peak, equity)
        equity_hist.append((t, equity))
        res.equity_curve.append((t, equity))

        # Kapital sprzed doby - do limitu straty dziennej.
        cutoff = t - timedelta(hours=24)
        day_ago = START_EQUITY
        for et, ev in reversed(equity_hist):
            if et <= cutoff:
                day_ago = ev
                break

        # --- wyjscia (NIGDY nie podlegaja limitom - wyjsc wolno zawsze) -----
        for sym in list(positions):
            cur = present.get(sym)
            if not cur:
                continue
            nxt_idx = cur["idx"] + 1
            rows = panels[sym]
            if nxt_idx >= len(rows):
                continue
            regime, sygnaly = cache[sym][cur["idx"]]
            score, _conf, _ = combine(sygnaly, regime, weights.get(regime), with_contributions=False)
            pos = positions[sym]
            # PROG WYJSCIA Z REZIMU, W KTORYM POZYCJA JEST TERAZ, nie z tego,
            # w ktorym powstala - i ZAWSZE liczba: rezim bez kalibracji progu
            # wejscia nie moze uniemozliwiac zamkniecia pozycji.
            exit_gate = bram[sym].wyjscie(regime)
            same = (pos.side > 0 and score > 0) or (pos.side < 0 and score < 0)
            if same and abs(score) >= exit_gate:
                continue
            if egz.limit:
                # Wyjscie po cenie zamkniecia swiecy decyzyjnej. Gdy sie nie
                # wypelni, POZYCJA ZOSTAJE OTWARTA - nieudane wyjscie
                # w spadajacym rynku kosztuje najwiecej i musi byc w modelu.
                limit_px = zlecenie_z_limitem(sym, cur["row"], rows[nxt_idx],
                                              kupno=pos.side < 0)
                if limit_px is None:
                    res.niewypelnione_wyjscia += 1
                    continue
                res.wypelnione_wyjscia += 1
                exit_px = limit_px
            else:
                exit_px = rows[nxt_idx]["price"] * (1 - slip * pos.side)
            pnl = pos.side * (exit_px - pos.entry) * pos.qty
            realized += pnl
            fees_paid += pos.qty * exit_px * fee
            res.trades.append({
                # ZWROT PO PROWIZJI. Prowizja jest placona dwa razy - przy wejsciu
                # od notionalu wejscia, przy wyjsciu od notionalu wyjscia - i musi
                # siedziec W ZWROCIE Z TRANSAKCJI, nie tylko w krzywej kapitalu.
                # Sharpe liczy sie wlasnie z tych zwrotow i to on decyduje, czy
                # okno jest dodatnie; poprzednia wersja odejmowala prowizje
                # wylacznie od krzywej, wiec miara, na ktorej opiera sie caly
                # werdykt, byla liczona PRZED kosztami.
                # FINANSOWANIE TAK SAMO JAK PROWIZJA - w zwrocie, nie tylko
                # w krzywej. Powod jest ten sam co akapit wyzej i dotyczy tej
                # samej miary: Sharpe liczy sie z `ret`. Zamiana waluty na
                # ulamek idzie po notionalu WEJSCIA, bo w tej samej bazie
                # wyrazona jest cala reszta tego wyrazenia.
                "ret": (pos.side * (exit_px / pos.entry - 1)
                        - fee * (1 + exit_px / pos.entry)
                        - pos.finansowanie_w_zwrocie),
                "conf": pos.conf, "pokrycie": pos.pokrycie, "symbol": sym,
                "opened_at": pos.opened_at, "closed_at": rows[nxt_idx]["t"],
                # SWIECE TRZYMANIA w tej samej mierze co etykieta IC (H-7):
                # etykieta to zwrot od ceny swiecy i+1 do i+1+H, a transakcja
                # niesie cene od swiecy po decyzji wejscia do swiecy po decyzji
                # wyjscia - roznica indeksow decyzji.
                "swiec": cur["idx"] - pos.idx_wejscia,
                # STRONA I FINANSOWANIE PRZY KAZDEJ TRANSAKCJI.
                #
                # Bez `side` wyniku strony krotkiej nie da sie oddzielic od
                # dlugiej NICZYM - a to jest jedyne pytanie, dla ktorego wariant
                # perp w ogole istnieje: audyt zmierzyl, ze 135 ze 180 sygnalow
                # ponad progiem to sygnaly krotkie, i nikt nie wie, ile byly
                # warte. Odtwarzanie strony po znaku zwrotu jest niemozliwe
                # (stratny long i stratny short wygladaja tak samo).
                #
                # `finansowanie` osobno od `ret`, choc juz w nim siedzi: bez tego
                # nie da sie odpowiedziec, czy krotka noga zarabia na ruchu ceny,
                # czy na inkasowaniu stawki - a to sa dwie rozne strategie
                # i dwa rozne ryzyka.
                "side": pos.side, "finansowanie": pos.finansowanie,
                # NOTIONAL WEJSCIA - baza, w ktorej wyrazony jest `ret`. Bez niego
                # koszt podany jako ulamek rundy (`Karta.doplata_poza_symulacja`)
                # nie daje sie przeliczyc na walute konta, czyli na krzywa
                # kapitalu - patrz `wynik_po_doplacie`.
                "notional": pos.qty * pos.entry,
            })
            del positions[sym]
            order_times.append(t)

        # Po wyjsciach kapital i ekspozycja sie zmienily - przeliczamy.
        unreal = sum(p.side * ((present[s]["row"]["price"] if s in present else p.entry) - p.entry)
                     * p.qty for s, p in positions.items())
        equity = START_EQUITY + realized - fees_paid + unreal
        peak = max(peak, equity)

        # --- limity KONTA, sprawdzane w kazdej swiecy ------------------------
        # Niezaleznie od tego, czy cokolwiek chcemy kupic. Obsuniecie podczas
        # samego trzymania pozycji tez musi uruchomic kill-switch - inaczej
        # limit dziala tylko wtedy, gdy system probuje wejsc, czyli nie wtedy,
        # kiedy jest najbardziej potrzebny.
        # EKSPOZYCJA PO CENIE RYNKOWEJ, nie po cenie wejscia - ta sama regula co
        # w produkcji (`risk.RiskKernel._open_exposure` i `loop.wycen`), z tym
        # samym spadkiem na cene wejscia, gdy swiecy dla instrumentu nie ma.
        # Kosztem liczyly do 2026-09-09 obie strony i obu zwyzka cicho zwalniala
        # miejsce pod kolejne zakupy: limit luzowal sie dokladnie wtedy, gdy
        # zaangazowanie roslo.
        per_inst = {ids[s]: Decimal(str(
            p.qty * (present[s]["row"]["price"] if s in present else p.entry)))
            for s, p in positions.items()}
        exposure = sum(per_inst.values(), Decimal(0))
        hour_ago = t - timedelta(hours=1)
        orders_h = sum(1 for ot in order_times if ot > hour_ago)

        max_exposure = max(max_exposure, float(exposure))
        suma_ekspozycji += float(exposure)
        swiec_ekspozycji += 1
        # Liczymy z `per_inst`, nie z `positions`, zeby oba liczniki opisywaly
        # DOKLADNIE ten sam zbior - inaczej tozsamosc
        # `ekspozycja = srednio_pozycji x sredni_notional` przestaje byc
        # sprawdzalna, a wtedy rozbicie niczego nie dowodzi.
        suma_pozycji += len(per_inst)
        swiec_z_pozycja += 1 if per_inst else 0

        if killswitch is None:
            konto = evaluate_account(
                RiskState(equity=Decimal(str(equity)), peak=Decimal(str(peak)),
                          day_ago=Decimal(str(day_ago)), exposure=exposure,
                          per_instrument=per_inst, orders_last_hour=orders_h,
                          killswitch=None),
                limits)
            if konto.trigger_killswitch:
                killswitch = {"reason": konto.trigger_killswitch, "triggered_at": t}
                res.killswitch_at = t
                res.killswitch_reason = konto.trigger_killswitch

        # Kandydatow sortujemy po SILE SYGNALU, nie po nazwie instrumentu.
        # Gdy limit ekspozycji nie pozwala wejsc we wszystkie, kapital ma
        # dostac najmocniejszy sygnal, a nie ten, ktory jest wczesniej
        # w alfabecie. Poprzednia wersja iterowala po sorted(present) i przez
        # to systematycznie faworyzowala ADAUSDT wzgledem XRPUSDT.
        kandydaci = []
        for sym, cur in present.items():
            if sym in positions:
                continue
            rows = panels[sym]
            if cur["idx"] + 1 >= len(rows):
                continue
            regime, sygnaly = cache[sym][cur["idx"]]
            score, conf, _ = combine(sygnaly, regime, weights.get(regime), with_contributions=False)
            enter_gate = bram[sym].wejscie(regime)
            if enter_gate is None:
                # Rezim bez skalibrowanego progu: NIE WCHODZIMY. Liczymy to
                # jawnie, bo inaczej "brak kalibracji" wyglada w wyniku
                # dokladnie tak samo jak "slabe sygnaly".
                res.pominiete_bez_progu += 1
                continue
            if abs(score) < enter_gate or conf < min_conf:
                continue
            kandydaci.append((abs(score), sym, cur, score, conf))
        kandydaci.sort(key=lambda k: (-k[0], k[1]))

        for _sila, sym, cur, score, conf in kandydaci:
            if score < 0 and not wolno_short:
                res.pominiete_shorty += 1
                continue
            rows = panels[sym]
            nxt_idx = cur["idx"] + 1
            price = rows[nxt_idx]["price"]
            # WIELKOSC POZYCJI Z CENY DECYZJI, WYKONANIE PO CENIE NASTEPNEJ SWIECY.
            #
            # Petla decyzyjna liczy `qty = notional / price`, gdzie `price` to
            # zamkniecie swiecy decyzyjnej (`loop.py`, galaz `enter_*`) - innej
            # ceny w chwili decyzji nie zna. Do 2026-10-02 stala tu cena
            # NASTEPNEJ swiecy, czyli symulacja dzielila budzet przez cene, ktorej
            # produkcja jeszcze nie widzi: po wzroscie kupowala mniej sztuk, po
            # spadku wiecej, niz kupi produkcja. Rozjazd maly co do wielkosci,
            # ale systematyczny i skorelowany z ruchem, ktory jest przedmiotem
            # pomiaru. Cena wykonania (`entry` nizej) zostaje cena NASTEPNEJ
            # swiecy - decyzja z zamkniecia T nie moze sie wypelnic po cenie T.
            cena_decyzji = cur["row"]["price"]
            notional, qty = _sizing(score, cena_decyzji, baza)

            state = RiskState(
                equity=Decimal(str(equity)), peak=Decimal(str(peak)),
                day_ago=Decimal(str(day_ago)), exposure=exposure,
                per_instrument=per_inst, orders_last_hour=orders_h,
                killswitch=killswitch,
            )
            # RODZAJ INSTRUMENTU IDZIE DO REGUL, tak samo jak w produkcji
            # (`risk.RiskKernel.check` czyta go z `ref.instrument`). Bez tego
            # symulacja wariantu perp nie zobaczylaby wymogu limitu dzwigni,
            # ktory produkcje zatrzyma - czyli mierzylaby strategie luzniejsza
            # niz ta, ktora system wykona. Dokladnie ta roznica kazala
            # przeniesc limity do `risk_rules` (patrz docstring modulu).
            v = evaluate_limits(state, limits, ids[sym],
                                Decimal(str(notional)), Decimal(str(qty)),
                                kind=kind)

            if v.trigger_killswitch and killswitch is None:
                # Kill-switch wymaga RECZNEGO odblokowania, wiec do konca okna
                # nie ma juz zadnych wejsc. To nie jest ostroznosc symulacji -
                # tak zachowa sie produkcja.
                killswitch = {"reason": v.trigger_killswitch, "triggered_at": t}
                res.killswitch_at = t
                res.killswitch_reason = v.trigger_killswitch

            if v.verdict == "block":
                res.blocked += 1
                continue
            if v.verdict == "downsize":
                res.downsized += 1
                qty = float(v.qty)
                notional = qty * cena_decyzji

            side = 1 if score > 0 else -1
            if egz.limit:
                limit_px = zlecenie_z_limitem(sym, cur["row"], rows[nxt_idx],
                                              kupno=side > 0, wejscie=True)
                if limit_px is None:
                    res.niewypelnione_wejscia += 1
                    # Ruch, ktory przepadl: od ceny, po ktorej chcielismy wejsc,
                    # do ceny UTRACONY_RUCH_SWIEC swiec po decyzji (albo konca
                    # panelu). Liczony ze strony pozycji.
                    cel = rows[min(cur["idx"] + UTRACONY_RUCH_SWIEC, len(rows) - 1)]
                    res.utracone_ruchy.append(side * (cel["price"] / cena_decyzji - 1))
                    continue
                res.wypelnione_wejscia += 1
                entry = limit_px
            else:
                entry = price * (1 + slip * side)
            # POKRYCIE LICZONE TYLKO DLA TRANSAKCJI, KTORA NAPRAWDE POWSTAJE.
            # `combine` wyzej idzie dla kazdej swiecy kazdego instrumentu, wiec
            # slad przy tamtym wywolaniu alokowalby slownik miliony razy.
            # Tutaj wywolan jest tyle, ile transakcji - rzedu tysiaca na okno.
            _slad_pokrycia: dict = {}
            combine(cache[sym][cur["idx"]][1], cache[sym][cur["idx"]][0],
                    weights.get(cache[sym][cur["idx"]][0]),
                    with_contributions=False, slad=_slad_pokrycia)
            positions[sym] = Position(side=side, qty=qty, entry=entry, conf=conf,
                                      opened_at=t,
                                      pokrycie=_slad_pokrycia.get("pokrycie", 1.0),
                                      idx_wejscia=cur["idx"])
            fees_paid += qty * entry * fee
            order_times.append(t)

            per_inst[ids[sym]] = per_inst.get(ids[sym], Decimal(0)) + Decimal(str(qty * entry))
            exposure += Decimal(str(qty * entry))
            orders_h += 1

        # Po wejsciach: to, co portfel niesie przez przedzial (t, t+1].
        suma_przenoszona += float(exposure)

    # Domkniecie po ostatniej swiecy - pozycja otwarta na koncu okna liczy sie
    # po ostatniej znanej cenie, inaczej jej wynik znikalby z oceny.
    last_t = timeline[-1]
    for sym, pos in list(positions.items()):
        cur = by_time[last_t].get(sym)
        px = cur["row"]["price"] if cur else pos.entry
        # Domkniecie po ostatniej swiecy idzie ZAWSZE po cenie rynkowej -
        # koniec okna nie czeka na wypelnienie limitu.
        exit_px = px * (1 - POSLIZG_TAKER * pos.side)
        realized += pos.side * (exit_px - pos.entry) * pos.qty
        fees_paid += pos.qty * exit_px * k.taker
        # Domkniecie na koncu okna idzie po cenie rynkowej, wiec wyjscie placi
        # stawke TAKER niezaleznie od trybu egzekucji - tak jak w krzywej kapitalu.
        res.trades.append({"ret": (pos.side * (exit_px / pos.entry - 1)
                                   - fee - k.taker * exit_px / pos.entry
                                   - pos.finansowanie_w_zwrocie),
                           "conf": pos.conf, "pokrycie": pos.pokrycie,
                           "symbol": sym, "opened_at": pos.opened_at, "closed_at": last_t,
                           # Domkniecie koncem okna UCINA trzymanie - liczba
                           # swiec jest tu dolnym oszacowaniem, stad znacznik.
                           # Cena wyjscia to cena swiecy `last_t` (bez +1).
                           "swiec": (cur["idx"] - pos.idx_wejscia - 1) if cur else None,
                           "koniec_okna": True,
                           "side": pos.side, "finansowanie": pos.finansowanie,
                           "notional": pos.qty * pos.entry})
    res.equity_curve.append((last_t, START_EQUITY + realized - fees_paid))
    res.max_exposure = max_exposure
    res.avg_exposure = (suma_ekspozycji / swiec_ekspozycji) if swiec_ekspozycji else 0.0
    res.avg_exposure_przenoszona = ((suma_przenoszona / swiec_ekspozycji)
                                    if swiec_ekspozycji else 0.0)
    if swiec_ekspozycji:
        res.udzial_swiec_z_pozycja = swiec_z_pozycja / swiec_ekspozycji
        res.srednio_pozycji = suma_pozycji / swiec_ekspozycji
    if swiec_z_pozycja:
        res.pozycji_gdy_w_rynku = suma_pozycji / swiec_z_pozycja
    if suma_pozycji:
        res.sredni_notional = suma_ekspozycji / suma_pozycji
    res.base_notional = baza
    res.prowizja = fee
    res.poslizg = slip
    res.finansowanie_zaplacone = finansowanie_razem
    res.wyplat_finansowania = wyplat_razem
    return res


def metrics(res: PortfolioResult, min_trades: int) -> dict:
    """Miary z symulacji portfelowej. Obsuniecie z krzywej KAPITALU, nie z transakcji."""
    rets = [t["ret"] for t in res.trades]
    curve = [e for _, e in res.equity_curve] or [START_EQUITY]

    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    total_return = (curve[-1] / START_EQUITY - 1) * 100

    base = {"max_dd": round(max_dd, 4), "n_trades": len(rets),
            "total_return": round(total_return, 4), "trades": res.trades,
            # UDZIAL kapitalu, ktory srednio pracowal - w tej samej bazie
            # co total_return, wiec te dwie liczby wolno przez siebie
            # dzielic. Bez niej zwrot z okna jest nieporownywalny
            # z czymkolwiek o innej ekspozycji.
            "avg_exposure": round(res.avg_exposure / START_EQUITY, 6),
            # Ta sama miara liczona PO wejsciach - do porownan z odniesieniami
            # (patrz `PortfolioResult.avg_exposure_przenoszona`).
            "ekspozycja_przenoszona": round(res.avg_exposure_przenoszona / START_EQUITY, 6),
            # Czynniki tej sredniej. Mnoza sie z powrotem:
            #   avg_exposure = srednio_pozycji x notional_na_pozycje
            "w_rynku": round(res.udzial_swiec_z_pozycja, 6),
            "srednio_pozycji": round(res.srednio_pozycji, 6),
            # Pozycji na swiece, w ktorej COKOLWIEK jest otwarte. To ten czynnik
            # wchodzi do iloczynu trzech skladnikow; `srednio_pozycji` jest juz
            # usredniona po swiecach pustych, wiec pomnozenie jej przez `w_rynku`
            # liczyloby ten sam czynnik dwa razy.
            "pozycji_w_rynku": round(res.pozycji_gdy_w_rynku, 6),
            "notional_na_pozycje": round(res.sredni_notional / START_EQUITY, 6),
            # Budzet wyliczony z limitu - odniesienie dla powyzszego. Roznica
            # miedzy nimi to `udzial_budzetu(score)`, czyli sila sygnalu.
            "budzet_na_pozycje": round(res.base_notional / START_EQUITY, 6),
            "blocked": res.blocked, "downsized": res.downsized,
            "killswitch": res.killswitch_reason,
            "niewypelnione": res.niewypelnione_wejscia + res.niewypelnione_wyjscia,
            "wypelnione": res.wypelnione_wejscia + res.wypelnione_wyjscia,
            # SAME WEJSCIA (2026-10-11). Kryterium H-5 i pomiar demo (42/64)
            # dotycza WEJSC; suma wyzej miesza je z wyjsciami, ktore ponawiaja
            # sie co decyzje i na produkcji koncza takerem. Do 11.10 trener
            # zestawial z demo wlasnie te sume.
            "niewypelnione_wejscia": res.niewypelnione_wejscia,
            "wypelnione_wejscia": res.wypelnione_wejscia,
            "pominiete_shorty": res.pominiete_shorty,
            "pominiete_bez_progu": res.pominiete_bez_progu}
    if len(rets) < min_trades:
        return base | {"sharpe": 0.0, "sortino": 0.0, "hit_rate": 0.0, "avg_r": 0.0,
                       "profit_factor": 0.0, "insufficient": True}

    mean = statistics.fmean(rets)
    sd = max(statistics.pstdev(rets), MIN_SD)
    down = [r for r in rets if r < 0]
    dsd = max(statistics.pstdev(down) if len(down) > 1 else sd, MIN_SD)
    gains = sum(r for r in rets if r > 0)
    losses = -sum(r for r in rets if r < 0)
    return base | {
        "sharpe": round(mean / sd, 4), "sortino": round(mean / dsd, 4),
        "hit_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "avg_r": round(mean * 100, 4),
        "profit_factor": round(gains / losses, 4) if losses > 0 else 0.0,
    }


def wynik_po_doplacie(res: PortfolioResult, doplata: float) -> PortfolioResult:
    """Wynik symulacji PO doliczeniu kosztu, ktorego symulacja nie nalicza.

    `doplata` to `Karta.doplata_poza_symulacja` - ta sama liczba, ktora DSR
    odejmuje od zlaczonej proby. Przepuszczenie wyniku okna przez te funkcje
    PRZED `metrics` sprawia, ze KAZDA miara okna (Sharpe, trafnosc, zwrot
    z konta, obsuniecie) opisuje swiat po doplacie z karty, a nie tylko werdykt
    DSR. Do 2026-10-02 bramka statusu brala trzy miary sprzed spreadu i jedna
    po nim.

    DWIE BAZY, JEDNA DOPLATA:
      * `ret` transakcji - przez `koszty.zwroty_po_doplacie`, czyli dokladnie
        tak, jak odejmuje ja DSR,
      * krzywa kapitalu - doplata razy NOTIONAL WEJSCIA transakcji, w walucie.
        Polowa na wejsciu, polowa na wyjsciu: karta liczy spread jako "polowe
        spreadu na noge razy dwie nogi", a symulacja tak samo rozklada
        prowizje. Wejscie obciaza punkty krzywej PO swiecy decyzji (tam, gdzie
        pojawia sie prowizja wejscia), wyjscie - od `closed_at` wlacznie.
        Ostatni punkt krzywej niesie wiec caly koszt, a obsuniecie widzi go
        tam, gdzie zostal zaplacony, a nie dopiero na koncu okna.

    Doplata zero oddaje TEN SAM obiekt - zachowanie sprzed zmiany co do bitu.
    Transakcja bez `notional` przy doplacie dodatniej PRZERYWA: koszt bez bazy
    nie ma sie od czego policzyc, a zero byloby tu cichym klamstwem.
    """
    if not doplata:
        return res
    for t in res.trades:
        if "notional" not in t:
            raise ValueError("transakcja bez `notional` - nie da się przeliczyć "
                             "dopłaty na walutę konta")
    nowe_zwroty = zwroty_po_doplacie([t["ret"] for t in res.trades], doplata)
    trades = [t | {"ret": r} for t, r in zip(res.trades, nowe_zwroty)]
    polowa = [doplata * t["notional"] / 2 for t in res.trades]
    curve = []
    for ct, ev in res.equity_curve:
        koszt = sum(p for t, p in zip(res.trades, polowa) if ct > t["opened_at"])
        koszt += sum(p for t, p in zip(res.trades, polowa) if ct >= t["closed_at"])
        curve.append((ct, ev - koszt))
    return replace(res, trades=trades, equity_curve=curve)


POWTORNE_WEJSCIE_SWIEC = 3


def udzial_powtornych_wejsc(trades: list[dict], bar_minutes: int,
                            swiece: int = POWTORNE_WEJSCIE_SWIEC) -> float | None:
    """Odsetek transakcji, ktore sa POWROTEM w ten sam instrument tuz po wyjsciu.

    NIE JEST TO CZESTOTLIWOSC HANDLU. Licznik pyta o jedna, waska rzecz: ile
    transakcji otwarto w instrumencie, z ktorego wyszlismy mniej niz `swiece`
    swiec wczesniej. Okno o dwustu transakcjach na dwudziestu instrumentach
    moze miec tu zero, a okno o trzydziestu - polowe.

    PO CO. Hipoteza postawiona 2026-09-27 mowi, ze zaobserwowany spadek zwrotu
    na transakcje w gestych oknach `range` bierze sie z `EXIT_RATIO = 0.45`:
    wyjscie przy 0,126 po wejsciu na 0,280 zamyka pozycje przy kazdym
    oscylowaniu wyniku i natychmiast otwiera ja z powrotem, a kazda taka runda
    lapie coraz mniejszy wycinek tego samego ruchu. Sygnatura zjawiska to
    NIEZMIENIONA TRAFNOSC przy zalamanym zwrocie na transakcje - dokladnie to,
    co widac w danych - i tego wlasnie nie da sie odroznic od "prog przepuszcza
    gorsze sygnaly" bez tej jednej liczby.
    Warunek rozstrzygajacy ustalony PRZED pomiarem: w oknach `range` z >= 55
    transakcjami odsetek ponad 40% przy jednoczesnie ponizej 15% w oknach
    z < 55 potwierdza hipoteze; podobne wartosci w obu kubelkach ja obalaja.

    WYNIK: HIPOTEZA OBALONA, 2026-09-27 wieczorem. Warunek nie zostal spelniony
    ani w polowie, a pomiar poszedl na produkcyjnych nastawach, suchym
    przebiegiem, na obu torach:

        tor 4h, 37 okien   range >=55 transakcji: mediana 15% (zakres 7-18)
                           range < 55           : mediana  6% (zakres 4-21)
        tor 1h, 89 okien   range >=55           : mediana 10% (zakres 6-19)
                           range < 55           : mediana  8% (zakres 5-15)

    Progu 40% nie dotyka zadne okno. Korelacja odsetka powrotow z Sharpe'em na
    transakcje: r = +0,053 po 37 oknach (4h) i -0,074 po 89 (1h), czyli zero
    w obie strony. `EXIT_RATIO = 0,45` NIE wypycha nas z pozycji, w ktora zaraz
    wracamy, i nie tlumaczy spadku zwrotu na transakcje w gestych oknach
    `range`. Byla to ostatnia zyjaca hipoteza w tej sprawie: whipsaw (trafnosc
    0,4664 -> 0,4731 przy zwrocie nizszym o 0,424 pp) i skala wyniku (iloraz
    zmiennosci test/trening 0,8266 wobec 0,8278) odpadly wczesniej, a limit
    czestotliwosci po poprawce na nakladanie okien zeszedl do +0,0084.

    MIARA ZOSTAJE, mimo obalenia hipotezy, ktora ja zamowila. Jest tanio
    liczona, idzie do `learn.wf_metric` i do wydruku okna, a pytanie "czy prog
    wyjscia nie robi z nas kolowrotka" wroci przy kazdej zmianie `EXIT_RATIO`.
    Usuniecie jej znaczylo by, ze nastepny raz trzeba zaczac od zera - i ze
    ktos moze postawic te sama hipoteze nie wiedzac, ze zostala zmierzona.

    Odstep liczymy w SWIECACH tego horyzontu (`bar_minutes`), nie w minutach:
    ten sam kod liczy tor 4-godzinny i godzinowy, a "3 swiece" znaczy w nich
    dwanascie i trzy godziny.

    None przy braku transakcji - zero znaczyloby "zmierzone i wyszlo zero",
    a nie ma czego mierzyc.
    """
    if not trades:
        return None
    prog = timedelta(minutes=bar_minutes * swiece)
    wg_symbolu: dict[str, list[dict]] = {}
    for t in trades:
        wg_symbolu.setdefault(t["symbol"], []).append(t)
    powtorne = 0
    for lista in wg_symbolu.values():
        lista.sort(key=lambda t: t["opened_at"])
        for poprzednia, kolejna in zip(lista, lista[1:]):
            if kolejna["opened_at"] - poprzednia["closed_at"] < prog:
                powtorne += 1
    return powtorne / len(trades)


def kup_i_trzymaj(panels: dict[str, list[dict]], kind: str = "spot",
                  doplata: float = 0.0) -> float:
    """Zwrot z prostego kupienia i trzymania calego zbioru, rowna waga.

    PUNKT ODNIESIENIA, KTOREGO BRAKOWALO.

    Odsetek dodatnich okien nie mowi nic o tym, czy strategia jest LEPSZA od
    nierobienia niczego. Strategia wylacznie dluga na rosnacym rynku bedzie
    miala duzo dodatnich okien z samego faktu, ze rynek rosl - i wyglada to
    identycznie jak umiejetnosc.

    Krypto miedzy 2022 a 2026 mocno wzroslo, wiec bez tego porownania kazdy
    wynik dlugiej strony jest nie do zinterpretowania.

    Koszt liczymy raz: jedno wejscie i jedno wyjscie na instrument, po cenie
    rynkowej - bo tyle wlasnie kosztuje kupic i trzymac.

    `doplata` - KOSZT RUNDY SPOZA SYMULACJI, TAKZE DLA ODNIESIENIA. Od
    2026-10-02 strategia jest mierzona po `Karta.doplata_poza_symulacja`
    (spread, niewypelnienie), wiec odniesienie bez niej bylo by asymetryczne
    w strone TRUDNIEJSZEJ bramki: strategia placilaby spread, a koszyk nie.
    Kup-i-trzymaj tez przechodzi przez spread - raz przy wejsciu, raz przy
    wyjsciu - czyli placi JEDNA doplate rundy na instrument. Wolajacy podaje
    doplate z karty RYNKOWEJ (`karta_rundy(kind, maker=False, ...)`), bo tak
    ten koszyk jest kupowany: prowizja i poslizg sa juz policzone wyzej.

    OBCIAZENIE KONCA OKNA (audyt 2 R-d, audyt Fable F-3.5) NIE SIEDZIALO
    W TEJ FUNKCJI, tylko w mnozniku, przez ktory wolajacy ja skaluje. Koszyk
    niesie cene przez wszystkie przedzialy od `rows[0]` do `rows[-1]`, kazdy
    raz - takze ostatni. Ekspozycja strategii (`avg_exposure`) gubila po
    jednym przedziale na transakcje, takze na domknieciu wymuszonym koncem
    okna, wiec „koszyk przy tej samej ekspozycji" byl mnozony przez za mala
    liczbe. Porownania z odniesieniami ida od 2026-10-03 po
    `ekspozycja_przenoszona` (patrz `PortfolioResult`) - test
    `tests/test_kup_i_trzymaj_koniec_okna.py`.
    """
    k = koszty(kind)
    zwroty = []
    for rows in panels.values():
        if len(rows) < 2:
            continue
        wejscie = rows[0]["price"] * (1 + POSLIZG_TAKER)
        wyjscie = rows[-1]["price"] * (1 - POSLIZG_TAKER)
        brutto = wyjscie / wejscie - 1
        zwroty.append(zwroty_po_doplacie([brutto - 2 * k.taker], doplata)[0])
    return sum(zwroty) / len(zwroty) * 100 if zwroty else 0.0
