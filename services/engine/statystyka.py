"""Statystyka wspolna dla trenera i diagnostyki.

Wydzielona, bo obie strony jej potrzebuja, a wzajemny import trenera
i diagnostyki tworzyl cykl.
"""
from __future__ import annotations

import math
import statistics


def rangi(xs: list[float]) -> list[float]:
    """Rangi z usrednianiem remisow."""
    posortowane = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(posortowane):
        j = i
        while j + 1 < len(posortowane) and xs[posortowane[j + 1]] == xs[posortowane[i]]:
            j += 1
        srednia = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[posortowane[k]] = srednia
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Korelacja rangowa. None, gdy proba za mala albo ktoras kolumna stala.

    Rangowa, a nie liniowa, bo interesuje nas UPORZADKOWANIE sytuacji od
    najlepszej do najgorszej, a nie skala - wyniki sygnalow sa i tak przycinane
    do zakresu -1..1, wiec ich skala jest umowna.

    Zwraca None zamiast liczby, gdy nie ma czego liczyc. Liczba bez znaczenia
    jest gorsza niz jej brak, bo wyglada jak wynik.
    """
    if len(xs) < 30:
        return None
    rx, ry = rangi(xs), rangi(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    licz = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return licz / (sx * sy) if sx > 0 and sy > 0 else None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Przedzial ufnosci dla odsetka - metoda Wilsona.

    Nie zwykly przedzial normalny, bo ten przy malych probach i odsetkach
    blisko krancow potrafi wyjsc poza [0,1] i podaje falszywie waskie granice.
    Wilson nie ma tej wady i jest standardem przy n rzedu kilkudziesieciu.

    DLACZEGO TO JEST TU POTRZEBNE

    Przy 40 oknach przedzial ma szerokosc okolo 30 punktow procentowych. Wynik
    52,5% to [37,5%, 67,1%], a 62,5% to [47,0%, 75,8%] - przedzialy niemal sie
    pokrywaja. Bez tej informacji roznica dziesieciu punktow wyglada jak
    poprawa, a jest szumem.

    Cztery kolejne dobowe przebiegi, roznce sie wylacznie przesunieciem okna
    o jeden dzien, daly 52,5%, 57,5%, 60,0% i 62,5%. To jest skala wahania
    samej miary, nie zmiany strategii.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    srodek = (p + z * z / (2 * n)) / d
    polowa = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, srodek - polowa), min(1.0, srodek + polowa))


# Wartosci krytyczne rozkladu t Studenta, dwustronnie, poziom 95%, wedlug
# stopni swobody. Tablica, a NIE stale 1,96, bo proby, ktore ten projekt liczy,
# bywaja jednocyfrowe: przy trzech obserwacjach przedzial normalny jest ponad
# dwa razy za waski. Za waski przedzial nie jest drobna niescisloscia - mowi
# "zmierzone i rozne od zalozenia" o czyms, czego jeszcze nie widac.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
       25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def t95(df: int) -> float:
    """Wartosc krytyczna t dla podanej liczby stopni swobody.

    Miedzy wpisami tablicy bierzemy wartosc dla NAJBLIZSZEGO MNIEJSZEGO df,
    czyli zawsze ostrozniejsza (szersza) z dwoch. Interpolacja bylaby
    dokladniejsza i bez znaczenia: roznica miedzy 2,086 a 2,060 jest o rzad
    wielkosci mniejsza niz rozrzut, ktory tym mierzymy.

    MNIEJSZEGO, A NIE WIEKSZEGO - to nie jest ta sama strona. Wartosc krytyczna
    t MALEJE wraz z df, wiec siegniecie po WIEKSZY df daje wartosc MNIEJSZA,
    czyli przedzial WEZSZY: dokladnie odwrotnie niz mowilo uzasadnienie i
    dokladnie w strone, przed ktora ostrzega komentarz przy tablicy. Do
    2026-09-23 petla szla `for k in sorted(T95): if df <= k`, przez co df=11
    dostawalo 2,179 zamiast 2,201 (za wasko o 1,0%), df=13 -> 2,131 zamiast
    2,160, df=16 -> 2,086 zamiast 2,120 (najwiekszy blad, 1,6%).

    Skala bledu byla mala, kierunek nie: `poslizg.py` liczy przez
    `przedzial_sredniej` przedzial zmierzonego poslizgu i ALARMUJE, gdy
    zalozenie POSLIZG_TAKER wypada z przedzialu. Przy 17 wypelnieniach (df=16)
    przedzial byl 1,6% za waski, wiec zalozenie lezace tuz wewnatrz
    prawdziwego przedzialu wypadalo z policzonego - falszywy alarm "poslizg
    rozjechal sie z zalozeniem" i pokusa, zeby zmienic stala kosztu na
    podstawie szumu.

    Powyzej najwiekszego wpisu (df > 120) zostaje 1,980, a nie asymptotyczne
    1,96, z tego samego powodu: szersza z dwoch.
    """
    for k in sorted(T95, reverse=True):
        if df >= k:
            return T95[k]
    return T95[1]


# Ponizej tego rozrzutu proba nie niesie informacji o zmiennosci i iloraz
# przestaje byc miara czegokolwiek. Nie jest to podloga PODSTAWIANA pod
# mianownik - to prog, ponizej ktorego oddajemy None.
#
# ROZNICA WOBEC `portfolio.MIN_SD` JEST CALA RZECZA. Tam 1e-4 wchodzi do
# mianownika przez `max(pstdev(rets), MIN_SD)`, wiec osiem identycznych zwrotow
# po 1% daje Sharpe 100 - i ta setka jest liczba wpisana, zeby nie bylo
# dzielenia przez zero, a nie przewaga stukrotna. Tutaj taka proba oddaje None.
MIN_ROZRZUT = 1e-12


def sharpe(zwroty: list[float]) -> float | None:
    """Sredni zwrot podzielony przez jego odchylenie. NA TRANSAKCJE, nie rocznie.

    NAZWA JEST MYLACA I TRZEBA TO POWIEDZIEC WPROST. „Sharpe" bez przymiotnika
    znaczy w finansach miare ANUALIZOWANA. Ta nie jest anualizowana i nie da sie
    jej porownac z zadna liczba z literatury: roznica to czynnik rzedu
    pierwiastka z liczby transakcji w roku, czyli kilkanascie razy. Prog
    produkcyjny 0,05 jest dobrany do TEJ miary - patrz komentarz w `train.py`.

    ODCHYLENIE PROBKOWE, NIE POPULACYJNE. `portfolio.metrics` liczy `pstdev`,
    czyli dzieli przez n zamiast przez n-1. Przy osmiu transakcjach zaniza
    mianownik o 6,5%, czyli zawyza Sharpe o tyle samo - w kazdym oknie i zawsze
    w te sama strone. Probka nie jest populacja; okno testowe jest wycinkiem.

    None, a nie liczba, gdy proba jest jednoelementowa albo stala. Jedna
    transakcja nie ma rozrzutu, a proba stala ma rozrzut zero - w obu wypadkach
    iloraz albo nie istnieje, albo powstaje wylacznie z tego, czym zatkano
    mianownik. To ta sama zasada, co w `przedzial_sredniej`: brak pomiaru musi
    wygladac inaczej niz pomiar.
    """
    if len(zwroty) < 2:
        return None
    sd = statistics.stdev(zwroty)
    if sd < MIN_ROZRZUT:
        return None
    return statistics.fmean(zwroty) / sd


def profit_factor(zwroty: list[float]) -> float | None:
    """Suma zyskow podzielona przez sume strat. None, gdy strat NIE BYLO.

    DLACZEGO None, A NIE ZERO. `portfolio.metrics` zwraca w tym miejscu 0.0,
    czyli okno bez ani jednej straty - najlepsze mozliwe - dostaje najgorsza
    mozliwa wartosc. Wchodzi potem do sredniej po oknach i ciagnie ja w dol,
    podczas gdy okno o wartosci 129 ciagnie ja w gore. Zaden z tych dwoch
    wplywow nie jest zamierzony i oba sa niewidoczne w wyniku.

    Zero oddajemy wylacznie wtedy, gdy zyskow nie bylo wcale - to jest
    prawdziwe zero, nie brak wartosci.
    """
    if not zwroty:
        return None
    zyski = sum(r for r in zwroty if r > 0)
    straty = -sum(r for r in zwroty if r < 0)
    if straty <= 0:
        return None
    return zyski / straty


def _kwantyl(posortowane: list[float], p: float) -> float | None:
    """Kwantyl z interpolacja liniowa. None, gdy proba za mala."""
    n = len(posortowane)
    if n < 4:
        return None
    poz = p * (n - 1)
    dol = int(poz)
    gora = min(dol + 1, n - 1)
    return posortowane[dol] + (posortowane[gora] - posortowane[dol]) * (poz - dol)


def rozklad(xs: list[float]) -> dict:
    """Opis rozkladu, a nie jedna liczba. CZYSTA FUNKCJA.

    DLACZEGO TO ISTNIEJE JAKO OSOBNA FUNKCJA

    Zeby nie dalo sie podac sredniej bez mediany i bez odsetka ujemnych.
    Raport walk-forward podawal `profit_factor 1,6419 (0 .. 129,2275)`: srednia
    z wielkosci nieograniczonej z gory, przy ktorej jedno okno na czterdziesci
    decyduje o calej liczbie. Mediana odpowiada na pytanie, ktore naprawde
    zadajemy - jak wyglada TYPOWE okno - a odsetek ujemnych na drugie:
    jak czesto jest zle.

    Kwartyle, a nie sam zakres. Zakres opisuje dwie skrajne obserwacje;
    przy rozkladzie skosnym to akurat te dwie, ktorych nie chcemy uogolniac.
    """
    n = len(xs)
    if n == 0:
        return {"n": 0, "srednia": None, "mediana": None, "q1": None, "q3": None,
                "najmniejszy": None, "najwiekszy": None,
                "ujemnych": 0, "udzial_ujemnych": None}
    s = sorted(xs)
    ujemnych = sum(1 for x in xs if x < 0)
    return {
        "n": n,
        "srednia": statistics.fmean(xs),
        "mediana": statistics.median(s),
        "q1": _kwantyl(s, 0.25),
        "q3": _kwantyl(s, 0.75),
        "najmniejszy": s[0],
        "najwiekszy": s[-1],
        "ujemnych": ujemnych,
        "udzial_ujemnych": ujemnych / n,
    }


def przedzial_sredniej(xs: list[float]) -> tuple[float, float] | None:
    """Przedzial ufnosci 95% dla SREDNIEJ z proby. None, gdy nie ma czego liczyc.

    None, a nie (x, x), przy jednej obserwacji. Przedzial o zerowej szerokosci
    wygladalby jak pomiar idealnie precyzyjny, a jedna obserwacja nie mowi
    o rozrzucie NIC. To ta sama zasada, przez ktora rozjazd_z_symulacja zwraca
    None zamiast zera: brak pomiaru musi wygladac inaczej niz pomiar.
    """
    n = len(xs)
    if n < 2:
        return None
    m = statistics.fmean(xs)
    polowa = t95(n - 1) * statistics.stdev(xs) / math.sqrt(n)
    return (m - polowa, m + polowa)


# ------------------------------------------------- deflowany Sharpe (DSR)
#
# Stala Eulera-Mascheroniego. Wchodzi do wzoru na OCZEKIWANE MAKSIMUM z N
# probek rozkladu normalnego - to jest przyblizenie Bailey'a i Lopeza de Prado
# (2014), a nie stala dobrana pod wynik.
GAMMA_EULERA = 0.5772156649015329

# Prog, powyzej ktorego DSR mozna czytac jako „wynik nie jest artefaktem
# selekcji". 0,95 to ten sam poziom, na ktorym stoi kazdy inny przedzial w tym
# module (`wilson`, `t95`) - nie osobna konwencja dla tej jednej miary.
PROG_DSR = 0.95

_N01 = statistics.NormalDist()


def _momenty(xs: list[float]) -> tuple[float, float, float] | None:
    """Drugi, trzeci i czwarty moment centralny, dzielone przez n (nie n-1)."""
    n = len(xs)
    if n < 2:
        return None
    m = statistics.fmean(xs)
    d = [x - m for x in xs]
    m2 = sum(t * t for t in d) / n
    if m2 < MIN_ROZRZUT * MIN_ROZRZUT:
        return None
    m3 = sum(t ** 3 for t in d) / n
    m4 = sum(t ** 4 for t in d) / n
    return m2, m3, m4


def skosnosc(xs: list[float]) -> float | None:
    """Skosnosc proby - MOMENTOWA (dzielona przez n), nie nieobciazona.

    Ta wersja, a nie poprawka Fishera na obciazenie, bo wzor na deflowany
    Sharpe jest wyprowadzony dla momentow rozkladu i taka wlasnie wielkosc
    w nim stoi. Przy probach rzedu tysiaca transakcji roznica miedzy wersjami
    jest ulamkiem procenta; przy n rzedu dziesieciu byla by istotna i dlatego
    ta funkcja NIE jest przeznaczona do malych prob.

    None, gdy proba jest jednoelementowa albo stala - ta sama zasada, co
    w `sharpe`: brak pomiaru musi wygladac inaczej niz pomiar.
    """
    mm = _momenty(xs)
    if mm is None:
        return None
    m2, m3, _ = mm
    return m3 / m2 ** 1.5


def kurtoza(xs: list[float]) -> float | None:
    """Kurtoza proby - NIE NADWYZKOWA. Rozklad normalny ma tu 3, nie 0.

    NAZWA JEST MYLACA I TRZEBA TO POWIEDZIEC WPROST, dokladnie jak przy
    `sharpe`. Wieksza czesc bibliotek (scipy, pandas) zwraca pod nazwa
    „kurtosis" NADWYZKE nad rozkladem normalnym, czyli liczbe o 3 mniejsza.
    Wzor na deflowany Sharpe potrzebuje surowej kurtozy: podstawienie
    nadwyzki zamienia czlon `(g4 - 1) / 4` na `(g4 - 4) / 4`, czyli przy
    rozkladzie normalnym z +0,5 na -0,25 - a to ODWRACA znak poprawki i czyni
    bramke LATWIEJSZA do przejscia, bez zadnego objawu.
    """
    mm = _momenty(xs)
    if mm is None:
        return None
    m2, _, m4 = mm
    return m4 / (m2 * m2)


def oczekiwany_maksymalny_sharpe(n_prob: int,
                                odchylenie_prob: float) -> float | None:
    """Oczekiwane MAKSIMUM Sharpe'a z `n_prob` prob, gdy zadna nie ma przewagi.

    To jest poprzeczka, ktora deflowany Sharpe podstawia w miejsce zera:
    najlepsza z N konfiguracji wyjdzie dodatnia sama z siebie, i tym wyzej,
    im wieksze N. Przyblizenie Bailey'a i Lopeza de Prado (2014):

        E[max SR] ~ sd * ( (1-g) * F^-1(1 - 1/N) + g * F^-1(1 - 1/(N*e)) )

    gdzie `g` to stala Eulera-Mascheroniego, `F^-1` to odwrotna dystrybuanta
    rozkladu normalnego standardowego, a `sd` to ODCHYLENIE Sharpe'ow miedzy
    probami (pierwiastek z V[{SR_n}] u Bailey'a).

    N = 1 oddaje ZERO, a nie liczbe: przy jednej probie nie bylo z czego
    wybierac, wiec nie ma czego odejmowac. Wzor sam by tu nie przeszedl -
    `F^-1(1 - 1/1) = F^-1(0)` nie istnieje.

    None, gdy N < 1 albo odchylenie nie jest dodatnie. Zero podstawione
    w miejsce nieznanego odchylenia dawaloby poprzeczke na wysokosci zera,
    czyli zdejmowaloby cala poprawke na wielokrotne testowanie - i robiloby to
    po cichu, w strone LATWIEJSZEJ bramki.
    """
    if n_prob < 1:
        return None
    if n_prob == 1:
        return 0.0
    if odchylenie_prob <= 0:
        return None
    return odchylenie_prob * (
        (1 - GAMMA_EULERA) * _N01.inv_cdf(1 - 1 / n_prob)
        + GAMMA_EULERA * _N01.inv_cdf(1 - 1 / (n_prob * math.e)))


def liczba_niezaleznych(klucze: list) -> int:
    """Ile NIEZALEZNYCH obserwacji jest w probie, ktorej obserwacje skupiaja sie
    w grupach o wspolnym szoku (np. transakcje otwarte na tej samej swiecy).
    CZYSTA FUNKCJA. Liczba roznych kluczy.

    To jest gorna granica korelacji wewnatrz grupy (rho = 1): grupa liczy sie
    jako jedna obserwacja niezaleznie od liczebnosci. Uzasadnienie wyboru przy
    `deflowany_sharpe` (argument `n_efektywne`)."""
    return len(set(klucze))


def deflowany_sharpe(zwroty: list[float], n_prob: int,
                     odchylenie_prob: float | None = None,
                     n_efektywne: int | None = None) -> dict:
    """Deflowany Sharpe (Bailey, Lopez de Prado 2014). CZYSTA FUNKCJA.

    Odpowiada na pytanie, na ktore sam Sharpe nie odpowiada: jakie jest
    prawdopodobienstwo, ze PRAWDZIWA przewaga jest dodatnia, skoro ten wynik
    jest NAJLEPSZYM z `n_prob` przetestowanych konfiguracji i skoro zwroty nie
    sa normalne.

    Dwie poprawki naraz, obie w strone ostrozniejsza:

      1. POPRZECZKA NIE JEST ZEREM, a oczekiwanym maksimum z N prob pod
         hipoteza zerowa (`oczekiwany_maksymalny_sharpe`). Sharpe +0,10
         wybrany z jednej proby i ten sam +0,10 wybrany z jedenastu tysiecy
         to dwa rozne wyniki, a dzisiejsza bramka nie odroznia ich niczym.
      2. MIANOWNIK LICZY SKOSNOSC I KURTOZE, a nie zaklada normalnosci:

             z = (SR - SR*) * sqrt(T - 1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2)

         `g3` to skosnosc, `g4` KURTOZA SUROWA (3 dla normalnego). Przy
         ujemnej skosnosci - typowej dla strategii z krotkim ogonem zyskow
         i dlugim strat - mianownik ROSNIE, wiec DSR spada. Pominiecie tych
         dwoch momentow jest wiec zalozeniem dzialajacym na korzysc strategii.

    `odchylenie_prob` - POMIAR ALBO PODSTAWIENIE, I WYNIK MOWI KTORE

    Bailey liczy V[{SR_n}] jako rozrzut Sharpe'ow MIEDZY KONFIGURACJAMI - bo to
    on rzadzi tym, jak wysoko wypada maksimum z N prob. Zrodlem jest
    `train.odchylenie_prob_z_bazy`: `learn.learning_state_log.median_oos_sharpe`
    grupowany po DISTINCT `learn.model_version.params`, czyli po tej samej
    wielkosci, ktora jest N.

    NIE JEST TO ROZRZUT MIEDZY OKNAMI, i ta pomylka zostala tu raz popelniona
    i wdrozona. Zmierzone na produkcji 2026-09-27, tor 4h:

        miedzy OKNAMI         (learn.wf_metric, n=2353)        sd = 0,2627
        miedzy PRZEBIEGAMI    (learning_state_log, n=29)       sd = 0,0270
        miedzy KONFIGURACJAMI (DISTINCT params, n=28)          sd = 0,0266
        podstawienie z H0     (T=1500)                         sd = 0,0260

    Roznica jest dziesieciokrotna, a skutek na werdykt nie jest kosmetyczny:
    przy SR +0,0762 poprzeczka rosla z +0,062 na +0,615, a DSR spadal z 0,70
    na 0,00 - z "nie przechodzi o wlos" na "nie przechodzi o rzad wielkosci".
    Rozrzut miedzy oknami mierzy zmiennosc RYNKU okno po oknie oraz szum
    estymacji Sharpe'a z jednego okna; zaden z tych skladnikow nie jest
    rozrzutem konfiguracji. Uzasadnienie liczbowe - w `odchylenie_prob_z_bazy`.

    Pominiecie argumentu wstawia w to miejsce WARIANCJE SAMEGO ESTYMATORA pod
    hipoteza zerowa: gdy zadna proba nie ma przewagi, Sharpe'y roznia sie
    wylacznie bledem pomiaru, czyli sd = sqrt(1 - g3*SR + (g4-1)/4*SR^2) /
    sqrt(T-1). Kierunek pomylki jest znany: konfiguracje roznia sie nie tylko
    szumem, wiec PRAWDZIWY rozrzut jest WIEKSZY, poprzeczka WYZSZA, a prawdziwy
    DSR NIZSZY. SKALA jest teraz takze zmierzona i jest MALA - 0,0266 wobec
    0,0260, czyli 2,7%, a nie rzad wielkosci. Podstawienie bylo wiec droga
    optymistyczna, ale niemal o nic.

    TA SAMA LICZBA JEST PODLOGA. Rozrzut konfiguracji nie moze byc mniejszy od
    bledu estymatora, bo konfiguracje roznia sie CO NAJMNIEJ nim. Pomiar
    ponizej tej podlogi jest odrzucany na jej rzecz i `zrodlo_odchylenia` to
    mowi - inaczej niepelny pomiar ULATWIALBY bramke.

    Wynik NAZYWA swoje zrodlo w polu `zrodlo_odchylenia` i podaje podloge
    w `odchylenie_podloga_h0`. Bez tych pol liczba z pomiaru, liczba
    z podstawienia i liczba z podlogi wygladaja identycznie, a znacza trzy
    rozne rzeczy.
    Zwraca SLOWNIK ZAWSZE, z `dsr = None` i wypelnionym `powod`, gdy nie ma
    czego liczyc. Bramka ma wtedy WSTRZYMAC promocje, a nie przepuscic ja -
    patrz `train.bramka_dsr`.

    `n_efektywne` - T W PIERWIASTKU TO LICZBA NIEZALEZNYCH OBSERWACJI, NIE WIERSZY
    (audyt 2026-10-09, W-2).

    Wzor zaklada T niezaleznych zwrotow: blad standardowy Sharpe'a maleje jak
    1/sqrt(T-1). Transakcje OOS nie sa niezalezne - kilkanascie par krypto
    otwieranych na tej samej swiecy 4h niesie jeden szok rynkowy (korelacja
    par ok. 0,8, `train.py` przy `n_trades`). Z T rownym liczbie transakcji
    strategia, ktora handluje SZERZEJ, przechodzi bramke latwiej, choc w zadnym
    oknie nie jest lepsza: ta sama proba 500 transakcji powielona x2 i x3 (zero
    nowej informacji) dawala DSR 0,8875 -> 0,9949 -> 0,9998.

    Dlatego T = `n_efektywne`, gdy podane - w `train` liczba ROZNYCH SWIEC
    WEJSCIA (`liczba_niezaleznych`). Dlaczego ta miara, a nie inna:
      * powielenie proby jej nie zmienia (duplikat ma te sama swiece), wiec
        powielenie nie podnosi DSR - to jest test regresji;
      * jest gorna granica korelacji w swiecy (rho = 1). Przy rho ~ 0,8 i m
        transakcjach w swiecy prawdziwe T_eff = n / (1 + (m-1)*rho) wychodzi
        najwyzej ok. 1/rho = 1,25x wiecej - blad w strone OSTROZNIEJSZEJ bramki,
        jedynej dopuszczalnej. Estymacja rho z proby (ICC) przy kilku-
        kilkunastu transakcjach na swiece ma rozrzut wiekszy niz ta roznica,
        a pomylka w dol ULATWIALABY bramke;
      * okna testowe walk-forward sa ROZLACZNE, wiec swiece wejscia nie
        powtarzaja sie miedzy oknami (nakladanie 29/30 dotyczy okien
        TRENINGOWYCH, nie testowych);
      * NIE koryguje autokorelacji szeregowej (pozycje trzymane przez kilka
        swiec nakladaja sie w czasie). Ta zostaje i dziala w te sama strone:
        prawdziwe T jest jeszcze mniejsze.
    Sharpe, skosnosc i kurtoza zostaja liczone NA TRANSAKCJE - to ta sama
    jednostka, w ktorej mierzony jest rozrzut konfiguracji (`odchylenie_prob`)
    i w ktorej bramka ma prog. Zmienia sie wylacznie liczba obserwacji.
    """
    t_obs = len(zwroty) if n_efektywne is None else min(n_efektywne, len(zwroty))
    wynik = {"dsr": None, "sharpe": None, "prog": None, "skosnosc": None,
             "kurtoza": None, "n_obserwacji": len(zwroty), "n_prob": n_prob,
             "n_efektywne": t_obs,
             "odchylenie_prob": odchylenie_prob,
             # CZY TO POMIAR, CZY PODSTAWIENIE - NAZWANE, NIE DOMYSLNE.
             # Ustawiane na wejsciu, a nie przy uzyciu: gdyby stalo nizej, kazda
             # nowa sciezka wyjscia z tej funkcji musialaby o nim pamietac.
             "zrodlo_odchylenia": ("pomiar rozrzutu miedzy konfiguracjami"
                                   if odchylenie_prob is not None
                                   else "podstawienie: wariancja estymatora pod H0"),
             "powod": ""}
    if n_prob < 1:
        wynik["powod"] = ("licznik przetestowanych konfiguracji jest zerowy — "
                          "nie wiadomo, z ilu prób ten wynik jest maksimum")
        return wynik
    sr = sharpe(zwroty)
    if sr is None:
        wynik["powod"] = ("Sharpe ze złączonej próby nie istnieje (mniej niż "
                          "dwie transakcje albo zerowy rozrzut)")
        return wynik
    g3, g4 = skosnosc(zwroty), kurtoza(zwroty)
    if g3 is None or g4 is None:
        wynik["powod"] = "skośność i kurtoza nieobliczalne na tej próbie"
        return wynik
    wynik.update({"sharpe": sr, "skosnosc": g3, "kurtoza": g4})
    if t_obs < 2:
        wynik["powod"] = (f"za mało niezależnych obserwacji ({t_obs} różnych "
                          f"świec wejścia przy {len(zwroty)} transakcjach)")
        return wynik

    # Mianownik PSR. Moze wyjsc niedodatni przy duzym Sharpe i duzej skosnosci -
    # wtedy wzor nie ma zastosowania i oddajemy brak, a nie liczbe.
    wariancja = 1 - g3 * sr + (g4 - 1) / 4 * sr * sr
    if wariancja <= 0:
        wynik["powod"] = (f"mianownik PSR niedodatni ({wariancja:+.4f}) — wzór "
                          f"nie ma zastosowania przy tej skośności i kurtozie")
        return wynik
    sigma = math.sqrt(wariancja)

    # PODLOGA H0 - POMIAR NIE MOZE OBNIZYC POPRZECZKI PONIZEJ SZUMU ESTYMATORA.
    #
    # `sigma/sqrt(T-1)` to rozrzut Sharpe'ow, ktory powstalby, GDYBY zadna proba
    # nie miala przewagi: konfiguracje identyczne, roznice wylacznie z bledu
    # pomiaru. Prawdziwe V[SR_n] musi byc od tego WIEKSZE albo rowne, bo
    # konfiguracje roznia sie CO NAJMNIEJ szumem - to jest nierownosc, nie
    # zalozenie.
    #
    # Pomiar mniejszy od podlogi nie jest wiec wiadomoscia "konfiguracje sa
    # zgodniejsze, niz sadzilismy". Jest wiadomoscia "nasz pomiar rozrzutu jest
    # NIEPELNY" - najczesciej dlatego, ze konfiguracje oceniano na tych samych
    # danych, wiec czesc szumu sie skrocila. Uzycie go obnizyloby poprzeczke pod
    # poziom, ktory sam blad pomiaru uzasadnia, czyli ULATWILOBY bramke przez
    # niedomiar wiedzy. Bierzemy wieksza z dwoch i MOWIMY, ktora wygrala.
    #
    # Zmierzone 2026-09-27 (tor 4h): pomiar 0,0266 wobec podlogi 0,0259 - przeszedl
    # o 2,7%. Margines jest maly, a blad estymacji sd przy n=28 wynosi ~14%, wiec
    # ten warunek NIE jest formalnoscia: kolejny przebieg moze zmierzyc ponizej.
    podloga = sigma / math.sqrt(t_obs - 1)
    wynik["odchylenie_podloga_h0"] = podloga
    if odchylenie_prob is None:
        odchylenie_prob = podloga
        wynik["odchylenie_prob"] = odchylenie_prob
    elif odchylenie_prob < podloga:
        wynik["zrodlo_odchylenia"] = (
            f"podloga H0: zmierzony rozrzut {odchylenie_prob:.4f} wyszedl PONIZEJ "
            f"samego bledu estymatora {podloga:.4f}, wiec pomiar jest niepelny")
        odchylenie_prob = podloga
        wynik["odchylenie_prob"] = odchylenie_prob
    prog = oczekiwany_maksymalny_sharpe(n_prob, odchylenie_prob)
    if prog is None:
        wynik["powod"] = ("rozrzut Sharpe'ów między próbami nie jest dodatni — "
                          "poprzeczki na wielokrotne testowanie nie ma z czego "
                          "policzyć")
        return wynik
    wynik["prog"] = prog
    wynik["dsr"] = _N01.cdf((sr - prog) * math.sqrt(t_obs - 1) / sigma)
    return wynik

def sharpe_dla_dsr(zwroty: list[float], n_prob: int, cel: float,
                   odchylenie_prob: float | None = None,
                   n_efektywne: int | None = None) -> float | None:
    """Jaki Sharpe/transakcje dalby DSR rowny `cel` na TEJ SAMEJ probie.

    PO CO. Sam werdykt "DSR 0,9524 przy progu 0,95" nie mowi, jak blisko progu
    stoi - a przy tej strategii stoi TAK blisko, ze przesuniecie okna o jeden
    dzien przewraca go w obie strony. Zmierzone 2026-09-27, tor 4h, piec
    przesuniec `--days` 1400..1396:

        --days  status      DSR    Sharpe   okien dodatnich
          1400  validated  0,9524  +0,1016   27/38 (71%)
          1399  validated  0,9732  +0,1078   28/38 (74%)
          1398  learning   0,9342  +0,0975   31/40 (78%)
          1397  learning   0,9362  +0,0978   28/39 (72%)
          1396  validated  0,9790  +0,1095   29/39 (74%)

    Trzy razy `validated`, dwa razy `learning`, poprzeczka selekcji IDENTYCZNA
    (+0,0611) we wszystkich pieciu - cala zmiana siedzi w Sharpe'ie, ktory chodzi
    w przedziale 0,0120, czyli sd = 0,0056. To **0,22 bledu standardowego**
    samego Sharpe'a (1/sqrt(1500) = 0,0258). Bramka rozstrzyga wiec roznice
    PIEC RAZY DROBNIEJSZE od szumu wlasnego pomiaru, a jej wyjscie jest binarne
    i czyta sie jak fakt o strategii. Zauwaz tez odwrotnosc: przesuniecie 1398
    ma NAJLEPSZY odsetek okien (78%) i mimo to nie przechodzi na DSR.

    JAK TO LICZY, I DLACZEGO NIE DRUGI RAZ TYM SAMYM WZOREM. Przesuwamy cala
    probe o stala `delta`: zmienia to WYLACZNIE srednia, bo odchylenie, skosnosc
    i kurtoza sa momentami CENTRALNYMI, a `T` i poprzeczka selekcji nie zaleza
    od wartosci zwrotow. Bisekcja po `delta` wola `deflowany_sharpe`, wiec nie
    ma tu drugiej kopii wzoru Baileya - a kopia wlasnie tutaj byla by najgorsza,
    bo klamalaby o odleglosci od progu, ktory sama liczy.

    Oddaje None, gdy DSR nie da sie policzyc na tej probie (za malo obserwacji,
    zerowe odchylenie, nieobliczalna skosnosc) - te same przypadki, w ktorych
    `deflowany_sharpe` oddaje `dsr = None`.
    """
    baza = deflowany_sharpe(zwroty, n_prob, odchylenie_prob, n_efektywne)
    if baza["dsr"] is None or baza["sharpe"] is None:
        return None
    sd = statistics.stdev(zwroty)
    if sd <= 0:
        return None

    def dsr_po(delta: float) -> float | None:
        return deflowany_sharpe([z + delta for z in zwroty], n_prob,
                                odchylenie_prob, n_efektywne)["dsr"]

    # Widelki: szukamy delty, przy ktorej DSR przechodzi przez `cel`. Rozszerzamy
    # je, dopoki cel nie znajdzie sie miedzy koncami - zamiast zgadywac zakres.
    lo, hi = -2.0 * sd, 2.0 * sd
    for _ in range(60):
        d_lo, d_hi = dsr_po(lo), dsr_po(hi)
        if d_lo is None or d_hi is None:
            return None
        if d_lo <= cel <= d_hi:
            break
        lo, hi = lo * 2, hi * 2
    else:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        d = dsr_po(mid)
        if d is None:
            return None
        if d < cel:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12 * max(1.0, abs(hi)):
            break
    srodek = (lo + hi) / 2
    return baza["sharpe"] + srodek / sd


# Ile konfiguracji z policzonym Sharpe'em OOS musi byc w bazie, zeby ich rozrzut
# WOLNO bylo podstawic pod V[{SR_n}] Baileya. Nizej zostaje podstawienie
# z hipotezy zerowej, opisane jako podstawienie.
#
# DLACZEGO PROG, A NIE "byle cokolwiek zmierzone". Odchylenie probkowe z trzech
# liczb ma samo rozrzut rzedu 40% swojej wartosci, a wchodzi do poprzeczki
# selekcji liniowo. Zamiana podstawienia na taki "pomiar" nie domyka luki,
# tylko przenosi ja w miejsce, w ktorym nikt jej nie szuka: DSR wygladalby
# wtedy na policzony z danych i skakalby o kilkadziesiat procent miedzy
# przebiegami roznjacymi sie jednym oknem.
#
# 20 to ta sama skala, przy ktorej `spearman` zaczyna cokolwiek liczyc (30)
# i przy ktorej `t95` przestaje rosnac gwaltownie. Przy 40 oknach na przebieg
# jeden przebieg `argmax` zapelnia ten prog w calosci.
MIN_PROB_ROZRZUTU = 20


def rozrzut_sharpeow(sharpes: list[float]) -> float | None:
    """Odchylenie Sharpe'ow MIEDZY konfiguracjami - V[{SR_n}] Baileya, pierwiastek.

    None, gdy proba nie siega `MIN_PROB_ROZRZUTU` albo rozrzutu nie ma. None
    znaczy "nie zmierzono" i wolajacy MUSI wtedy zostac przy podstawieniu -
    a nie podstawic zera, ktore zdjeloby cala poprawke na wielokrotne testowanie.

    ODCHYLENIE PROBKOWE (n-1), jak w `sharpe`: zbior konfiguracji, ktore ktos
    policzyl, jest probka z konfiguracji, ktore mogl policzyc.
    """
    if len(sharpes) < MIN_PROB_ROZRZUTU:
        return None
    sd = statistics.stdev(sharpes)
    return sd if sd >= MIN_ROZRZUT else None
