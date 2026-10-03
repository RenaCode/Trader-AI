"""Dostawca modelu jezykowego dla analizy wiadomosci.

DLACZEGO TA WARSTWA ISTNIEJE

Analiza wiadomosci byla napisana wprost na kliencie Anthropica: `messages.create`
z `output_config`, `thinking` i `cache_control` siedzialy w `news_worker`. Dopoki
dostawca byl jeden, nie bylo w tym nic zlego. Gdy pojawil sie drugi klucz - Gemini,
na czas oczekiwania na Anthropica - okazalo sie, ze zamiana dostawcy wymaga zmian
w piecu miejscach potoku, ktory dziala na produkcji.

Interfejs jest WASKI CELOWO. Dostawca umie dwie rzeczy: zadac pytanie z wymuszonym
schematem odpowiedzi i policzyc, ile to kosztowalo. Prompty, schematy, progi
istotnosci i cala reszta zadania zostaja w `news_worker` - inaczej dodanie
trzeciego dostawcy oznaczaloby przepisanie promptow po raz trzeci.

CZEGO TA WARSTWA NIE ZALATWIA

Analiza z dwoch roznych modeli NIE JEST tym samym pomiarem. Cecha `news_sent`
zbudowana w polowie z ocen Gemini, a w polowie z ocen Claude'a, ma w srodku
uskok, ktorego model nie widzi - a kalibracja wagi zaklada, ze cecha znaczy przez
caly czas to samo. Dlatego kolumna `news.analysis.model` zapisuje model przy
KAZDYM wierszu i zmiana dostawcy jest w logu wykrzyczana, a nie cicha.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, NamedTuple

import httpx


class BladOdpowiedzi(RuntimeError):
    """Wywolanie doszlo do skutku, ale wynik jest bezuzyteczny.

    NIESIE ZUZYCIE TOKENOW, bo nieudane wywolanie tez kosztuje. Do 2026-09-07
    tabela `news.api_spend` miala kolumne `ok`, do ktorej nikt nigdy nie zapisal
    `false`: nieudane wywolanie nie bylo zapisywane w ogole. Limit dobowy - jedyny
    bezpiecznik przed petla palaca budzet - byl wiec slepy dokladnie na te
    wywolania, ktore potrafia sie powtarzac w kolko.
    """

    def __init__(self, powod: str, usage: dict | None = None):
        super().__init__(powod)
        self.usage = usage or {"input": 0, "output": 0, "cache_read": 0}


class Przeciazony(RuntimeError):
    """Dostawca odmawia z powodu LIMITU, nie z powodu bledu w zapytaniu.

    `dobowy=True` znaczy, ze wyczerpany jest limit NA DOBE, a nie tempo. To sa
    dwie rozne sytuacje i myla sie tylko dlatego, ze maja ten sam kod HTTP:
    limit tempa mija w sekundy i ponawianie ma sens, limit dobowy nie minie do
    nastepnej doby i KAZDE ponowienie jest czysta strata czasu.

    Osobna klasa, a nie goly RuntimeError, bo to jedyny wyjatek, po ktorym wolno
    siegnac po drugiego dostawce. Lapanie RuntimeError zamiotlo by przy okazji
    kazdy blad w naszym kodzie - i zamiast awarii dostalibysmy ciche zejscie
    na dostawce zapasowego, z rachunkiem, ktory rosnie bez powodu.
    """

    def __init__(self, komunikat: str, *, dobowy: bool = False):
        super().__init__(komunikat)
        self.dobowy = dobowy


class Wyczerpany(Przeciazony):
    """Awaria TRWALA: nie minie w tym cyklu i najpewniej nie minie dzis sama.

    Wyczerpany kredyt, uniewazniony klucz, brak uprawnien do modelu. Rozroznienie
    nie jest akademickie - to jest ROZNICA W TYM, CO WOLNO ZROBIC DALEJ. Przy
    limicie tempa kolejny artykul ma sens (limit mija w sekundy), przy braku
    kredytu KAZDY kolejny artykul to jedno wywolanie, ktore na pewno odmowi.
    Zmierzone na produkcji 2026-09-10: 400 "Your credit balance is too low"
    powtorzone 52-62 razy w kazdym cyklu, cykl co 5 minut - okolo 15 tysiecy
    bezuzytecznych wywolan na dobe, bo zaden bezpiecznik nie mial czego zewrzec.

    DLACZEGO PODKLASA `Przeciazony`, A NIE OSOBNA GALAZ
    `Przeciazony` znaczy "dostawca odmawia z powodu limitu, nie z powodu bledu
    w naszym zapytaniu" - i to jest jedyny wyjatek, po ktorym wolno siegnac po
    drugiego dostawce w triazu. Martwy klucz spelnia ten warunek tak samo jak
    limit tempa: zejscie na zapasowego jest wtedy tak samo uprawnione, a nawet
    bardziej pilne. Osobna galaz wymagalaby dopisania drugiego `except` w kazdym
    miejscu, ktore juz teraz obsluguje odmowe - i pierwsze zapomniane miejsce
    przywrocilo by dokladnie ten blad, ktory ta klasa naprawia.

    Rozni sie DLUGOSCIA zwarcia: `Przeciazony` na minuty, `Wyczerpany` na godziny.
    """


class BrakDostawcy(RuntimeError):
    """Nie ma juz KOGO zapytac - kazdy dostawca w tej konfiguracji jest odciety.

    Podnoszony BEZ wywolania API, wiec nie niesie zuzycia tokenow. To jest
    sygnal "przestan probowac w tym cyklu", a nie kolejna awaria do policzenia
    w statystykach - patrz kontrakt w `Dostawca`.

    NIE jest podklasa `Przeciazony`: po `Przeciazony` wolno siegnac po drugiego
    dostawce, a tu wlasnie drugiego dostawcy juz nie ma.
    """


class Odmowa(BladOdpowiedzi):
    """Model odmowil odpowiedzi. Nie jest to awaria - jest to odpowiedz."""

    def __init__(self, usage: dict, powod: str = "model odmówił odpowiedzi"):
        super().__init__(powod, usage)


def limit_dobowy_usd() -> float:
    """Twardy limit wydatkow na dobe.

    `ANTHROPIC_DAILY_USD` zostaje jako nazwa zapasowa, bo pod taka nazwa zmienna
    wystepuje w starszych opisach wdrozenia; nowa nazwa jest neutralna wobec
    dostawcy.

    UWAGA NA TRESC TEGO KOMENTARZA. Do 12.09.2026 stalo tu, ze `ANTHROPIC_DAILY_USD`
    "juz jest ustawiona na klastrze". NIE BYLA - ani na `trader-news`, ani na
    `trader-api`; oba procesy schodzily na 5.0 ponizej. Komentarz opisywal stan
    zamierzony jako stan faktyczny, wiec kazdy, kto go czytal, szukal wartosci
    limitu nie tam, gdzie ona naprawde byla. Wartosc obowiazujaca sprawdza sie
    jedna komenda, nie lektura:
        kubectl get deploy trader-news -o jsonpath='{.spec.template.spec.containers[0].env[*].name}'
    """
    for nazwa in ("LLM_DAILY_USD", "ANTHROPIC_DAILY_USD"):
        v = os.environ.get(nazwa)
        if v:
            return float(v)
    return 5.0


def limit_wywolan_dobowy() -> int:
    """Twardy limit LICZBY wywolan na dobe - druga bramka obok kwotowej.

    Istnieje, bo kwotowa nie zadzialala ani razu. Darmowe progi dostawcow sa
    liczone w ZAPYTANIACH, nie w dolarach: 11.09.2026 Gemini odcial potok po
    512 wywolaniach za lacznie $0,114, czyli przy 2% wykorzystania limitu
    kwotowego. Bramka, ktora mierzy nie te wielkosc co dostawca, nie zatrzyma
    niczego przed nim - zatrzyma dopiero po fakcie.

    Domysl 1500 wynika z pomiaru, nie z okraglosci: zdrowa doba to 503-516
    wywolan (07-11.09), doba z petla deduplikacji to 4404 (09.09). 1500 lezy
    miedzy nimi z zapasem w obie strony.
    """
    v = os.environ.get("LLM_DAILY_CALLS")
    return int(v) if v else 1500


def _bez_additional_properties(schema: Any) -> Any:
    """Schemat w wersji, ktora przyjmuje Gemini.

    `additionalProperties` jest w JSON Schema, ale nie w podzbiorze OpenAPI,
    ktorego uzywa `responseSchema`. Wyslanie go konczy sie bledem 400, wiec
    zamiast utrzymywac drugi schemat - z ryzykiem, ze rozjedzie sie z pierwszym -
    tniemy pole w locie.
    """
    if isinstance(schema, dict):
        return {k: _bez_additional_properties(v) for k, v in schema.items()
                if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_bez_additional_properties(v) for v in schema]
    return schema


class Wykonawca(NamedTuple):
    """Dostawca i model, ktore NAPRAWDE obsluzyly wywolanie.

    Nie ten, o ktory prosilismy - ten, ktory odpowiedzial (albo odmowil). Przy
    jednym dostawcy to zawsze to samo; rozjezdza sie dokladnie wtedy, gdy
    mieszanka zeszla na dostawce zapasowego.
    """

    nazwa: str
    model: str

    def __str__(self) -> str:
        return f"{self.nazwa}/{self.model}"


def oznacz_wykonawce(e: Exception, nazwa: str, model: str) -> None:
    """Dopisuje do WYJATKU, kto go zwrocil. Pierwszy wpis wygrywa.

    DLACZEGO NA WYJATKU, A NIE W `usage`

    Przy sukcesie model faktyczny wraca w `usage["model_uzyty"]` i to wystarcza.
    Przy awarii `usage` czesto nie ma w ogole: `Przeciazony` i `Wyczerpany` nie
    niosa zadnego zuzycia, bo odmowa zapadla, zanim powstal jakikolwiek token.
    A to wlasnie sciezka awarii idzie do logu - i tam brakowalo prawdy.

    Zmierzone na produkcji 2026-09-10, ta sama linia 52-62 razy w kazdym cyklu:

        BLAD triaz gemini-3.5-flash-lite: BadRequestError: ... 'Your credit
        balance is too low to access the Anthropic API...'

    Nazwa mowila "gemini", odmowil Anthropic (mieszanka zeszla na zapasowego po
    wyczerpaniu limitu dobowego Gemini), a szukanie awarii poszlo za nazwa -
    czyli w strone dostawcy, ktory z tym bledem nie mial nic wspolnego.

    Pierwszy wpis wygrywa, bo stempluje ten, kto WYKONAL wywolanie; warstwy
    wyzej tylko przepuszczaja wyjatek dalej i nadpisanie zamienialoby prawde
    z powrotem na model zadany.
    """
    if getattr(e, "wykonawca", None) is not None:
        return
    try:
        e.wykonawca = Wykonawca(nazwa, model)
    except AttributeError:
        # Wyjatek z `__slots__` nie przyjmie atrybutu. Log spadnie wtedy na
        # model zadany - gorzej, ale nie kosztem awarii W OBSLUDZE AWARII.
        pass


def kto_wykonal(e: Exception, ai: "Dostawca", model_zadany: str) -> Wykonawca:
    """Wykonawca odczytany z wyjatku; gdy go nie ma - ten, o ktory prosilismy.

    Domysl jest PRAWDZIWY tam, gdzie dostawca jest jeden: zadany model jest
    wtedy jedynym, ktory mogl odpowiedziec. Rozjazd wprowadza dopiero mieszanka
    i to ona stempluje wyjatki (`DostawcaMieszany._zapytaj_u`) - stempel siedzi
    w warstwie, ktora KIERUJE zapytaniami, wiec obejmuje takze dostawcow
    dopisanych pozniej, ktorzy o zadnym stemplowaniu nie wiedza.
    """
    w = getattr(e, "wykonawca", None)
    return w if isinstance(w, Wykonawca) else Wykonawca(ai.nazwa, model_zadany)


class Dostawca:
    """Wspolny interfejs. Podklasa dostarcza transport i cennik.

    KONTRAKT NA AWARIE — CZYTA GO `news_worker`, WIEC MUSI DAC SIE ZROZUMIEC TUTAJ

    `zapytaj` konczy sie na jeden z pieciu sposobow i kazdy znaczy co innego dla
    NASTEPNEGO artykulu:

      * wynik `(dane, usage)`,
      * `Odmowa` / `BladOdpowiedzi` — wywolanie doszlo do skutku i KOSZTOWALO,
        ale wyniku nie ma. Ten artykul nie idzie dalej, nastepny probujemy
        normalnie,
      * `Przeciazony` — awaria PRZEJSCIOWA (limit tempa, 5xx, timeout). Mija
        sama, nastepny artykul ma sens,
      * `Wyczerpany` (podklasa `Przeciazony`) — awaria TRWALA (brak kredytu,
        martwy klucz, brak uprawnien do modelu). Nie minie w tym cyklu,
      * `BrakDostawcy` — nie ma juz kogo pytac; wywolanie NIE poszlo do API.

    JEDEN WARUNEK, KTORY MA SPRAWDZAC WOLAJACY: `czy_ma_czym_pytac()`.

    Zwraca False, gdy dalsze pytania w tym cyklu sa z gory stracone. Wtedy
    news_worker ma PRZESTAC wolac `triage`/`analyze`, ale MA DALEJ ZAPISYWAC
    artykuly (sciezka `analizuj=False`): kanal RSS nie odda jutro tego, czego
    dzis nie pobierzemy, a zapisany artykul bez analizy odzyska `nadrob_zaleglosci`
    w oknie 24 h, gdy tylko dostawca wroci. Przerwanie CALEGO cyklu byloby
    lekarstwem gorszym od choroby — traciloby material bezpowrotnie.

    Warunek trzeba sprawdzac TAKZE wewnatrz petli po artykulach, bo stan zmienia
    sie w jej trakcie: pierwszy artykul dostaje `Wyczerpany`, a dopiero przez
    niego dostawca zostaje odciety. `zapytaj` samo pilnuje tej samej zasady
    i podnosi `BrakDostawcy` zamiast wolac martwe API — warunek u wolajacego
    oszczedza petle, a nie chroni budzet.

    `powod_niedostepnosci()` daje tekst do logu; pusty, gdy dostawca jest zdrowy.
    """

    nazwa: str = ""
    model_analizy: str = ""
    model_triazu: str = ""
    # USD za milion tokenow: model -> (wejscie, wyjscie)
    CENNIK: dict[str, tuple[float, float]] = {}
    CENNIK_DOMYSLNY: tuple[float, float] = (5.0, 25.0)

    # ILE TRWA ODCIECIE PO AWARII TRWALEJ.
    #
    # Szesc godzin, a nie dziesiec minut i nie "do konca doby". Brak kredytu nie
    # mija z uplywem czasu - konczy go CZLOWIEK, ktory doladuje konto albo wymieni
    # klucz. Proba co dziesiec minut to 144 bezuzyteczne wywolania na dobe zamiast
    # 15 tysiecy - lepiej, ale wciaz bez sensu, bo zadne z nich niczego nie zmieni.
    # Szesc godzin daje cztery proby na dobe i nic przy tym nie tracimy: artykuly
    # zapisane bez analizy odzyskuje `nadrob_zaleglosci` w oknie 24 h.
    #
    # Wymiana klucza NIE czeka na ten czas - nowy klucz to nowy obiekt dostawcy,
    # a stanu bezpiecznika nie wolno wtedy przenosic (patrz `DostawcaMieszany`).
    ZWARCIE_TRWALE_S = 6 * 3600.0

    # Stan bezpiecznika. Atrybuty KLASY, zeby podklasy nie musialy wolac
    # `super().__init__` - przypisanie w `odetnij` tworzy atrybut instancji.
    _niedostepny_do: float = 0.0
    _powod_niedostepnosci: str = ""
    # Odmowy POD RZAD. Trzyma je dostawca, ktory odmawia, a nie ten, kto pyta:
    # inaczej mieszanka musialaby prowadzic osobny licznik dla kazdej roli
    # i pierwsza zapomniana rola zostalaby bez bezpiecznika (tak wlasnie
    # analiza zostala bez niego do 2026-09-10).
    _odmowy_pod_rzad: int = 0

    # ZADANIA HTTP, A NIE WYWOLANIA LOGICZNE - dwie rozne wielkosci.
    #
    # Bramka dobowa liczyla wywolania LOGICZNE (jeden wiersz w `news.api_spend`
    # na jeden triaz albo jedna analize), a dostawcy licza ZADANIA HTTP. Jedno
    # logiczne wywolanie triazu to u Gemini do pieciu POST-ow (`_poslij`,
    # `PROBY = 5`), a u Anthropica do dwoch (`max_retries=1` - SDK ponawia
    # wewnatrz i z zewnatrz tego nie widac). Do tego wywolanie zakonczone
    # `Przeciazony` / `Wyczerpany` / timeoutem nie niesie `usage`, wiec
    # `news_worker.zapisz_nieudane` NIE WSTAWIA wiersza w ogole - takie zadanie
    # nie bylo liczone nawet jako jedno.
    #
    # Skutek jest dokladnie ten, przed ktorym ostrzega komentarz przy
    # `limit_wywolan_dobowy`: doba z presja limitu tempa to 500 wywolan
    # logicznych i do 2500 zadan do Google. Bramka czytala 500/1500 i meldowala
    # "wykorzystanie 33%", gdy darmowy prog 500/dobe byl juz przekroczony
    # pieciokrotnie. Bramka, ktora mierzy nie te wielkosc co dostawca, nie
    # zatrzyma niczego przed nim.
    #
    # Dlatego licznik siedzi TUTAJ, u dostawcy, a nie w dzienniku: podbija go
    # KAZDE zadanie HTTP - ponowienie i odmowa bez `usage` tak samo jak sukces.
    # Dziennik `api_spend` zostaje do rozliczenia KWOTY, bo tam liczy sie zuzycie
    # tokenow, a nie liczba pakietow.
    #
    # OGRANICZENIE, KTORE TRZEBA ZNAC: licznik zyje w PAMIECI PROCESU. Restart
    # poda gubi nadwyzke ponowien z biezacej doby (kwota i liczba wierszy wracaja
    # z bazy, bo tam sa zapisane). Zapis nadwyzki do bazy wymaga kolumny
    # w `news.api_spend`, czyli migracji - to jest zaleznosc poza tym plikiem.
    _zadan_http: int = 0
    _zadan_rozliczonych: int = 0

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        raise NotImplementedError

    def czy_ma_czym_pytac(self) -> bool:
        """Czy pytanie tego dostawcy ma jeszcze jakikolwiek sens w tym cyklu."""
        return time.monotonic() >= self._niedostepny_do

    def powod_niedostepnosci(self) -> str:
        return "" if self.czy_ma_czym_pytac() else self._powod_niedostepnosci

    def odetnij(self, ile_s: float, powod: str) -> None:
        """Przestan pytac tego dostawce przez `ile_s` sekund.

        Nie skraca juz trwajacego odciecia: awaria trwala, po ktorej przyjdzie
        zwykly limit tempa, nie moze zamienic szesciu godzin w dziesiec minut.
        """
        do = time.monotonic() + ile_s
        if do > self._niedostepny_do:
            self._niedostepny_do = do
            self._powod_niedostepnosci = powod

    def _sprawdz_dostepnosc(self) -> None:
        """Podnosi `BrakDostawcy` zamiast wolac API, o ktorym wiadomo, ze odmowi.

        Wolane na wejsciu `zapytaj`, bo bezpiecznik ma dzialac takze wtedy, gdy
        dostawca jest jeden - wyczerpany kredyt pali wtedy dokladnie tyle samo
        wywolan, co w mieszance, tylko nikt nie ma go od czego odbic.
        """
        if not self.czy_ma_czym_pytac():
            raise BrakDostawcy(
                f"{self.nazwa}: {self._powod_niedostepnosci} — odciety na "
                f"{(self._niedostepny_do - time.monotonic()) / 60:.0f} min")

    def uzyty_model(self, model: str, usage: dict) -> str:
        """Model, ktory NAPRAWDE obsluzyl zapytanie.

        Zwykle ten, o ktory prosilismy. Mieszanka dostawcow moze jednak zejsc
        na zapasowego przy limicie - a wtedy i cena, i wpis w dzienniku wydatkow
        musza dotyczyc modelu, ktory faktycznie pracowal. Inaczej dziennik
        pokazuje tanszy model i tansza stawke za prace, ktorej on nie wykonal.
        """
        return usage.get("model_uzyty") or model

    def koszt(self, model: str, usage: dict) -> float:
        inp, out = self.CENNIK.get(model, self.CENNIK_DOMYSLNY)
        # Odczyt z cache kosztuje ~10% ceny wejscia u obu dostawcow.
        billed_in = (usage["input"] - usage["cache_read"]) + usage["cache_read"] * 0.1
        return round(billed_in / 1e6 * inp + usage["output"] / 1e6 * out, 6)

    def zanotuj_zadanie(self, ile: int = 1) -> None:
        """Jedno ZADANIE HTTP poszlo do dostawcy. Takze ponowienie i odmowa.

        Wolane z miejsca, ktore naprawde wysyla pakiet - u Gemini z petli
        ponowien w `_poslij`, u Anthropica z middleware SDK, ktore chodzi raz na
        KAZDA probe wewnatrz jego wlasnej petli ponowien.
        """
        # Przypisanie (a nie `+=` na atrybucie klasy) tworzy atrybut INSTANCJI -
        # tak samo jak `odetnij` wyzej. Inaczej dwa rownolegle zbudowane obiekty
        # dostawcy dzielilyby jeden licznik.
        self._zadan_http = self._zadan_http + ile

    def zanotuj_rozliczenie(self, ile: int = 1) -> None:
        """Jedno zadanie trafilo do dziennika `news.api_spend`.

        Wolane przez `news_worker.Budzet.dolicz`, czyli dokladnie tam, gdzie
        powstaje wiersz dziennika. Rozdzial jest po to, zeby `zadania_poza_
        dziennikiem` liczylo tylko NADWYZKE i zadne zadanie nie wpadlo do bramki
        dwa razy.
        """
        self._zadan_rozliczonych = self._zadan_rozliczonych + ile

    def zadania_poza_dziennikiem(self) -> int:
        """Ile zadan HTTP tego procesu NIE MA odpowiednika w `api_spend`.

        To sa ponowienia i odmowy bez `usage` - czyli dokladnie ta czesc ruchu,
        ktorej bramka dobowa nie widziala. Nigdy ujemne: wiersze moga pochodzic
        z wczesniejszego procesu, zadania liczymy od startu tego.
        """
        return max(0, self._zadan_http - self._zadan_rozliczonych)

    async def zamknij(self) -> None:
        return None


class _LicznikZadanHTTP:
    """Middleware klienta Anthropica: jedno przejscie = jedno zadanie HTTP.

    Zwykla funkcja asynchroniczna, a NIE podklasa `anthropic.Middleware` - i to
    jest decyzja, nie skrot. Pakiet `anthropic` jest tylko w obrazie; klasa
    dziedziczaca po nim wymagalaby importu w chwili WCZYTANIA modulu, czyli
    wywracalaby `import llm` na kazdej maszynie bez tej zaleznosci - w tym przy
    testach, ktore dostawcy Anthropica w ogole nie dotykaja. SDK przyjmuje
    zwykly obiekt wywolywalny z asynchronicznym `__call__` i sam sprawdza,
    ze jest asynchroniczny (`_middleware.validate_async_middleware`).
    """

    def __init__(self, dostawca: "Dostawca"):
        self._dostawca = dostawca

    async def __call__(self, request, call_next):
        # PRZED wywolaniem, nie po nim: zadanie, ktore skonczylo sie timeoutem
        # albo zerwanym polaczeniem, tez zjadlo prog dostawcy, a `call_next`
        # wtedy rzuca i nic po nim juz sie nie wykona.
        self._dostawca.zanotuj_zadanie()
        return await call_next(request)


class DostawcaAnthropic(Dostawca):
    nazwa = "anthropic"

    # Stawki odczytane z cennika Anthropica 2026-09-23, USD za milion tokenow.
    #
    # `claude-sonnet-5` mial tu do 2026-09-23 wpisane (3.0, 15.0) - to sa stawki
    # SONNETA 4.6, poprzedniej generacji. Sonnet 5 kosztuje $2/$10. Pomylka szla
    # w strone bezpieczna (zawyzala koszt o 50%, czyli zamykala bramke kwotowa
    # przedwczesnie), ale dziennik `news.api_spend` przez nia klamal - a na tym
    # dzienniku stoi zarowno bramka, jak i `/api/api-spend`.
    CENNIK = {
        "claude-opus-5": (5.0, 25.0),
        "claude-sonnet-5": (2.0, 10.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    # NIEZNANY MODEL WYCENIAMY NAJDROZSZA DOSTEPNA TARYFA, nie stawka opusa.
    #
    # Do 2026-09-23 klasa dziedziczyla `CENNIK_DOMYSLNY = (5.0, 25.0)` z `Dostawca`,
    # czyli dokladnie stawke opusa - a u Anthropica sa modele DROZSZE (taryfa
    # klasy Fable to $10/$50). Model spoza tablicy - a to wlasnie nowsza rodzina
    # trafia do chartu przy zmianie - wyceenialby sie DWUKROTNIE ZA TANIO.
    # `DostawcaGemini` robi to poprawnie od poczatku i uzasadnia to asymetria
    # ryzyka (patrz jego `CENNIK_DOMYSLNY`): zawyzenie zatrzymuje potok na
    # limicie, zanizenie pozwala mu wydac wielokrotnosc limitu, zanim ktokolwiek
    # to zauwazy.
    CENNIK_DOMYSLNY = (10.0, 50.0)

    def __init__(self, klucz: str):
        from anthropic import AsyncAnthropic

        # TIMEOUT I LIMIT PONOWIEN WPISANE WPROST.
        #
        # Domyslnie SDK czeka 600 s i ponawia dwa razy, wiec JEDNO wywolanie
        # moglo zablokowac workera na okolo pol godziny - przy interwale cyklu
        # 300 s. Tak wlasnie zamilkl worker newsow 2026-09-08: pod staly
        # "Running", zero logow przez 41 minut, zadnego wyjatku do zlapania.
        # Klient Gemini obok mial 90 s od poczatku; ta rozbieznosc nie byla
        # decyzja, tylko przeoczeniem.
        #
        # 180 s z jednym ponowieniem daje najgorszy przypadek ~6 minut, co
        # miesci sie w limicie calego cyklu. Analiza z rozszerzonym myśleniem
        # bywa wolniejsza od triazu, stad wiecej niz 90 s.
        #
        # MIDDLEWARE LICZY ZADANIA HTTP, a nie wywolania logiczne. SDK ponawia
        # WEWNATRZ `messages.create` (`max_retries=1`, czyli do dwoch zadan na
        # jedno wywolanie) i z zewnatrz tego nie widac - tak samo jak nie bylo
        # widac piatki ponowien Gemini. Lancuch middleware chodzi raz na KAZDA
        # probe wewnatrz tej petli, wiec jest to jedyne miejsce, z ktorego da sie
        # policzyc to, co naprawde liczy dostawca. Patrz `Dostawca._zadan_http`.
        self._c = AsyncAnthropic(api_key=klucz, timeout=180.0, max_retries=1,
                                 middleware=[_LicznikZadanHTTP(self)])
        self.model_analizy = os.environ.get("ANALYSIS_MODEL", "claude-opus-5")
        self.model_triazu = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5")

    async def zamknij(self) -> None:
        # SDK TEZ TRZYMA PULE POLACZEN - nie tylko klient Gemini obok.
        # Dostawca Gemini domykal swoja od poczatku, ten nie domykal niczej,
        # wiec kazda wymiana klucza i kazde zakonczenie procesu zostawialy
        # otwarte gniazda. Widac to bylo dopiero przy dostawcy budowanym co
        # cykl: 338 cykli w jednym procesie to bylo 338 porzuconych pul.
        await self._c.close()

    # SLOWA, PO KTORYCH POZNAJEMY ODMOWE ROZLICZENIOWA.
    #
    # Rozroznienie idzie po TRESCI, nie po kodzie HTTP - dokladnie tak jak
    # `DostawcaGemini.limit_dobowy` czyta `quotaId`, bo Google tez daje jeden kod
    # na dwie rozne sytuacje. Tutaj jest tak samo, tylko gorzej: 400 to u
    # Anthropica ZAROWNO nasz blad w schemacie (do naprawienia w kodzie, ma byc
    # glosny), JAK I wyczerpany kredyt (nic w kodzie nie pomoze). Zmierzone na
    # produkcji 2026-09-10:
    #   400 {'type': 'invalid_request_error', 'message': 'Your credit balance is
    #   too low to access the Anthropic API. Please go to Plans & Billing...'}
    ODMOWA_ROZLICZENIOWA = ("credit balance", "purchase credits", "billing",
                            "insufficient funds", "credit card")

    @staticmethod
    def _tresc_odmowy(e: Exception) -> str:
        """Komunikat razem z cialem odpowiedzi, malymi literami.

        Cialo, bo `str(wyjatku)` bywa u SDK skrocone, a rozstrzyga wlasnie tresc.
        """
        czesci = [str(e), str(getattr(e, "message", "") or "")]
        cialo = getattr(e, "body", None)
        if cialo is not None:
            czesci.append(json.dumps(cialo, ensure_ascii=False, default=str))
        return " ".join(czesci).lower()

    def _przeloz_odmowe(self, e: Exception, model: str) -> None:
        """Zamienia odmowe SDK na wyjatek, ktory niesie DECYZJE, a nie kod HTTP.

        Nie podnosi niczego, gdy nie umie zaklasyfikowac - wtedy oryginalny blad
        leci dalej nietkniety. Cisza jest tu wazniejsza od domyslu: blad w naszym
        schemacie ma zostac glosny, a nie zamienic sie w "dostawca chwilowo
        niedostepny", po ktorym potok w nieskonczonosc czeka na cud.

        Rozpoznajemy po atrybutach (`status_code`, `body`), a nie przez
        `isinstance` — pakiet `anthropic` jest importowany leniwie, zeby obraz
        z samym kluczem Gemini nie musial go miec w ogole.
        """
        kod = getattr(e, "status_code", None)
        tresc = self._tresc_odmowy(e)
        rozliczeniowa = any(s in tresc for s in self.ODMOWA_ROZLICZENIOWA)

        if kod is None:
            # Timeout i zerwane polaczenie nie maja kodu HTTP. Sa przejsciowe:
            # SDK probowal juz raz (max_retries=1), nastepny artykul ma sens.
            if type(e).__name__ in ("APITimeoutError", "APIConnectionError"):
                raise Przeciazony(f"{self.nazwa}: brak odpowiedzi na zapytanie "
                                  f"o model {model} ({type(e).__name__})") from e
            return

        if kod == 429 and not rozliczeniowa:
            raise Przeciazony(f"{self.nazwa}: limit tempa na modelu {model} "
                              f"(HTTP 429)") from e
        if kod in (401, 403) or rozliczeniowa:
            # 401 martwy/odwolany klucz, 403 brak uprawnien do modelu,
            # tresc rozliczeniowa przy dowolnym kodzie - wszystkie trzy trwaja,
            # dopoki nie zrobi czegos czlowiek.
            powod = ("brak srodkow na koncie" if rozliczeniowa
                     else "klucz odrzucony albo bez uprawnien do modelu")
            self.odetnij(self.ZWARCIE_TRWALE_S, f"{powod} (HTTP {kod})")
            raise Wyczerpany(
                f"{self.nazwa}: {powod} (HTTP {kod}, model {model}). Ponawianie "
                f"nie pomoze - to sie nie odnowi samo, wymaga doladowania konta "
                f"albo wymiany klucza.") from e
        if kod >= 500:
            raise Przeciazony(f"{self.nazwa}: awaria po stronie dostawcy "
                              f"(HTTP {kod}, model {model})") from e
        # 400 bez tresci rozliczeniowej to NASZ blad - zly schemat, zle pole.
        # Leci dalej w oryginalnej postaci, glosno.

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        self._sprawdz_dostepnosc()
        kw: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # Prefiks systemowy jest miedzy wywolaniami staly, wiec trafia
            # w cache promptow - okolo dziesieciokrotnie tanszy przy odczycie.
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": user}],
        }
        if model == self.model_analizy:
            kw["thinking"] = {"type": "adaptive"}
            kw["output_config"]["effort"] = "medium"
        try:
            r = await self._c.messages.create(**kw)
        except Exception as e:                                   # noqa: BLE001
            # Klasyfikacja albo podniesie wlasny wyjatek, albo przepusci
            # oryginalny - patrz `_przeloz_odmowe`.
            self._przeloz_odmowe(e, model)
            raise
        usage = {
            "input": r.usage.input_tokens,
            "output": r.usage.output_tokens,
            "cache_read": getattr(r.usage, "cache_read_input_tokens", 0) or 0,
        }
        if r.stop_reason == "refusal":
            raise Odmowa(usage)
        # ODPOWIEDZ UCIETA NA LIMICIE TOKENOW TO NIE JEST WYNIK.
        #
        # Ten warunek stal po stronie Gemini od poczatku (`finishReason ==
        # MAX_TOKENS`), a tutaj nie bylo go W OGOLE do 2026-09-23. Skutek byl
        # gorszy niz brak wyniku, bo wyjatek wychodzil stad BEZ `usage`:
        #
        #   * sama odpowiedz rozumowania i zaden blok `text` - `next(...)` bez
        #     wartosci domyslnej podnosil `StopIteration`, ktore w funkcji
        #     asynchronicznej zamienia sie w `RuntimeError: coroutine raised
        #     StopIteration` - komunikat nieniosacy zadnej informacji o tym,
        #     co sie stalo,
        #   * ucięty JSON - `json.loads` podnosil `JSONDecodeError`.
        #
        # Zaden z tych wyjatkow nie niesie zuzycia tokenow, wiec `zapisz_nieudane`
        # odpadalo na warunku `if not u or not (...)` i NIE WSTAWIALO wiersza do
        # `news.api_spend`. Spalone ~4000 tokenow wyjscia bylo niewidoczne dla
        # OBU bramek dobowych - dokladnie dla tego wywolania, ktore potrafi sie
        # powtarzac (3 proby na artykul x N artykulow). Nie byl to tez
        # `Przeciazony`, wiec `_zanotuj_odmowe` nigdy sie nie wykonywalo
        # i bezpiecznik nie mial czego zewrzec.
        #
        # Tokeny ROZUMOWANIA licza sie do tego samego `max_tokens`, wiec to nie
        # jest przypadek brzegowy: dluzszy artykul wyczerpuje limit na samym
        # mysleniu, zanim padnie pierwsze slowo odpowiedzi.
        if r.stop_reason == "max_tokens":
            raise BladOdpowiedzi(
                f"odpowiedź ucięta na limicie {max_tokens} tokenów "
                f"(model {model} zużył {usage['output']} na wyjście razem "
                f"z rozumowaniem)", usage)
        tekst = next((b.text for b in r.content
                      if getattr(b, "type", None) == "text"), None)
        if tekst is None or not tekst.strip():
            # Brak bloku `text` przy `stop_reason` innym niz wyzej - np. sama
            # odpowiedz rozumowania. `next(...)` BEZ wartosci domyslnej robil
            # z tego `RuntimeError` w miejscu oddalonym od przyczyny; tu wychodzi
            # nazwany blad Z ZUZYCIEM, ktore da sie zapisac w dzienniku.
            raise BladOdpowiedzi(
                f"pusta odpowiedź modelu {model} "
                f"(stop_reason={r.stop_reason}, bloków {len(r.content)})", usage)
        return json.loads(tekst), usage


class DostawcaGemini(Dostawca):
    """Google Gemini przez REST.

    Bez pakietu SDK: potok ma juz `httpx`, a jedyne, czego tu potrzeba, to jedno
    wywolanie POST. Dodatkowa zaleznosc w obrazie kosztuje wiecej niz te
    trzydziesci linii.
    """

    nazwa = "gemini"
    BAZA = "https://generativelanguage.googleapis.com/v1beta/models"

    # Cennik odczytany z ai.google.dev/gemini-api/docs/pricing 2026-09-07,
    # taryfa platna, USD za milion tokenow.
    CENNIK = {
        "gemini-3.5-flash": (1.50, 9.00),
        "gemini-3.5-flash-lite": (0.30, 2.50),
        "gemini-3.6-flash": (0.75, 3.75),
        "gemini-3.1-flash-lite": (0.25, 1.50),
        "gemini-3.1-pro-preview": (2.00, 12.00),
    }
    # Nieznany model wyceniamy najdrozszym ze znanych, nie najtanszym: pomylka
    # w te strone zatrzymuje potok na limicie dobowym, pomylka w druga strone
    # pozwala mu wydac wielokrotnosc limitu, zanim ktokolwiek to zauwazy.
    CENNIK_DOMYSLNY = (2.00, 12.00)

    # TEMPO ZAPYTAN I PONAWIANIE
    #
    # Klient Anthropica ponawia sam - ten jest napisany na golym httpx i nie
    # ponawial nic. Zmierzone na produkcji 2026-09-07, pierwszy cykl po wpieciu
    # klucza: jedna analiza przeszla, siedem kolejnych zapytan dostalo 429.
    # W logu widnialo "przeanalizowanych 0, bledow 7" i proces szedl dalej -
    # potok wygladal na dzialajacy i milczal, a wiadomosci nie byly analizowane.
    #
    # Dwa mechanizmy, bo rozwiazuja dwie rozne rzeczy. PRZEPUSTNICA rozklada
    # zapytania w czasie, zeby limit nie byl przekraczany; PONAWIANIE ratuje
    # te, ktore mimo tego trafia na limit (inny proces, inny model, zmiana
    # limitu po stronie dostawcy).
    ODSTEP_S = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "4.0"))
    PROBY = 5
    # 429 to limit, nie awaria. 5xx to awaria po stronie dostawcy - zmierzone
    # 2026-09-07 na gemini-3.6-flash: 503 "high demand". Obie sa przejsciowe.
    PONAWIALNE = (429, 500, 502, 503, 504)

    def __init__(self, klucz: str, http: httpx.AsyncClient | None = None):
        self._klucz = klucz
        self._http = http or httpx.AsyncClient(timeout=90.0)
        self._wlasny_http = http is None
        self._ostatnie = 0.0
        self.model_analizy = os.environ.get("ANALYSIS_MODEL_GEMINI", "gemini-3.5-flash")
        self.model_triazu = os.environ.get("TRIAGE_MODEL_GEMINI", "gemini-3.5-flash-lite")

    async def _przepustnica(self) -> None:
        czekaj = self.ODSTEP_S - (time.monotonic() - self._ostatnie)
        if czekaj > 0:
            await asyncio.sleep(czekaj)
        self._ostatnie = time.monotonic()

    @staticmethod
    def _ile_czekac(r: httpx.Response, proba: int) -> float:
        """Ile odczekac przed kolejna proba.

        `Retry-After` od dostawcy ma pierwszenstwo przed naszym zgadywaniem -
        on wie, kiedy okno limitu sie odnowi, my nie.
        """
        naglowek = r.headers.get("Retry-After")
        if naglowek:
            try:
                return min(90.0, max(1.0, float(naglowek)))
            except ValueError:
                pass
        return min(60.0, 5.0 * (2 ** (proba - 1)))

    @staticmethod
    def limit_dobowy(r: httpx.Response) -> bool:
        """Czy 429 dotyczy limitu NA DOBE, a nie tempa zapytan.

        Google zwraca oba jako 429 i rozroznia je dopiero w tresci: pole
        `quotaId` konczy sie `PerDay...` przy limicie dobowym. Rozroznienie ma
        skutek praktyczny: przy limicie tempa ponawianie jest sensowne
        (odstep i tak mija w sekundy), przy dobowym KAZDE ponowienie to czysta
        strata - limit nie minie do nastepnej doby.

        Zmierzone na produkcji 2026-09-08:
          quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier
          quotaValue: 500, model: gemini-3.5-flash-lite

        Ciekawostka, ktora latwo zle zrozumiec: odpowiedz zawiera przy tym
        `retryDelay: 30s`. Ponowienie po 30 sekundach NIE POMOZE - to pole
        opisuje odstep miedzy probami, nie moment odnowienia limitu.
        """
        try:
            tresc = r.json()
        except ValueError:
            return False
        naruszenia = (tresc.get("error", {}).get("details") or [])
        for d in naruszenia:
            for v in d.get("violations", []) or []:
                if "PerDay" in (v.get("quotaId") or ""):
                    return True
        return "PerDay" in (tresc.get("error", {}).get("message") or "")

    # Tresci, po ktorych 400 od Google jest odmowa TRWALA, a nie naszym bledem.
    # 400 dostajemy takze za zly schemat (`additionalProperties`) i ten MUSI
    # zostac glosny - dlatego rozstrzyga tresc, tak samo jak przy `limit_dobowy`.
    ODMOWA_TRWALA = ("api key not valid", "api_key_invalid", "invalid api key",
                     "billing", "billing_disabled", "credit")

    def _odmowa_trwala(self, r: httpx.Response, model: str) -> None:
        """Podnosi `Wyczerpany`, gdy odmowa nie minie sama. Inaczej nic nie robi.

        Bez tego uniewazniony klucz Gemini wracal jako goly `HTTPStatusError`:
        nie `Przeciazony`, wiec mieszanka nie schodzila na zapasowego, wiec
        KAZDY artykul dostawal wlasne wywolanie do dostawcy, ktory na pewno
        odmowi. Ten sam mechanizm, ktory 2026-09-10 spalil 15 tysiecy wywolan
        na wyczerpanym koncie Anthropica.
        """
        if r.status_code not in (400, 401, 403):
            return
        try:
            tresc = json.dumps(r.json(), ensure_ascii=False, default=str).lower()
        except ValueError:
            tresc = r.text.lower()
        if r.status_code == 400 and not any(s in tresc for s in self.ODMOWA_TRWALA):
            return
        powod = ("klucz odrzucony albo rozliczenie wylaczone"
                 if r.status_code != 403 else "brak uprawnien do modelu")
        self.odetnij(self.ZWARCIE_TRWALE_S, f"{powod} (HTTP {r.status_code})")
        raise Wyczerpany(
            f"{self.nazwa}: {powod} (HTTP {r.status_code}, model {model}). "
            f"Ponawianie nie pomoze - wymaga wymiany klucza albo wlaczenia "
            f"rozliczenia w konsoli Google.")

    async def _poslij(self, model: str, body: dict) -> httpx.Response:
        for proba in range(1, self.PROBY + 1):
            await self._przepustnica()
            # KAZDA PROBA JEST ZADANIEM, ktore Google liczy do swojego progu -
            # a dziennik `news.api_spend` dostaje z calej tej petli najwyzej
            # jeden wiersz. Liczymy PRZED wyslaniem, bo zadanie zakonczone
            # timeoutem tez zjada prog, a `post` wtedy rzuca.
            self.zanotuj_zadanie()
            r = await self._http.post(f"{self.BAZA}/{model}:generateContent",
                                      json=body,
                                      headers={"x-goog-api-key": self._klucz})
            if r.status_code not in self.PONAWIALNE:
                self._odmowa_trwala(r, model)
                r.raise_for_status()
                return r
            if r.status_code == 429 and self.limit_dobowy(r):
                # BEZ PONAWIANIA. Piec prob z narastajacym odstepem kosztowalo
                # tu okolo 80 sekund NA ARTYKUL, a limit dobowy i tak nie minie.
                raise Przeciazony(
                    f"wyczerpany limit DOBOWY klucza dla modelu {model} "
                    f"(HTTP 429). Ponawianie nie pomoze - limit odnawia sie "
                    f"dopiero nastepnej doby.", dobowy=True)
            if proba == self.PROBY:
                break
            await asyncio.sleep(self._ile_czekac(r, proba))
        # Wyczerpane proby. Komunikat mowi WPROST, ze to limit, a nie zly klucz -
        # bez tego "429" w logu wyglada jak blad konfiguracji i kieruje szukanie
        # w zla strone.
        raise Przeciazony(
            f"dostawca odrzucił {self.PROBY} kolejnych zapytań o model {model} "
            f"(HTTP {r.status_code}) — to limit tempa albo dobowy limit klucza, "
            f"nie błędny klucz. Odstęp między zapytaniami: {self.ODSTEP_S:.1f} s "
            f"(GEMINI_MIN_INTERVAL_S)")

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        self._sprawdz_dostepnosc()
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _bez_additional_properties(schema),
                "maxOutputTokens": max_tokens,
            },
        }
        r = await self._poslij(model, body)
        d = r.json()
        usage = self._uzycie(d.get("usageMetadata", {}))

        kandydaci = d.get("candidates") or []
        if not kandydaci:
            # Odfiltrowany prompt nie zwraca kandydata w ogole.
            raise Odmowa(usage, "prompt odrzucony przez filtry dostawcy")
        c = kandydaci[0]
        powod = c.get("finishReason")
        if powod in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            raise Odmowa(usage, f"odpowiedź zablokowana: {powod}")
        if powod == "MAX_TOKENS":
            # ODCIETY JSON TO NIE JEST WYNIK. Bez tego warunku `json.loads`
            # rzucalby blad skladni, a w logu widnialoby "cykl przerwany:
            # JSONDecodeError" - komunikat, ktory nie mowi, co sie stalo.
            raise BladOdpowiedzi(
                f"odpowiedź ucięta na limicie {max_tokens} tokenów "
                f"(model {model} zużył {usage['output']} na wyjście razem z rozumowaniem)",
                usage)

        tekst = "".join(p.get("text", "") for p in c.get("content", {}).get("parts", []))
        if not tekst.strip():
            raise BladOdpowiedzi(
                f"pusta odpowiedź modelu {model} (finishReason={powod})", usage)
        return json.loads(tekst), usage

    @staticmethod
    def _uzycie(u: dict) -> dict:
        # TOKENY ROZUMOWANIA SA PLATNE JAK WYJSCIE i u modeli 3.x potrafia byc
        # ich wielokrotnoscia: zmierzone na gemini-3.5-flash 732 tokeny myslenia
        # przy 121 tokenach odpowiedzi. Liczenie samego `candidatesTokenCount`
        # zanizaloby koszt szesciokrotnie, a limit dobowy jest jedynym
        # bezpiecznikiem przed petla, ktora analizuje w kolko to samo.
        return {
            "input": u.get("promptTokenCount", 0),
            "output": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
            "cache_read": u.get("cachedContentTokenCount", 0),
        }

    async def zamknij(self) -> None:
        if self._wlasny_http:
            await self._http.aclose()


class DostawcaMieszany(Dostawca):
    """Triaż u jednego dostawcy, analiza u drugiego.

    DLACZEGO AKURAT TAK, A NIE ODWROTNIE — I DLACZEGO TO NIE JEST SAMA CENA

    Zmierzone na produkcji 2026-09-07, koszt jednego wywołania:

        triaż    gemini-3.5-flash-lite  $0,000213   claude-haiku-4-5  $0,001163
        analiza  gemini-3.5-flash       $0,007809   claude-opus-5     $0,007503

    Triaż to 94% wywołań, więc to on decyduje o rachunku — i tam Gemini jest
    5,5× tańszy. Analiza kosztuje u obu tyle samo, więc o jej przydziale
    decyduje wyłącznie jakość. Mieszanka wychodzi jednocześnie TAŃSZA niż sam
    Anthropic i LEPSZA niż samo Gemini; nie jest to kompromis.

    ALE POWÓD JEST GŁĘBSZY NIŻ CENNIK, i to on wyznacza, czego robić NIE wolno.
    Te dwie role różnią się tym, co psuje ich zmiana:

      * triaż WYBIERA, które wiadomości trafią do analizy. Zmiana modelu zmienia
        więc próbkę — ale nie wstawia uskoku w wartości samej cechy,
      * analiza WYTWARZA wartość cechy `news_sent`. Zmiana modelu wstawia
        w środek szeregu skok, którego kalibracja wagi nie zakłada i którego
        nie widać inaczej niż przez porównanie okresów.

    Dlatego tani dostawca idzie tam, gdzie jest wolumen, a mocny tam, gdzie jest
    POMIAR. Odwrócenie tego oszczędziłoby grosze i zepsuło cechę.

    ZEJŚCIA AWARYJNE (od 2026-09-22 w OBIE strony): odcięty triaż idzie do
    dostawcy analizy, odcięta analiza — do dostawcy triażu. Drugie było przez
    dwa tygodnie zabronione z powodu uskoku w cesze; dlaczego właściciel to
    odwrócił, stoi w `_analiza_zapasowa`. Zejście jest zawsze głośne w logu
    i podpisane modelem w każdym wierszu, więc nie jest ciche.

    Model analizy MUSI być inny niż model triażu — inaczej nie da się
    rozstrzygnąć, do którego dostawcy skierować zapytanie, a milczący wybór
    „pierwszy z brzegu" trafiałby raz tu, raz tam.

    WYMAGANIE: TA INSTANCJA MUSI PRZEŻYĆ WIELE CYKLI

    To nie jest uwaga na marginesie, tylko warunek działania bezpiecznika.
    Bezpiecznik jest STANEM — licznikiem odmów i chwilą, do której dostawcy nie
    pytamy. Instancja tworzona od nowa w każdym cyklu zaczyna ten stan od zera,
    więc zwarcie „na 60 minut" trwa dokładnie tyle, ile cykl.

    Widać to wprost w logu produkcyjnym: komunikat „[llm] triaz: gemini —
    wyczerpany limit DOBOWY; przechodze na anthropic na 60 min" pojawiał się co
    ~6 minut zamiast raz na godzinę. Zwarcie nie przeżywało cyklu, bo
    `wybierz_dostawce` woła `zbuduj_dostawce` przy KAŻDYM cyklu (klucz może
    pojawić się w interfejsie między cyklami i to jest słuszne).

    Wolno to pogodzić na dwa sposoby i oba są poprawne:

      * trzymać jedną instancję poza pętlą cykli i budować nową DOPIERO wtedy,
        gdy zmienią się klucze, albo
      * budować co cykl i przenosić stan przez `przejmij_stan(poprzednia)`.

    W obu wypadkach obowiązuje ta sama granica: stanu NIE WOLNO przenosić, gdy
    zmienił się klucz. Nowy klucz to dokładnie ta sytuacja, w której odcięcie ma
    zniknąć natychmiast — inaczej właściciel doładowuje konto, wpisuje nowy klucz
    i przez sześć godzin nic się nie dzieje.
    """

    nazwa = "mieszany"

    # Ile odmow triazu POD RZAD wystarczy, zeby przestac pytac wyczerpanego
    # dostawce. Dwie, a nie jedna: pojedyncze 429 to zwykly limit tempa, ktory
    # ponowienia zalatwiaja i po ktorym nie ma powodu zmieniac modelu. Dwie
    # z rzedu, KAZDA po pieciu ponowieniach, to juz nie tempo, tylko wyczerpany
    # klucz - i wtedy dalsze proby sa czysta strata czasu.
    PROG_ZWARCIA = 2
    # Po tym czasie probujemy znowu. Limity dostawcow sa OKRESOWE, wiec trwale
    # odciecie zamienialoby chwilowy limit w trwala zmiane modelu triazu -
    # czyli cicha zmiane probki artykulow.
    ZWARCIE_S = 600.0
    # LIMIT DOBOWY TO INNA SYTUACJA i wymaga innej liczby. Przy dziesieciu
    # minutach potok co dziesiec minut odkrywa od nowa, ze limit dobowy jest
    # wyczerpany - przez cala dobe. Godzina daje najwyzej 24 takie sprawdzenia
    # na dobe zamiast 144, a jednoczesnie nie czeka do polnocy: limity Google
    # odnawiaja sie o polnocy czasu pacyficznego, wiec sztywne "do konca doby"
    # liczone lokalnie byloby zle o kilka godzin w jedna albo w druga strone.
    ZWARCIE_DOBOWE_S = 3600.0

    def __init__(self, triaz: Dostawca, analiza: Dostawca):
        for d in (triaz, analiza):
            if not isinstance(d, Dostawca):
                # Bezpiecznik trzyma stan W DOSTAWCY, ktory odmawia. Obiekt
                # spoza tej rodziny nie ma gdzie go trzymac, wiec mieszanka
                # pytalaby go bez konca - czyli dokladnie to, co naprawiamy.
                # Lepiej upasc przy budowie niz po cichu zgubic bezpiecznik.
                raise TypeError(
                    f"{type(d).__name__} nie jest Dostawca - bezpiecznik "
                    f"trzyma stan w dostawcy i nie mialby go gdzie zapisac")
        modele = [triaz.model_triazu, triaz.model_analizy,
                  analiza.model_triazu, analiza.model_analizy]
        if len(set(modele)) != len(modele):
            # Kierowanie idzie PO NAZWIE MODELU (`_dla`), a od 2026-09-22 kazdy
            # z czterech modeli moze byc zadany (zejscia awaryjne w obie
            # strony). Powtorzona nazwa trafialaby raz tu, raz tam.
            raise ValueError(
                f"modele mieszanki musza byc rozne, a sa: {modele} - nie da sie "
                f"rozstrzygnac, do ktorego dostawcy skierowac zapytanie")
        self._triaz = triaz
        self._analiza = analiza
        # Licznik odmow POD RZAD i chwila odciecia siedza w DOSTAWCY, ktory
        # odmawia (`Dostawca._odmowy_pod_rzad`, `Dostawca._niedostepny_do`),
        # a nie tutaj. Wczesniej byly tu i dotyczyly wylacznie triazu - dlatego
        # analiza nie miala zadnego bezpiecznika i wyczerpany kredyt Anthropica
        # palil po jednym wywolaniu na artykul.
        self.model_triazu = triaz.model_triazu
        self.model_analizy = analiza.model_analizy
        self.nazwa = f"{triaz.nazwa}+{analiza.nazwa}"

    def _dla(self, model: str) -> Dostawca:
        """Kierowanie PO NAZWIE MODELU, nie po kolejności wywołań.

        Kolejność byłaby założeniem o tym, jak potok woła dostawcę - a to
        założenie przestaje być prawdziwe przy pierwszej zmianie w potoku,
        po cichu i z zachowaniem poprawnego wyglądu wyniku.
        """
        if model in (self.model_triazu, self._triaz.model_analizy):
            return self._triaz
        if model in (self.model_analizy, self._analiza.model_triazu):
            return self._analiza
        raise ValueError(f"model {model!r} nie nalezy do zadnego z dostawcow "
                         f"mieszanki ({self.model_triazu} / {self.model_analizy})")

    async def zapytaj(self, *, model: str, system: str, user: str,
                      schema: dict, max_tokens: int) -> tuple[dict, dict]:
        # BEZPIECZNIK NA WYCZERPANEGO DOSTAWCE - OBU, NIE TYLKO TRIAZU.
        #
        # Zejscie awaryjne nizej dziala poprawnie, ale wlacza sie DOPIERO po
        # wyczerpaniu piatki ponowien - okolo 80 sekund na artykul. Gdy klucz
        # jest wyczerpany CALKOWICIE, kazde zapytanie konczy sie 429 w 0,2 s,
        # wiec te 80 sekund to czysta kara, placona od nowa przy KAZDYM
        # artykule. Zmierzone na produkcji 2026-09-08: dwie decyzje triazu na
        # piec minut, cykl nie konczyl sie nigdy, puls milczal, a czujka
        # zywotnosci meldowala martwego workera.
        #
        # Po dwoch takich odmowach z rzedu przestajemy pytac wyczerpanego
        # dostawce i idziemy wprost do zapasowego. Po ZWARCIE_S sprobujemy
        # znowu - limity sa okresowe, wiec trwale odciecie zmienialoby chwilowy
        # limit w trwala zmiane modelu triazu.
        #
        # TO SAMO DOTYCZY DOSTAWCY ANALIZY, i tego wlasnie brakowalo do
        # 2026-09-10. Analiza nie ma zejscia awaryjnego, wiec o bezpieczniku dla
        # niej nikt nie pomyslal - i wyczerpany kredyt Anthropica wracal raz na
        # artykul, 52-62 razy w cyklu, bez konca. Analiza nie schodzi na innego
        # dostawce (patrz nizej), ale MA prawo przestac pytac martwego.
        dostawca = self._dla(model)
        if model == self.model_triazu and not dostawca.czy_ma_czym_pytac():
            return await self._triaz_zapasowy(system, user, schema, max_tokens)
        if model == self.model_analizy and not dostawca.czy_ma_czym_pytac():
            return await self._analiza_zapasowa(system, user, schema, max_tokens)
        if not dostawca.czy_ma_czym_pytac():
            # Model zapasowy zadany wprost u odcietego dostawcy - nie ma juz
            # dokad zejsc. Mowimy to WPROST i bez wywolania API.
            raise BrakDostawcy(
                f"analiza niemozliwa: {dostawca.nazwa} — "
                f"{dostawca.powod_niedostepnosci()}")
        try:
            data, usage = await self._zapytaj_u(
                dostawca, model, system=system, user=user, schema=schema,
                max_tokens=max_tokens)
            dostawca._odmowy_pod_rzad = 0
            return data, usage
        except Przeciazony as e:
            self._zanotuj_odmowe(dostawca, e)
            if model == self.model_analizy:
                # ANALIZA SCHODZI NA DOSTAWCE TRIAZU - decyzja wlasciciela
                # z 2026-09-22, odwracajaca poprzednia. Patrz `_analiza_zapasowa`.
                return await self._analiza_zapasowa(system, user, schema, max_tokens)
            if model != self.model_triazu:
                raise
            # TRIAZ WOLNO PRZERZUCIC, bo on WYBIERA artykuly, a nie ocenia je.
            # Zmiana modelu zmienia probke, nie wartosc cechy - a triaz, ktory
            # padl, zatrzymuje CALA analize, co widac wlasnie na produkcji:
            # darmowy klucz Gemini odbil 94% wywolan i potok stanal.
            return await self._triaz_zapasowy(system, user, schema, max_tokens)

    async def _zapytaj_u(self, dostawca: Dostawca, model: str,
                         **kw: Any) -> tuple[dict, dict]:
        """Wywolanie u KONKRETNEGO dostawcy, podpisane jego nazwa i modelem.

        JEDYNE miejsce, w ktorym mieszanka dotyka dostawcy - i dlatego jedyne,
        w ktorym trzeba pamietac o podpisie. Wczesniej `usage["model_uzyty"]`
        ustawialy dwie sciezki osobno (zwykla i zapasowa), a wyjatek nie niosl
        tej informacji w ogole; tedy przeciekal do logu model ZADANY zamiast
        tego, ktory naprawde odmowil.
        """
        try:
            data, usage = await dostawca.zapytaj(model=model, **kw)
        except Exception as e:                               # noqa: BLE001
            oznacz_wykonawce(e, dostawca.nazwa, model)
            # `Odmowa` i `BladOdpowiedzi` NIOSA zuzycie tokenow, ktore trafi do
            # `news.api_spend` (patrz `news_worker.zapisz_nieudane`). Wpis ma
            # nazywac model, ktory te tokeny spalil, bo wedlug jego stawki
            # liczony jest koszt - a na tym dzienniku stoi limit dobowy.
            zuzycie = getattr(e, "usage", None)
            if isinstance(zuzycie, dict):
                zuzycie.setdefault("model_uzyty", model)
            raise
        usage["model_uzyty"] = model
        return data, usage

    def _zanotuj_odmowe(self, dostawca: Dostawca, e: Przeciazony) -> None:
        """Licznik odmow i decyzja o odcieciu - DLA OBU DOSTAWCOW TAK SAMO.

        Do 2026-09-10 ten licznik dotyczyl wylacznie triazu, bo tylko triaz mial
        zejscie awaryjne i tylko o nim myslano przy pisaniu bezpiecznika. Skutek
        byl taki, ze gdy padl dostawca ANALIZY, nie zwieralo sie nic - a przy
        wyczerpanym kredycie kazdy artykul kupowal sobie wlasna odmowe.

        Trzy dlugosci odciecia, bo to sa trzy rozne sytuacje:
          * zwykly limit tempa    -> ZWARCIE_S, po dwoch odmowach pod rzad,
          * limit DOBOWY          -> ZWARCIE_DOBOWE_S, po pierwszej,
          * awaria TRWALA         -> ZWARCIE_TRWALE_S, po pierwszej.
        Awaria trwala i dobowa nie czekaja na druga odmowe, bo nie ma na co
        czekac: obie sa juz rozpoznane po tresci odpowiedzi, nie zgadniete
        z powtorzen.
        """
        dostawca._odmowy_pod_rzad += 1
        trwala = isinstance(e, Wyczerpany)
        prog = 1 if (trwala or e.dobowy) else self.PROG_ZWARCIA
        if dostawca._odmowy_pod_rzad < prog:
            return
        ile_s = (self.ZWARCIE_TRWALE_S if trwala
                 else self.ZWARCIE_DOBOWE_S if e.dobowy else self.ZWARCIE_S)
        powod = ("awaria TRWALA" if trwala
                 else "wyczerpany limit DOBOWY" if e.dobowy
                 else f"odmowy {dostawca._odmowy_pod_rzad} razy pod rzad")
        dostawca.odetnij(ile_s, f"{powod}: {e}")
        # GLOSNO, raz na zwarcie - tu jestesmy tylko wtedy, gdy dostawca byl
        # przed chwila jeszcze pytany. Cicha zmiana modelu triazu znaczy, ze
        # probka artykulow zmienia sie bez sladu, a to widac dopiero w wynikach,
        # tygodnie pozniej. Cicha przerwa w analizie znaczy dziure w cesze.
        if dostawca is self._triaz:
            print(f"[llm] triaz: {dostawca.nazwa} — {powod}; przechodze na "
                  f"{self._analiza.nazwa} na {ile_s / 60:.0f} min",
                  file=sys.stderr, flush=True)
        else:
            print(f"[llm] analiza: {dostawca.nazwa} — {powod}; analiza "
                  f"przechodzi na {self._triaz.nazwa}/{self._triaz.model_analizy} "
                  f"na {ile_s / 60:.0f} min — UWAGA: uskok w cesze news_sent, "
                  f"wiersze sa podpisane modelem (news.article_analysis.model)",
                  file=sys.stderr, flush=True)

    def czy_ma_czym_pytac(self) -> bool:
        """Mieszanka ma czym pytac, dopoki zyje KTORYKOLWIEK dostawca.

        Od 2026-09-22 analiza schodzi na dostawce triazu (`_analiza_zapasowa`),
        a triaz na dostawce analizy (`_triaz_zapasowy`), wiec jeden zywy
        dostawca wystarcza do calego potoku. Do tej daty liczyl sie wylacznie
        dostawca ANALIZY - i pusty rachunek Anthropica zatrzymywal ceche
        `news_sent` na 13 dni, mimo zywego klucza Gemini.

        Uwaga dla wolajacego: False znaczy "nie analizuj w tym cyklu", a NIE
        "przerwij cykl". Artykuly nadal trzeba zapisywac - patrz kontrakt
        w `Dostawca`.
        """
        return self._analiza.czy_ma_czym_pytac() or self._triaz.czy_ma_czym_pytac()

    def powod_niedostepnosci(self) -> str:
        powody = [f"{d.nazwa}: {d.powod_niedostepnosci()}"
                  for d in (self._triaz, self._analiza)
                  if not d.czy_ma_czym_pytac()]
        return "; ".join(powody)

    def zadania_poza_dziennikiem(self) -> int:
        """Nadwyzka ponowien OBU dostawcow razem.

        Mieszanka sama nie wysyla niczego - licza jej skladowe. Rozliczenia
        (`zanotuj_rozliczenie`) przychodza natomiast do NIEJ, bo `Budzet` zna
        tylko obiekt, ktorego pyta potok, i nie wie, ktory dostawca obsluzyl
        dane wywolanie. Odejmowanie musi wiec isc na tym samym poziomie, na
        ktorym sie dolicza - inaczej nadwyzka bylaby liczona dwa razy.
        """
        zadan = self._triaz._zadan_http + self._analiza._zadan_http
        return max(0, zadan - self._zadan_rozliczonych)

    def przejmij_stan(self, poprzednia: "DostawcaMieszany") -> None:
        """Przenosi stan bezpiecznika z instancji z poprzedniego cyklu.

        POWOD ISTNIENIA TEJ METODY. `wybierz_dostawce` buduje dostawce od nowa
        w kazdym cyklu, zeby klucz wpisany w interfejsie zadzialal bez restartu
        poda. Bez przeniesienia stanu odciecie „na 60 minut" trwa jeden cykl,
        czyli piec minut - dokladnie to widac w logu produkcyjnym z 2026-09-10.

        KIEDY NIE WOLNO TEGO WOLAC: gdy zmienil sie ktorykolwiek klucz. Nowy
        klucz to jedyna rzecz, ktora naprawia awarie trwala, wiec przeniesienie
        na niego starego odciecia kazaloby wlascicielowi czekac szesc godzin
        po doladowaniu konta. Kluczy nie widac z tego poziomu - porownanie
        nalezy do wolajacego, ktory je czyta.

        Odrzuca instancje o innym skladzie: przeniesienie stanu miedzy roznymi
        dostawcami odcieloby dostawce, ktory nigdy nie odmowil.
        """
        if (poprzednia.nazwa != self.nazwa
                or poprzednia.model_triazu != self.model_triazu
                or poprzednia.model_analizy != self.model_analizy):
            raise ValueError(
                f"stan bezpiecznika z innej mieszanki ({poprzednia.nazwa}) nie "
                f"pasuje do {self.nazwa} - odcielby dostawce, ktory nie odmowil")
        for nowy, stary in ((self._triaz, poprzednia._triaz),
                            (self._analiza, poprzednia._analiza)):
            nowy._niedostepny_do = stary._niedostepny_do
            nowy._powod_niedostepnosci = stary._powod_niedostepnosci
            nowy._odmowy_pod_rzad = stary._odmowy_pod_rzad
            # LICZNIK ZADAN HTTP IDZIE RAZEM Z BEZPIECZNIKIEM i z tego samego
            # powodu: ta metoda wolno wolac WYLACZNIE przy NIEZMIENIONYCH
            # kluczach, a niezmieniony klucz to ten sam prog dobowy u dostawcy.
            # Zerowanie licznika przy przebudowie obiektu kasowaloby nadwyzke
            # ponowien co cykl - czyli dokladnie to, przed czym ta bramka ma
            # bronic. Nowy klucz nowego licznika NIE dostaje tedy: przy zmianie
            # kluczy `dostawca_na_cykle` buduje obiekt bez `przejmij_stan`.
            nowy._zadan_http = stary._zadan_http
            nowy._zadan_rozliczonych = stary._zadan_rozliczonych
        self._zadan_rozliczonych = poprzednia._zadan_rozliczonych

    def koszt(self, model: str, usage: dict) -> float:
        # CENNIK BIERZE TEN DOSTAWCA, KTORY WYKONAL WYWOLANIE. Wspolna tablica
        # cen dawalaby liczbe wygladajaca poprawnie i bledna - a limit dobowy
        # opiera sie wlasnie na niej. Po zejsciu awaryjnym model z `usage`
        # rozni sie od tego, o ktory prosilismy.
        faktyczny = self.uzyty_model(model, usage)
        return self._dla(faktyczny).koszt(faktyczny, usage)

    def _zwarcie_otwarte(self) -> bool:
        """Czy dostawca TRIAZU jest odciety. Stan trzyma on sam."""
        return not self._triaz.czy_ma_czym_pytac()

    async def _triaz_zapasowy(self, system: str, user: str, schema: dict,
                              max_tokens: int) -> tuple[dict, dict]:
        """TRIAZ WOLNO PRZERZUCIC, bo on WYBIERA artykuly, a nie ocenia ich.

        Zmiana modelu zmienia probke, nie wartosc cechy. Analiza takiego zejscia
        NIE MA i to jest osobna decyzja, opisana wyzej.
        """
        if not self._analiza.czy_ma_czym_pytac():
            # OBAJ ODCIECI. Do 2026-09-10 ta sciezka konczyla sie wywolaniem do
            # martwego dostawcy - raz na artykul, w kolko. Teraz mowi wprost, ze
            # w tym cyklu nie ma czym pytac.
            raise BrakDostawcy(f"obaj dostawcy odcieci — {self.powod_niedostepnosci()}")
        zapasowy = self._analiza.model_triazu
        try:
            data, usage = await self._zapytaj_u(
                self._analiza, zapasowy, system=system, user=user,
                schema=schema, max_tokens=max_tokens)
        except Przeciazony as e:
            # ODMOWA ZAPASOWEGO TEZ SIE LICZY. Bez tego dostawca, ktory odmawia
            # WYLACZNIE na tej sciezce, nie zbieral zadnych odmow i jego
            # bezpiecznik nie zwieral sie nigdy - a wlasnie tedy ida wszystkie
            # zapytania po odcieciu triazu.
            self._zanotuj_odmowe(self._analiza, e)
            raise
        self._analiza._odmowy_pod_rzad = 0
        return data, usage

    async def _analiza_zapasowa(self, system: str, user: str, schema: dict,
                                max_tokens: int) -> tuple[dict, dict]:
        """ANALIZA SCHODZI NA DOSTAWCE TRIAZU. Decyzja wlasciciela z 2026-09-22.

        Do tej daty analiza zejscia NIE MIALA - z uzasadnieniem, ze podmiana
        modelu wstawia w szereg `news_sent` uskok, ktorego kalibracja wagi nie
        zaklada, a brakujacy wiersz przynajmniej widac. Rachunek za to
        wystawilo zycie: pusty kredyt Anthropica od 9 do 22 wrzesnia 2026
        znaczyl 13 dni bez ANI JEDNEJ wartosci cechy przy zywym, darmowym
        kluczu Gemini. Cecha miala wtedy 140 wierszy - nie bylo szeregu, ktory
        uskok moglby zepsuc, byla dziura, ktora rosla. Wlasciciel zdecydowal:
        dziura jest gorsza niz uskok.

        Co zostaje z poprzedniej zasady i CHRONI przed cicha podmiana:
          * kazdy wiersz `news.article_analysis` niesie `model` - trener moze
            odfiltrowac albo porownac okresy,
          * zejscie jest WYKRZYCZANE w logu przy kazdym odcieciu
            (`_zanotuj_odmowe`), nigdy ciche,
          * wraca samo: po `ZWARCIE_*_S` dostawca analizy jest pytany znowu,
            wiec doladowane konto przejmuje analize bez restartu.
        """
        if not self._triaz.czy_ma_czym_pytac():
            raise BrakDostawcy(f"obaj dostawcy odcieci — {self.powod_niedostepnosci()}")
        zapasowy = self._triaz.model_analizy
        try:
            data, usage = await self._zapytaj_u(
                self._triaz, zapasowy, system=system, user=user,
                schema=schema, max_tokens=max_tokens)
        except Przeciazony as e:
            # Odmowa na sciezce zapasowej tez sie liczy - z tego samego powodu
            # co w `_triaz_zapasowy`.
            self._zanotuj_odmowe(self._triaz, e)
            raise
        self._triaz._odmowy_pod_rzad = 0
        return data, usage

    async def zamknij(self) -> None:
        for d in (self._triaz, self._analiza):
            await d.zamknij()


def zbuduj_dostawce(klucze: dict[str, str | None]) -> Dostawca | None:
    """Dostawca z tego, co jest. CZYSTA FUNKCJA — bez bazy i bez srodowiska.

    OBA KLUCZE ZNACZA MIESZANKE, NIE WYBOR. Triaz to 94% wywolan i tam Gemini
    jest 5,5x tanszy; analiza kosztuje u obu tyle samo, wiec o niej decyduje
    jakosc. Wybieranie jednego dostawcy do wszystkiego bylo by w tej sytuacji
    gorsze pod kazdym wzgledem naraz — patrz `DostawcaMieszany`.

    Jeden klucz znaczy jednego dostawce, bez zmiany zachowania.

    UWAGA NA CYKL ZYCIA. Zwrocony obiekt TRZYMA STAN BEZPIECZNIKA (kiedy przestac
    pytac wyczerpanego dostawce). Wolanie tej funkcji w kazdym cyklu i wyrzucanie
    poprzedniego obiektu KASUJE ten stan - zwarcie „na 60 minut" trwa wtedy tyle,
    co cykl, czyli piec minut. Albo trzymaj jedna instancje miedzy cyklami i buduj
    nowa dopiero po zmianie klucza, albo przenies stan przez
    `DostawcaMieszany.przejmij_stan`. Szczegoly i granica („po zmianie klucza NIE
    przenosic") sa w docstringu `DostawcaMieszany`.
    """
    a = klucze.get("anthropic")
    g = klucze.get("gemini")
    if a and g:
        # KOLEJNOSC BUDOWY NIE JEST OBOJETNA. `DostawcaAnthropic` importuje
        # pakiet `anthropic` leniwie, wiec to on moze tu nie wstac - w obrazie
        # zlozonym pod samo Gemini dopisanie drugiego klucza konczy sie
        # ImportError. Zbudowany wczesniej `DostawcaGemini` mial juz wtedy
        # otwarta pule httpx, ktorej nikt nie zamknie: obiekt nie wyjdzie z tej
        # funkcji, wiec `zamknij_dostawce` u wolajacego nie ma czego zamknac.
        # Ten, ktory moze nie wstac, powstaje wiec ZANIM powstanie jakakolwiek
        # pula polaczen.
        analiza = DostawcaAnthropic(a)
        return DostawcaMieszany(triaz=DostawcaGemini(g), analiza=analiza)
    if a:
        return DostawcaAnthropic(a)
    if g:
        return DostawcaGemini(g)
    return None
