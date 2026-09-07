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
    exposure: Decimal
    per_instrument: dict[int, Decimal]
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

    # --- widoki uzywane przez reguly -------------------------------------
    #
    # Reguly pytaja o "wynik", a nie o "kapital". Te trzy wlasnosci decyduja
    # RAZ, ktora liczba jest wynikiem - zamiast powtarzac ten wybor przy kazdym
    # limicie, gdzie predzej czy pozniej jeden z nich zostalby pominiety.
    @property
    def wynik(self) -> Decimal:
        return self.nav if self.nav is not None else self.equity

    @property
    def wynik_szczyt(self) -> Decimal:
        return self.nav_peak if self.nav_peak is not None else self.peak

    @property
    def wynik_doba(self) -> Decimal:
        return self.nav_day_ago if self.nav_day_ago is not None else self.day_ago


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
    """
    violations: list[dict] = []
    usage: list[dict] = []

    # 1. Kill-switch. Nic dalej nie ma znaczenia.
    if state.killswitch:
        ts = state.killswitch.get("triggered_at")
        kiedy = f" od {ts:%Y-%m-%d %H:%M}" if ts else ""
        return RiskVerdict("block",
                           f"Kill-switch aktywny{kiedy}: {state.killswitch.get('reason')}")

    # 2. Strata dzienna.
    lim = limits.get("max_daily_loss")
    if lim and state.wynik_doba > 0:
        # Z WYNIKU, nie z kapitalu: wyplata obniza kapital zgodnie z wola
        # wlasciciela i nie jest strata dzienna.
        loss_pct = float((state.wynik_doba - state.wynik) / state.wynik_doba * 100)
        usage.append({"name": "max_daily_loss", "used": max(0.0, loss_pct),
                      "threshold": float(lim["value"])})
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
        # NIGDY PROCENTOWO OD SZCZYTU. Prog wyrazony jako procent szczytu jest
        # DOKLADNIE limitem obsuniecia, ktory stoi punkt nizej - czyli
        # duplikatem, ktory wyglada na dodatkowe zabezpieczenie i nim nie jest.
        #
        # Dopuszczalne sa dwie postacie. Kwotowa jest prosta, ale przestaje
        # dzialac po wyplacie: rachunek spada zgodnie z wola wlasciciela,
        # a prog zostaje w miejscu i zatrzymuje silnik bez powodu. `pct_wplat`
        # przesuwa sie razem z wplatami netto i wyraza to, o co chodzi:
        # "nie strac wiecej niz tyle z tego, co wlozylem".
        if lim.get("unit") == "pct_wplat" and state.wplaty_netto is not None:
            prog = Decimal(lim["value"]) / Decimal(100) * state.wplaty_netto
        else:
            prog = Decimal(lim["value"])
        usage.append({"name": "min_equity", "used": float(state.equity),
                      "threshold": float(prog)})
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
    if lim and state.wynik_szczyt > 0:
        # Z WYNIKU, nie z kapitalu: wplata podnosi kapital i szczyt, a nie jest
        # zyskiem - obsuniecie liczone od takiego szczytu mierzyloby przelew.
        dd_pct = float((state.wynik_szczyt - state.wynik) / state.wynik_szczyt * 100)
        usage.append({"name": "max_drawdown", "used": max(0.0, dd_pct),
                      "threshold": float(lim["value"])})
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

    # 5. Laczna ekspozycja.
    lim = limits.get("max_total_exposure")
    if lim:
        after = state.exposure + notional
        usage.append({"name": "max_total_exposure", "used": float(state.exposure),
                      "threshold": float(lim["value"])})
        if after > wartosc_limitu(lim, state.equity):
            violations.append({"limit": "max_total_exposure", "observed": float(after),
                               "threshold": float(lim["value"]), "action": lim["action"]})
            return RiskVerdict(
                "block",
                f"Ekspozycja po wejściu wyniosłaby {after:.0f} przy limicie {lim['value']:.0f}",
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
                    kapital: Decimal | None = None,
                    fallback: Decimal = Decimal("3000")) -> Decimal:
    """Bazowa wielkosc zlecenia wyprowadzona z limitu lacznej ekspozycji.

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
        return fallback
    # `kapital` jest potrzebny wylacznie wtedy, gdy limit zapisano procentowo.
    # Domyslnie START_EQUITY, zeby stare wywolania nie zmienily zachowania -
    # przy limicie kwotowym ta wartosc i tak nie jest uzywana.
    budzet = wartosc_limitu(
        lim, kapital if kapital is not None else Decimal(str(START_EQUITY))
    ) / Decimal(n_instruments)
    # Zlecenie mniejsze niz minimum gieldowe nie ma sensu - wtedy lepiej
    # obserwowac mniej instrumentow niz handlowac pylem.
    return max(budzet, Decimal("50"))
