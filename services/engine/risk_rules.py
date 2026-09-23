"""Czyste reguly limitow ryzyka - bez bazy danych.

DLACZEGO OSOBNY MODUL

Symulacja walk-forward musi stosowac DOKLADNIE te same limity co produkcja,
inaczej mierzy strategie, ktorej system nigdy nie wykona. Backtest, ktory
pokazuje obsuniecie 37% przy skonfigurowanym kill-switchu na 10%, nie opisuje
niczego, co moglo sie wydarzyc.

Napisanie drugiej implementacji limitow "na potrzeby symulacji" byloby
gwarancja rozjazdu przy pierwszej zmianie regul - to ten sam blad, przed ktorym
broni `feat.as_of()` po stronie cech. Dlatego reguly zyja tutaj, jako funkcja
czysta operujaca na przekazanym STANIE, a oba tory tylko dostarczaja ten stan:

  - RiskKernel        -> stan czyta z bazy (pozycje, kapital, zlecenia)
  - symulacja portfela -> stan trzyma w pamieci

Kolejnosc kontroli jest istotna i zachowana: najpierw to, co zatrzymuje CALY
silnik (kill-switch, strata dzienna, obsuniecie), potem to, co dotyczy
pojedynczego zlecenia (czestotliwosc, ekspozycja, wielkosc). Inaczej system
moglby zmniejszyc zlecenie i wyslac je mimo aktywnego kill-switcha.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class RiskState:
    """Stan konta w chwili decyzji. Zrodlo (baza / pamiec) nie ma znaczenia.

    DWA POJECIA KAPITALU, i to jest cala tresc tej klasy.

    `equity` to PIENIADZE na rachunku - rosna od wplat, spadaja od wyplat. Nimi
    mierzy sie wielkosc pozycji i prog kapitalu, bo one dotycza pieniedzy.

    `nav` to wartosc jednostki uczestnictwa: startuje od 1.0 i zmienia sie
    WYLACZNIE od wyniku handlu. Nia mierzy sie obsuniecie i strate dzienna,
    bo one dotycza umiejetnosci.

    Do 2026-09-07 istnialo tylko pierwsze. Wplata wygladala wiec jak zysk,
    a wyplata jak strata - i obsuniecie liczylo sie od szczytu, ktory podniosl
    przelew, a nie handel. Na rachunku papierowym roznicy nie bylo, bo przeplyw
    jest jeden. Przy realnych srodkach jest to blad w kazdym limicie naraz.
    """
    equity: Decimal
    peak: Decimal
    day_ago: Decimal
    # EKSPOZYCJA: `None` ZNACZY „NIE ZMIERZONA", A NIE „ZERO".
    #
    # Czyta ja wylacznie `evaluate_limits`, czyli sciezka POJEDYNCZEGO ZLECENIA.
    # `evaluate_account` - ta, ktora wola dozorca ryzyka co 60 s - nie dotyka
    # ani `exposure`, ani `per_instrument` w zadnej ze swoich czterech regul.
    # Wolajacy, ktory tej wielkosci nie mierzy, ma prawo jej nie podac.
    #
    # DLACZEGO NIE `Decimal(0)` JAKO DOMYSL. Zero jest poprawna zmierzona
    # wartoscia (rachunek bez pozycji i bez zlecen), wiec nie da sie go odroznic
    # od braku pomiaru. Limit `max_total_exposure` przy niezmierzonej ekspozycji
    # podanej jako zero przepuszczalby KAZDE zlecenie i wygladalby przy tym
    # dokladnie tak, jak limit dzialajacy. `None` w tym miejscu zamienia ten
    # cichy blad w `ValueError` w pierwszej sekundzie - patrz `evaluate_limits`.
    exposure: Decimal | None
    per_instrument: dict[int, Decimal] | None
    orders_last_hour: int
    killswitch: dict | None = None
    # Wartosc jednostki: biezaca, szczytowa i sprzed doby. Domysl None znaczy
    # "brak przeplywow" - wtedy nav jest proporcjonalne do equity i reguly
    # spadaja z powrotem na equity. Dzieki temu symulacja walk-forward, ktora
    # przeplywow nie zna, nie musi o nich wiedziec.
    nav: Decimal | None = None
    nav_peak: Decimal | None = None
    nav_day_ago: Decimal | None = None
    # Suma wplat pomniejszona o wyplaty. Odniesienie dla progu kapitalu
    # wyrazonego ulamkiem (`pct_wplat`).
    wplaty_netto: Decimal | None = None
    # Czy krzywa `nav` siega POCZATKU rachunku, czy tylko chwili, w ktorej
    # kolumna zaczela byc wypelniana. Patrz `_nav_wiarygodne` - to jest cala
    # tresc tego pola.
    nav_od_poczatku: bool = True

    # --- widoki uzywane przez reguly -------------------------------------
    #
    # Reguly pytaja o "wynik", a nie o "kapital". Te trzy wlasnosci decyduja
    # RAZ, ktora liczba jest wynikiem - zamiast powtarzac ten wybor przy kazdym
    # limicie, gdzie predzej czy pozniej jeden z nich zostalby pominiety.
    @property
    def _nav_wiarygodne(self) -> bool:
        """Czy wolno mierzyc wynik jednostka. Decyzja jest JEDNA dla calej trojki.

        DWA POWODY, DLA KTORYCH TO NIE JEST TRZYKROTNE `is not None`.

        PIERWSZY: JEDNOSTKI. `nav` krazy wokol 1.0, `equity` wokol 100 000.
        Gdyby kazda wlasnosc spadala na kapital osobno, stan z `nav` bez
        `nav_peak` dalby obsuniecie (100000 - 1) / 100000 = 99,999% - czyli
        kill-switch z arytmetyki, nie z rynku. Albo cala trojka jest w
        jednostkach, albo cala w pieniadzach; trzeciej mozliwosci nie ma.

        DRUGI, I TO JEST POWOD ISTNIENIA `nav_od_poczatku`: SZCZYT Z OKROJONEJ
        HISTORII. Kolumna `nav` powstala w migracji 0031 i nie zostala
        uzupelniona wstecz, wiec `max(nav)` bylo szczytem liczonym od dnia
        migracji, a nie od poczatku rachunku. Na produkcji 2026-09-09 dawalo to
        obsuniecie 0,0101% zamiast rzeczywistych 0,0429% - zaniżenie 4,2x przy
        86% krzywej niewidocznej dla tej miary.

        Kierunek bledu jest JEDNOSTRONNY i to czyni go grozniejszym niz sama
        skala: obsuniecie liczone od pozniejszego szczytu jest zawsze mniejsze
        albo rowne prawdziwemu, nigdy wieksze. Gdyby `nav` zaczelo sie zapisywac
        w dolku (nowy tryb, purge_demo.py, migracja przerwana w polowie),
        rachunek juz 9% pod woda dostalby do dyspozycji kolejne 10% - lacznie
        19% przy progu 10%, bez jednego bledu w dzienniku.

        Dlatego brak pelnej historii cofa pomiar na `equity`, ktorego szczyt
        liczy sie z CALEJ krzywej. Kapital rosnie od wplat, wiec ten szczyt bywa
        zawyzony - i to jest wlasnie kierunek, ktory chcemy: nie wiedzac, gdzie
        byl szczyt, zatrzymujemy sie za wczesnie, a nie za pozno.
        """
        return (self.nav is not None and self.nav_peak is not None
                and self.nav_day_ago is not None and self.nav_od_poczatku)

    @property
    def wynik_w_jednostkach(self) -> bool:
        """Publiczna odpowiedz na "ktora miara wygrala".

        Istnieje po to, zeby drugi czytelnik krzywej (`wznowienie.py` przez
        `stan_kapitalu.zloz_stan`) mogl NAZWAC jednostke w dzienniku zwolnienia,
        nie siegajac po pole prywatne i nie odtwarzajac tego warunku u siebie.
        Odtworzony warunek jest dokladnie tym, co rozjechalo bezpiecznik
        z dozorca po migracji 0031.
        """
        return self._nav_wiarygodne

    @property
    def wynik(self) -> Decimal:
        return self.nav if self._nav_wiarygodne else self.equity

    @property
    def wynik_szczyt(self) -> Decimal:
        return self.nav_peak if self._nav_wiarygodne else self.peak

    @property
    def wynik_doba(self) -> Decimal:
        return self.nav_day_ago if self._nav_wiarygodne else self.day_ago


@dataclass
class RiskVerdict:
    verdict: str                      # 'allow' | 'downsize' | 'block'
    reason: str | None = None
    qty: Decimal | None = None        # wielkosc po ewentualnym obcieciu
    violations: list[dict] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)
    # Powod uruchomienia kill-switcha, gotowy do pokazania czlowiekowi.
    trigger_killswitch: str | None = None
    # NAZWA limitu, ktory go zazadal - osobno od powodu.
    #
    # Powod jest zdaniem po polsku i zmienia sie razem z liczbami. Regula
    # wznowienia musi wiedziec, KTORY limit zadzialal, bo trzy limity krytyczne
    # znacza trzy rozne rzeczy: strata dzienna mija razem z doba, obsuniecie
    # wymaga odbudowy kapitalu, a prog kapitalu jest granica, ktorej system nie
    # przekracza sam. Wyciaganie tego z tekstu powodu dzialaloby do pierwszej
    # zmiany sformulowania.
    trigger_limit: str | None = None


class LimitNieobliczalny(RuntimeError):
    """Limit zapisany procentowo, a podstawy procentu NIE ZNAMY.

    Nie jest to ani zero, ani inna jednostka - jest to brak pomiaru. Limit,
    ktorego nie da sie policzyc, nie moze zamilknac: jedyna bezpieczna reakcja
    to wstrzymanie handlu, bo alternatywa jest handel bez dzialajacej podlogi
    kapitalu przy zielonym statusie.
    """


def wartosc_limitu(lim: dict, kapital: Decimal) -> Decimal:
    """Limit w walucie, niezaleznie od tego, jak zostal zapisany.

    DLACZEGO TO ISTNIEJE

    Trzy limity byly zapisane jako KWOTY wyliczone dla rachunku 100 000 USD:
    pozycja 5 000, laczna ekspozycja 15 000, budzet 1 500. Dopoki rachunek ma
    100 000, jest to 5%, 15% i 1,5% - dokladnie to, na czym mierzono wynik.

    Na rachunku 10 000 te same kwoty znacza 50%, 150% i 15%. Limit lacznej
    ekspozycji przestaje wtedy cokolwiek ograniczac, bo na spocie bez dzwigni
    nie da sie przekroczyc 100% rachunku - a caly profil ryzyka rosnie
    dziesieciokrotnie wzgledem zmierzonego.

    Nie objawiloby sie to awaria. System handlowalby dalej, raportowal zielone
    statusy i ryzykowal dziesieciokrotnie wiecej, niz wynika z pomiaru.

    `pct_equity` wiaze limit z kapitalem, wiec zmiana wplaty nie zmienia profilu
    ryzyka. `quote_ccy` zostaje, bo limit wyrazony kwotowo bywa tym, o co chodzi
    (np. minimum gieldowe) - ale przestaje byc domyslnym sposobem zapisu.
    """
    if lim.get("unit") == "pct_equity":
        return Decimal(lim["value"]) / Decimal(100) * kapital
    return Decimal(lim["value"])


def prog_min_equity(lim: dict, state: RiskState) -> Decimal:
    """Podloga kapitalu w walucie. JEDNO miejsce, bo czyta ja pomiar I regula.

    NIGDY PROCENTOWO OD SZCZYTU. Prog wyrazony jako procent szczytu jest
    DOKLADNIE limitem obsuniecia (`max_drawdown`) - czyli duplikatem, ktory
    wyglada na dodatkowe zabezpieczenie i nim nie jest.

    Dopuszczalne sa dwie postacie. Kwotowa jest prosta, ale przestaje dzialac po
    wyplacie: rachunek spada zgodnie z wola wlasciciela, a prog zostaje w miejscu
    i zatrzymuje silnik bez powodu. `pct_wplat` przesuwa sie razem z wplatami
    netto i wyraza to, o co chodzi: "nie strac wiecej niz tyle z tego, co
    wlozylem".

    BRAK PODSTAWY PROCENTU JEST BLEDEM, A NIE INNA JEDNOSTKA (naprawa
    2026-09-23)

    Bylo tu `if unit == "pct_wplat" and state.wplaty_netto is not None`, czyli
    przy `wplaty_netto is None` limit `{"value": 95, "unit": "pct_wplat"}`
    schodzil po cichu na ostatnia linijke i stawal sie progiem 95 JEDNOSTEK
    WALUTY zamiast 95% wplat. Na rachunku 10 000 warunek `state.equity <= prog`
    nie zostalby spelniony NIGDY - kill-switch chroniacy pieniadze wlasciciela
    bylby wylaczony, przy zielonym statusie i pelnym wierszu w
    `risk.limit_usage` z threshold 95.

    Jest to ten sam blad, ktory opisuje `wartosc_limitu` kilkanascie linii
    wyzej („`lim['value']` przy jednostce `pct_equity` to LICZBA PROCENTOWA
    (15), a `used` jest kwota") - niepoprawiony w drugim miejscu.

    `wplaty_netto` pochodzi z `SQL_NAV_BIEZ` przez `risk._wynik()` i jest
    `None` wszedzie tam, gdzie najswiezszy punkt z niepustym `nav` ma pusta
    kolumne `wplaty_netto` - czyli w stanach po migracji lub czyszczeniu, dla
    ktorych 0042 swiadomie zostawia NULL-e. Ta sama sciezka obowiazuje dozorce
    przez `stan_kapitalu.stan_ryzyka`, wiec niezalezny nadzorca milczalby
    tak samo.

    Dzis jest to latentne: produkcja ma `min_equity` jako `quote_ccy` 95000
    (migracja 0028). Ale `pct_wplat` jest tym, co docstring wyzej zaleca dla
    rachunku zywego, i jest juz uzywane w czterech testach - pulapka czeka
    dokladnie na przejscie na live.

    DLACZEGO WYJATEK, A NIE WARTOSC ZASTEPCZA. Kazda liczba zwrocona z tego
    miejsca zostanie porownana z kapitalem i przesadzi o tym, czy silnik
    handluje dalej. Nie ma liczby, ktora znaczy „nie wiem": zero wylacza
    podloge, a kapital biezacy zatrzymalby handel natychmiast i bez powodu.
    Wolajacy ma sie o tym dowiedziec i zdecydowac sam - robi to
    `pomiar_konta` razem z `evaluate_account`, wstrzymujac handel.
    """
    if lim.get("unit") == "pct_wplat":
        if state.wplaty_netto is None:
            raise LimitNieobliczalny(
                "min_equity jest zapisany jako procent wpłat "
                f"({lim['value']}% pct_wplat), a wpłat netto nie znamy "
                "(exec.equity_point.wplaty_netto jest NULL) — progu nie da się "
                "policzyć, a zejście na jednostkę kwotową zamieniłoby go "
                f"po cichu w próg {lim['value']} jednostek waluty")
        return Decimal(lim["value"]) / Decimal(100) * state.wplaty_netto
    return Decimal(lim["value"])


def pomiar_konta(state: RiskState,
                 limits: dict[str, dict]) -> tuple[list[dict], dict]:
    """Zuzycie KAZDEGO limitu konta - policzone ZANIM zapadnie jakikolwiek werdykt.

    DLACZEGO POMIAR JEST ODDZIELONY OD OCENY

    Do 2026-09-12 obie rzeczy robila jedna petla warunkow w `evaluate_account`:
    kazdy limit dopisywal swoj pomiar tuz przed wlasnym `return`. Wygladalo to
    poprawnie i mialo skutek, ktorego nikt nie zaplanowal - POMIAR ZNIKAL
    DOKLADNIE WTEDY, GDY BYL NAJPOTRZEBNIEJSZY:

      * przy aktywnym kill-switchu funkcja wracala w punkcie 1, PRZED
        pierwszym pomiarem. Zatrzymane konto nie produkowalo wiec ani jednego
        wiersza w `risk.limit_usage` - a to jest stan, w ktorym pytanie "czy
        bezpiecznik w ogole liczy" jest jedynym, jakie zostaje. Zmierzone na
        produkcji 2026-09-12: kill-switch aktywny od 2026-09-09 03:20, ostatni
        zapisany pomiar sprzed 9629 minut, `min_equity` BEZ ANI JEDNEGO pomiaru
        w calej historii;
      * przy naruszeniu wczesniejszego limitu ginely pomiary wszystkich
        pozniejszych. Naruszona strata dzienna kasowala pomiar `min_equity`
        i `max_drawdown`, czyli dokladnie w chwili klopotu dwa z trzech filarow
        podlogi kapitalu przestawaly zostawiac slad.

    Oba objawy to ta sama wada: NADZOR STAJE SIE NIEWIDOCZNY W CHWILI, W KTOREJ
    JEST NAJBARDZIEJ POTRZEBNY. Ten sam ksztalt, co tlumienie powiadomien
    spelnione raz na zawsze i co zawieszony worker meldujacy `Running 1/1`.

    Pomiar jest wiec teraz bezwarunkowy, a krotkie spiecie ocen zostaje - to
    dwie rozne rzeczy i nie moga dzielic jednego `return`.

    CO NADAL MOZE ZOSTAC NIEZMIERZONE, I DLACZEGO TO NIE JEST TA SAMA WADA

    Warunki `wynik_doba > 0` i `wynik_szczyt > 0` zostaja, bo mowia o BRAKU
    DANYCH, a nie o werdykcie: bez odniesienia sprzed doby nie ma czego dzielic.
    Rozroznia je czujka - limit bez ani jednego pomiaru w historii jest dla niej
    alarmem tak samo jak pomiar przeterminowany (patrz `dozor.ocen_cisze_pomiaru`).

    ZWRACA DWIE RZECZY, BO SLUZA DWOM ROZNYM ODBIORCOM

    `usage` idzie DO BAZY: `float` i `used` obciete do zera, bo `utilization`
    ujemne czytaloby sie jak blad zapisu, a nie jak rachunek z zyskiem.

    `surowe` idzie DO REGULY i niesie liczbe, ktora regula naprawde porownuje:
    nieobciety odsetek straty i obsuniecia (te moga byc ujemne) oraz podloge
    kapitalu jako `Decimal`. Przekazanie regule liczby z `usage` byloby cicha
    zmiana warunku bezpieczenstwa - obcinanie i konwersja na `float` nalezy do
    zapisu, nie do oceny.
    """
    usage: list[dict] = []
    surowe: dict = {}

    lim = limits.get("max_daily_loss")
    if lim and state.wynik_doba > 0:
        # Z WYNIKU, nie z kapitalu: wyplata obniza kapital zgodnie z wola
        # wlasciciela i nie jest strata dzienna.
        loss_pct = float((state.wynik_doba - state.wynik) / state.wynik_doba * 100)
        surowe["max_daily_loss"] = loss_pct
        usage.append({"name": "max_daily_loss", "used": max(0.0, loss_pct),
                      "threshold": float(lim["value"])})

    lim = limits.get("min_equity")
    if lim:
        # NIEOBLICZALNY PROG ZOSTAJE ZAPISANY JAKO BRAK POMIARU, a nie jako
        # pomiar z podstawiona liczba. Wiersz w `risk.limit_usage` z threshold
        # 95 przy progu, ktorego nie da sie policzyc, jest gorszy niz brak
        # wiersza: wyglada na dzialajacy bezpiecznik. Pozostale limity mierzymy
        # dalej - "nadzor staje sie niewidoczny w chwili, w ktorej jest
        # najbardziej potrzebny" to wada, ktora ta funkcja juz raz naprawiala.
        try:
            prog = prog_min_equity(lim, state)
        except LimitNieobliczalny as e:
            surowe["min_equity_blad"] = str(e)
        else:
            surowe["min_equity"] = prog
            usage.append({"name": "min_equity", "used": float(state.equity),
                          "threshold": float(prog)})

    lim = limits.get("max_drawdown")
    if lim and state.wynik_szczyt > 0:
        # Z WYNIKU, nie z kapitalu: wplata podnosi kapital i szczyt, a nie jest
        # zyskiem - obsuniecie liczone od takiego szczytu mierzyloby przelew.
        dd_pct = float((state.wynik_szczyt - state.wynik) / state.wynik_szczyt * 100)
        surowe["max_drawdown"] = dd_pct
        usage.append({"name": "max_drawdown", "used": max(0.0, dd_pct),
                      "threshold": float(lim["value"])})

    return usage, surowe


def evaluate_account(state: RiskState, limits: dict[str, dict]) -> RiskVerdict:
    """Limity dotyczace CALEGO KONTA - kill-switch, strata dzienna, obsuniecie.

    Wydzielone z evaluate_limits, bo musza byc sprawdzane W KAZDYM CYKLU, a nie
    tylko przy probie wejscia w pozycje.

    Powod jest konkretny i byl realna luka: silnik wolal risk.check() wylacznie
    dla akcji zaczynajacych sie od "enter". Jesli trzymal otwarte pozycje i nie
    probowal wchodzic w nowe - a wlasnie tak zachowuje sie podczas gwaltownego
    spadku, bo sygnaly sa niespojne - to obsuniecie kapitalu NIE BYLO PRZEZ
    NIKOGO OCENIANE. Kill-switch na 10% nie mial szans zadzialac dokladnie w tej
    sytuacji, dla ktorej istnieje.

    Wykryte przez test symulacji portfelowej, ktory nie potrafil doprowadzic do
    uruchomienia kill-switcha mimo straty przekraczajacej prog.

    KAZDE WYJSCIE Z TEJ FUNKCJI NIESIE PELNY POMIAR. Liczby pochodza z
    `pomiar_konta` i sa tam policzone RAZ - uzasadnienie i historia wady
    w docstringu tamtej funkcji. Werdykt nadal zwiera na pierwszym naruszeniu;
    zwiera sie tylko OCENA, nie pomiar.
    """
    violations: list[dict] = []
    usage, surowe = pomiar_konta(state, limits)

    # 1. Kill-switch. Nic dalej nie ma znaczenia DLA WERDYKTU - ale pomiar idzie
    #    dalej razem z nim, bo zatrzymane konto jest jedynym, przy ktorym pytanie
    #    "czy bezpiecznik nadal liczy" nie ma innej odpowiedzi niz ten wiersz.
    if state.killswitch:
        ts = state.killswitch.get("triggered_at")
        kiedy = f" od {ts:%Y-%m-%d %H:%M}" if ts else ""
        return RiskVerdict(
            "block",
            f"Kill-switch aktywny{kiedy}: {state.killswitch.get('reason')}",
            usage=usage)

    # 2. Strata dzienna.
    lim = limits.get("max_daily_loss")
    if lim and "max_daily_loss" in surowe:
        loss_pct = surowe["max_daily_loss"]
        if loss_pct >= float(lim["value"]):
            violations.append({"limit": "max_daily_loss", "observed": loss_pct,
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block", f"Strata dzienna {loss_pct:.2f}% osiągnęła limit {lim['value']}%",
                violations=violations, usage=usage,
                trigger_killswitch=(f"Strata dzienna {loss_pct:.2f}% przekroczyła limit "
                                    f"{lim['value']}%") if lim["action"] == "killswitch" else None,
                trigger_limit="max_daily_loss" if lim["action"] == "killswitch" else None)

    # 3. PROG KAPITALU - twarda podloga w walucie, nie w procentach od szczytu.
    #
    # DLACZEGO OSOBNO OD max_drawdown
    #
    # Obsuniecie liczy sie od SZCZYTU, a szczyt rosnie razem z rachunkiem. Przy
    # progu 10% podloga to 0,9 x szczyt, wiec po wzroscie kapitalu przesuwa sie
    # w gore - i to jest dobre. Ale na samym poczatku, gdy szczyt rowna sie
    # wplacie, ten sam prog dopuszcza strate 10% wplaconych pieniedzy, zanim
    # cokolwiek zadziala.
    #
    # Wlasciciel wplacajacy 10 000 USD nie mysli w kategoriach "10% od szczytu",
    # tylko "ile z moich pieniedzy moze zniknac". To sa dwa rozne pytania i
    # potrzebuja dwoch roznych limitow. Ten wyraza drugie z nich wprost, w tych
    # samych jednostkach, w ktorych wplacil.
    #
    # CZEGO TEN LIMIT NIE ROBI: nie jest zleceniem stop-loss. Sprawdza sie raz
    # na cykl decyzyjny, wiec miedzy sprawdzeniami cena moze zejsc nizej.
    # Ogranicza to, ILE SYSTEM ZARYZYKUJE DALEJ, a nie to, ile juz stracil.
    lim = limits.get("min_equity")
    if lim:
        if "min_equity_blad" in surowe:
            # HANDEL WSTRZYMANY, BO PODLOGI KAPITALU NIE MA.
            #
            # `block` zatrzymuje NOWE wejscia i zostawia wyjscia - dokladnie
            # ten stan, ktorego potrzebujemy: nie wchodzimy bez dzialajacego
            # bezpiecznika, ale nie zamykamy sie tez w pozycjach, ktore juz
            # mamy. Kill-switcha NIE uruchamiamy: on wymaga recznego zwolnienia
            # i jest odpowiedzia na strate, a tu nie wiemy nawet, czy strata
            # jest - brakuje jednej kolumny w `exec.equity_point`.
            #
            # W `threshold` idzie LICZBA Z LIMITU, a nie prog w walucie -
            # progu w walucie nie ma, o to wlasnie chodzi. `risk.violation`
            # ma te kolumne NOT NULL (migracja 0007), a `detail` niesie cale
            # `v`, wiec powod i jednostka zostaja w wierszu czytelne.
            violations.append({"limit": "min_equity",
                               "observed": float(state.equity),
                               "threshold": float(lim["value"]),
                               "jednostka": lim.get("unit"),
                               "powod": surowe["min_equity_blad"],
                               "action": lim["action"]})
            return RiskVerdict(
                "block",
                f"Próg kapitału nieobliczalny — handel wstrzymany: "
                f"{surowe['min_equity_blad']}",
                violations=violations, usage=usage)
        prog = surowe["min_equity"]
        if state.equity <= prog:
            violations.append({"limit": "min_equity", "observed": float(state.equity),
                               "threshold": float(prog), "action": lim["action"]})
            return RiskVerdict(
                "block", f"Kapitał {state.equity:.2f} spadł do progu {prog:.2f}",
                violations=violations, usage=usage,
                trigger_killswitch=(f"Kapitał {state.equity:.2f} spadł do progu "
                                    f"{prog:.2f}") if lim["action"] == "killswitch" else None,
                trigger_limit="min_equity" if lim["action"] == "killswitch" else None)

    # 4. Obsuniecie od szczytu.
    lim = limits.get("max_drawdown")
    if lim and "max_drawdown" in surowe:
        dd_pct = surowe["max_drawdown"]
        if dd_pct >= float(lim["value"]):
            violations.append({"limit": "max_drawdown", "observed": dd_pct,
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block", f"Obsunięcie {dd_pct:.2f}% osiągnęło limit {lim['value']}%",
                violations=violations, usage=usage,
                trigger_killswitch=(f"Obsunięcie kapitału {dd_pct:.2f}% przekroczyło limit "
                                    f"{lim['value']}%") if lim["action"] == "killswitch" else None,
                trigger_limit="max_drawdown" if lim["action"] == "killswitch" else None)

    return RiskVerdict("allow", usage=usage)


def evaluate_limits(state: RiskState, limits: dict[str, dict],
                    instrument_id: int, notional: Decimal,
                    qty: Decimal) -> RiskVerdict:
    """Pelna ocena zlecenia: najpierw limity konta, potem limity zlecenia."""
    konto = evaluate_account(state, limits)
    if konto.verdict != "allow":
        return konto
    violations: list[dict] = list(konto.violations)
    usage: list[dict] = list(konto.usage)

    # 4. Czestotliwosc zlecen - ochrona przed petla bledu.
    lim = limits.get("max_orders_per_hour")
    if lim:
        n = state.orders_last_hour
        usage.append({"name": "max_orders_per_hour", "used": float(n),
                      "threshold": float(lim["value"])})
        if n >= float(lim["value"]):
            violations.append({"limit": "max_orders_per_hour", "observed": float(n),
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block", f"Wykonano {n} zleceń w ostatniej godzinie przy limicie {lim['value']}",
                violations=violations, usage=usage)

    # EKSPOZYCJA MUSI BYC ZMIERZONA, ZANIM ZACZNIEMY JA OCENIAC.
    #
    # Od tego miejsca w dol dwie reguly czytaja `exposure` i `per_instrument`.
    # Stan zlozony przez wolajacego, ktory tych wielkosci nie liczy (dozorca
    # ryzyka, `stan_kapitalu.stan_ryzyka`), nie nadaje sie do oceny ZLECENIA -
    # i ma sie o tym dowiedziec glosno, a nie przez limit, ktory przepuszcza
    # wszystko. Ten warunek nie moze zapalic sie na produkcji: `RiskKernel.check`
    # i symulacja portfelowa mierza obie wielkosci przed wolaniem.
    if state.exposure is None or state.per_instrument is None:
        raise ValueError(
            "evaluate_limits dostalo stan bez zmierzonej ekspozycji - ocena "
            "zlecenia na takim stanie przepuscilaby kazde wejscie")

    # 5. Laczna ekspozycja.
    lim = limits.get("max_total_exposure")
    if lim:
        after = state.exposure + notional
        # PROG W PIENIADZACH, nie w zapisie limitu - tak samo jak nizej przy
        # `max_position_notional`, ktory robil to poprawnie od poczatku.
        #
        # `lim["value"]` przy jednostce `pct_equity` to LICZBA PROCENTOWA (15),
        # a `used` jest kwota. Slad zostawiany w risk.limit_usage mowil wiec
        # "zuzyto 1350 z 15", czyli wykorzystanie 9000% - i tak samo liczyl to
        # wiersz w risk.violation oraz zdanie pokazywane czlowiekowi. Limit,
        # ktorego wykorzystania nie da sie odczytac, jest limitem niewidocznym,
        # a ten wlasnie przestal byc martwy i zaczal blokowac wejscia.
        cap = wartosc_limitu(lim, state.equity)
        usage.append({"name": "max_total_exposure", "used": float(state.exposure),
                      "threshold": float(cap)})
        if after > cap:
            violations.append({"limit": "max_total_exposure", "observed": float(after),
                               "threshold": float(cap), "action": lim["action"]})
            return RiskVerdict(
                "block",
                f"Ekspozycja po wejściu wyniosłaby {after:.0f} przy limicie {cap:.0f}",
                violations=violations, usage=usage)

    # 6. Wielkosc pojedynczej pozycji - tutaj obcinamy zamiast blokowac.
    lim = limits.get("max_position_notional")
    if lim:
        cap = wartosc_limitu(lim, state.equity)
        current = state.per_instrument.get(instrument_id, Decimal(0))
        usage.append({"name": "max_position_notional", "used": float(current),
                      "threshold": float(cap)})
        if current + notional > cap:
            allowed = cap - current
            if allowed <= 0:
                violations.append({"limit": "max_position_notional", "observed": float(current),
                                   "threshold": float(cap), "action": "block"})
                return RiskVerdict("block",
                                   f"Pozycja na tym instrumencie już wypełnia limit {cap:.0f}",
                                   violations=violations, usage=usage)
            factor = allowed / notional
            violations.append({"limit": "max_position_notional",
                               "observed": float(current + notional),
                               "threshold": float(cap), "action": "downsize"})
            return RiskVerdict("downsize",
                               f"Zlecenie obcięte do limitu wielkości pozycji ({cap:.0f})",
                               qty=(qty * factor).quantize(Decimal("0.000001")),
                               violations=violations, usage=usage)

    return RiskVerdict("allow", qty=qty, usage=usage)


# Limity domyslne - uzywane przez symulacje, gdy nie da sie ich odczytac z bazy.
# Muszą odpowiadać wierszom zasianym w risk.limit_def; rozjazd tutaj oznaczałby,
# że backtest mierzy inny system niż produkcja.
# Limity domyslne. Jednostki sa czescia definicji, nie ozdoba: `pct_equity`
# wiaze limit z kapitalem, wiec ten sam zapis znaczy to samo na rachunku
# 100 000 i na 10 000. Wartosci dobrane tak, zeby na 100 000 dawaly DOKLADNIE
# te kwoty, ktore obowiazywaly wczesniej - zmiana jednostki nie mialaby prawa
# zmienic zmierzonego wyniku i nie zmienia.
DEFAULT_LIMITS = {
    "max_daily_loss":        {"value": Decimal("2"),   "unit": "pct",
                              "action": "killswitch"},
    "max_drawdown":          {"value": Decimal("10"),  "unit": "pct",
                              "action": "killswitch"},
    "max_orders_per_hour":   {"value": Decimal("20"),  "unit": "count",
                              "action": "block"},
    "max_position_notional": {"value": Decimal("5"),   "unit": "pct_equity",
                              "action": "downsize"},   # bylo 5 000 przy 100 000
    "max_total_exposure":    {"value": Decimal("15"),  "unit": "pct_equity",
                              "action": "block"},      # bylo 15 000 przy 100 000
    # Twarda podloga w walucie. 5% ponizej kapitalu startowego, czyli ciasniej
    # niz limit obsuniecia - to ona staje sie wiazaca. Zmierzone na 39 oknach:
    # najgorszy punkt ciaglej sciezki to 99,97% wplaty, wiec ten prog nie
    # zadzialalby ani razu. Jest siatka, nie kagancem.
    # Wpisane liczba, nie wyrazeniem ze START_EQUITY: ta stala jest zdefiniowana
    # NIZEJ w pliku i odwolanie do niej tutaj wywraca import. Zwiazek miedzy nimi
    # pilnuje test, a nie kolejnosc linii - bo kolejnosc linii nie jest niczym,
    # na czym wolno oprzec limit ryzyka.
    "min_equity":            {"value": Decimal("95000"),
                              "unit": "quote_ccy", "action": "killswitch"},
}


# --------------------------------------------------------------- wspolne stale
#
# Te trzy rzeczy byly do 2026-09-06 zdefiniowane OSOBNO w symulacji, w trenerze
# i w petli decyzyjnej. Wartosci sie zgadzaly, wiec nic nie bylo widac - ale to
# jest dokladnie ten uklad, ktory ten modul mial wykluczyc (patrz docstring
# wyzej). Tego samego dnia znaleziono w projekcie dwa rozjazdy tego rodzaju:
# stawke prowizji zapisana osobno w petli i regule wypelnienia limitu, ktora
# omal nie zostala przepisana po raz drugi.

# Kapital poczatkowy rachunku. Symulacja i rachunek papierowy musza startowac
# z tej samej liczby, inaczej ich krzywe nie sa porownywalne.
START_EQUITY = 100_000.0

# Prog pewnosci, ponizej ktorego sygnal nie prowadzi do wejscia. Trener kalibruje
# wagi przy TYM progu; gdyby produkcja stosowala inny, handlowalaby innym
# podzbiorem sygnalow niz ten, na ktorym mierzono wynik.
MIN_CONFIDENCE = 0.25


def udzial_budzetu(score: float) -> float:
    """Jaka czesc budzetu na pozycje angazuje sygnal o sile `score`.

    Wzor byl przepisany w dwoch miejscach: `portfolio._sizing` z komentarzem
    "wielkosc zlecenia dokladnie jak w petli decyzyjnej" - czyli z jawnym
    przyznaniem, ze to kopia. Kopia opisana w komentarzu nadal jest kopia
    i rozjedzie sie przy pierwszej zmianie.

    Zwracamy sam UDZIAL, a nie gotowa wielkosc: petla liczy w Decimal i zaokragla
    do groszy, symulacja we float. Dzielenie sie sama regula, a nie arytmetyka,
    nie zmusza zadnej ze stron do zmiany typu.
    """
    return min(1.0, abs(score) * 2)


def position_budget(limits: dict[str, dict], n_instruments: int,
                    kapital: Decimal | None) -> Decimal | None:
    """Bazowa wielkosc zlecenia wyprowadzona z limitu lacznej ekspozycji.

    ZWRACA None, GDY BUDZETU NIE DA SIE POLICZYC - i to jest cala zmiana
    z 2026-09-09. Nie ma tu zadnej wartosci zastepczej, bo kazda z nich byla
    LICZBA UDAJACA POMIAR.

    Do tego dnia `kapital` mial domysl START_EQUITY (100 000), a przy braku
    limitu lacznej ekspozycji funkcja oddawala stala 3000. Skutek na rachunku
    wlasciciela, ktory zamierza wplacic 10 000 przy dziesieciu instrumentach:

        budzet bazowy = 15% * 100 000 / 10 = 1500          (a mial byc 150)
        kazde zlecenie obciete przez max_position_notional do 5% * 10 000 = 500
        dziesiec zlecen po 500 = 5000, czyli 50% rachunku
        limit lacznej ekspozycji: 15% * 10 000 = 1500 -> przekroczony 3,3 raza

    Na demo nie bylo tego widac, bo kapital (100 063) byl przypadkiem rowny
    stalej. Profil ryzyka trzymal sie wiec na ZBIEZNOSCI DWOCH LICZB, a nie na
    kontroli, i rozjechalby sie dokladnie w chwili wplaty innej kwoty. Domysl
    parametru byl tu grozniejszy niz jego brak: wywolanie bez kapitalu
    wygladalo na poprawne i nie zostawialo sladu.

    Wolajacy ma na None jedna sensowna odpowiedz: NIE WCHODZIC. Petla decyzyjna
    dopisuje wtedy powod do `powody_wstrzymania` - tej samej sciezki, ktora
    wstrzymuje wejscia przy niepelnym pomiarze kapitalu.

    DLACZEGO NIE STALA

    Bazowa wielkosc 3000 USDT przy limicie lacznej ekspozycji 15 000 i dwunastu
    obserwowanych instrumentach oznacza, ze system chce zaangazowac 36 000 -
    dwa i pol raza ponad limit. W praktyce mieszcza sie cztery, piec pozycji,
    a pozostale wejscia sa odrzucane.

    Nie jest to bezpieczniejsze: laczne ryzyko wyznacza limit, a nie liczba
    odrzuconych zlecen. Jest za to gorsze pod dwoma wzgledami. Po pierwsze
    portfel jest skoncentrowany na kilku instrumentach zamiast rozlozony na
    wszystkich, przy tej samej ekspozycji. Po drugie o tym, ktore wejscia
    przejda, decyduje kolejnosc rozpatrywania, a nie sila sygnalu.

    Budzet liczony z limitu daje pelna dywersyfikacje przy NIEZMIENIONYM
    ryzyku lacznym. Nie jest to poluzowanie limitu - to jego wykorzystanie
    zgodnie z przeznaczeniem.
    """
    lim = limits.get("max_total_exposure")
    if not lim or n_instruments <= 0:
        # Brak limitu lacznej ekspozycji nie znaczy "budzet dowolny". Ta sama
        # nieobecnosc sprawia, ze `evaluate_limits` w ogole pomija kontrole
        # ekspozycji (`if lim:`), wiec stala odesłana stad bylaby wielkoscia
        # pozycji, ktorej NIC juz nie ogranicza.
        return None
    # KAPITAL JEST WYMAGANY, nawet gdy limit zapisano kwotowo. Sprawdzamy go
    # przed `wartosc_limitu`, a nie w jej srodku, bo przy jednostce `quote_ccy`
    # kapital nie wchodzi do wzoru - i wywolanie bez pomiaru przechodziloby
    # niezauwazone dopoty, dopoki ktos nie zmieni jednostki limitu na
    # procentowa. Zgoda na handel nie moze zalezec od zapisu limitu.
    if kapital is None or kapital <= 0:
        return None
    budzet = wartosc_limitu(lim, kapital) / Decimal(n_instruments)
    # Zlecenie mniejsze niz minimum gieldowe nie ma sensu - wtedy lepiej
    # obserwowac mniej instrumentow niz handlowac pylem. Podloga moze wyniesc
    # budzet ponad udzial przypadajacy na instrument; laczna ekspozycja i tak
    # sie nie przekroczy, bo od 2026-09-09 kumuluje sie W OBREBIE CYKLU
    # (risk.RiskKernel._open_exposure) i po prostu utnie ostatnie wejscia.
    return max(budzet, Decimal("50"))
