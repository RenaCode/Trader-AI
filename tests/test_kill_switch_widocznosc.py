"""Widoczność AKTYWNEGO kill-switcha: zatrzymany system nie może wyglądać na zdrowy.

DLACZEGO OSOBNY PLIK OD `test_killswitch.py`

Tamten plik pilnuje ŁAŃCUCHA: limit → żądanie → wiersz w bazie → wstrzymane
wejścia → ręczne zwolnienie. Cały ten łańcuch działał, a system i tak wyglądał
na zdrowy — bo widoczność stanu to osobne zagadnienie od samego bezpiecznika.
Ten plik nie dotyka bezpiecznika ani polityki zwolnienia; pilnuje wyłącznie
tego, CZY DA SIĘ ZOBACZYĆ, że jest aktywny.

CO ZMIERZONO NA PRODUKCJI, ZANIM TO POWSTAŁO (2026-09-09)

`risk.killswitch_event` id=2, `triggered_at` 03:20, `released_at` PUSTE —
bezpiecznik blokował wejścia czwartą godzinę. Czujka `--co ryzyko` widziała go,
wypisywała „(już zgłoszone — cisza)" i kończyła się KODEM ZERO, więc zadanie
`trader-zywotnosc` meldowało `Complete`. Zatrzymany system był nieodróżnialny od
zdrowego w obu kanałach naraz: w powiadomieniach i w `kubectl get jobs`.

Złożyły się na to dwie NIEZALEŻNE przyczyny — każda w pojedynkę wystarczała:

  1. tłumienie brzmiało „istnieje powiadomienie młodsze niż `triggered_at`",
     a ten warunek raz spełniony jest spełniony NA ZAWSZE, bo `triggered_at`
     się nie zmienia. Nie było więc rytmu przypominania, tylko cisza bez końca;
  2. kod wyjścia opisywał ZDARZENIE („czy właśnie coś wysłaliśmy"), a nie STAN
     („czy handel stoi"), więc milczał dokładnie wtedy, gdy milczał też kanał
     powiadomień.

To ten sam wzorzec, który kosztował ten projekt tygodnie ciszy przy dryfie cech
(patrz `test_dryf.py`): reguła „zgłaszaj ZMIANĘ" nie widzi STANU, który stoi.

CO ZMIERZONO DOBĘ PÓŹNIEJ (2026-09-10) — I DLACZEGO CZĘŚĆ 2 JEST INNA

Naprawa przyczyny 2 brzmiała: kod wyjścia niezerowy przez CAŁY czas blokady.
Rozumowanie było słuszne, skutek — zły. Zadanie chodzi co dziesięć minut, więc
kill-switch aktywny od 2190 minut dał 144 padnięte Joby na dobę, 144 pody
w stanie Error, mail z każdego i aplikację ArgoCD trwale w stanie Degraded.

Wada jest ta sama, którą ten plik tropi w kanale powiadomień, tylko w drugim
kanale: sygnał, który przychodzi zawsze, przestaje cokolwiek znaczyć. Czerwony
Job co dziesięć minut uczy przewijać czerwone Joby — także ten jeden, w którym
czujka naprawdę padła.

Kod wyjścia jest kanałem o JEDNYM BICIE i z natury niesie „czy ten przebieg
się udał". Opis STANU się w nim nie mieści — ani wtedy, gdy bit jest wolny
(runda 1, stan przepadał), ani wtedy, gdy jest zatkany na stałe (runda 2, stan
przestał być czytany). Opis stanu ma własny, nieograniczony kanał: LOG.

Dzisiejsza reguła, jedna dla wszystkich czujek w `dozor.py`:

    kod wyjścia niezerowy  <=>  czujka W TYM PRZEBIEGU zgłosiła coś NOWEGO

czyli kod wyjścia jest SPÓJNY z tłumieniem powiadomień, a nie niezależny od
niego. Argument „powiadomienie da się przespać i zgubić" zostaje w mocy
i dlatego każde PRZYPOMNIENIE nadal podnosi kod wyjścia — raz na dobę zamiast
144 razy. Stan jest przy tym wypisywany w logu przy KAŻDYM przebiegu; cisza
w logu byłaby tu błędem gorszym niż obie poprzednie rundy razem.

Część 1 tego pliku pilnuje rytmu powiadomień i jest nietknięta. Część 2 pilnuje
kodu wyjścia i mierzy dziś odwrotność tego, co mierzyła — razem z liczbą, przez
którą to się zmieniło.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.engine.dozor import (
    KS_PRZYPOMNIENIE_DLUGIE_H,
    KS_PRZYPOMNIENIE_H,
    KS_RYTM_ROSNIE_PO_H,
    ocen_kill_switch,
    odstep_przypomnienia_ks,
    sprawdz_ryzyko,
)

# Okres zadania `trader-zywotnosc` (charts/trader/values.yaml, zywotnoscCron).
# Rozdzielczosc czasowa calej czujki: rzadziej niz co tyle minut nic sie tu nie
# zdarza, a czesciej niz co tyle nie da sie niczego zglosic.
PRZEBIEG_MIN = 10


# --- 1. przyczyna pierwsza: cisza bez końca ---------------------------------
def test_nowe_zdarzenie_idzie_NATYCHMIAST():
    """Rytm nie może opóźniać pierwszego zgłoszenia — to jedyny moment, w którym
    powiadomienie niesie informację, a nie przypomnienie."""
    w = ocen_kill_switch(wiek_min=1, od_zgloszenia_min=None)
    assert w is not None and w["pierwsze"] is True


def test_w_oknie_rytmu_kanal_powiadomien_milczy():
    """Zadanie chodzi co 10 minut. Bez własnego tłumienia byłoby sześć
    powiadomień na godzinę o rzeczy, która się nie zmienia — a alarm, który
    przychodzi zawsze, przestaje cokolwiek znaczyć."""
    assert ocen_kill_switch(wiek_min=240, od_zgloszenia_min=PRZEBIEG_MIN) is None
    assert ocen_kill_switch(wiek_min=240,
                            od_zgloszenia_min=KS_PRZYPOMNIENIE_H * 60 - 1) is None


def test_po_uplywie_odstepu_przypomnienie_JEDNAK_idzie():
    """KANAREK NA CAŁY SENS TEJ ZMIANY.

    Poprzednia wersja nie miała tu żadnej granicy: warunkiem ciszy było samo
    istnienie wcześniejszego powiadomienia, więc ta asercja była nie do
    spełnienia niezależnie od tego, ile czasu minęło.
    """
    w = ocen_kill_switch(wiek_min=400, od_zgloszenia_min=KS_PRZYPOMNIENIE_H * 60)
    assert w is not None and w["pierwsze"] is False


def test_rytm_ROSNIE_z_wiekiem_zdarzenia():
    """Przez pierwszą dobę pytanie brzmi „czy ktoś to zobaczył", później już
    tylko „czy ktoś o tym pamięta" — i to jest inna częstotliwość."""
    assert odstep_przypomnienia_ks(0) == KS_PRZYPOMNIENIE_H
    assert odstep_przypomnienia_ks(KS_RYTM_ROSNIE_PO_H * 60 - 1) == KS_PRZYPOMNIENIE_H
    assert odstep_przypomnienia_ks(KS_RYTM_ROSNIE_PO_H * 60) == KS_PRZYPOMNIENIE_DLUGIE_H
    assert KS_PRZYPOMNIENIE_DLUGIE_H > KS_PRZYPOMNIENIE_H, \
        "rytm malejący z czasem uczyłby odklikiwać alarm o zatrzymanym systemie"


def test_pierwsze_przypomnienie_nie_budzi_dwa_razy_tej_samej_nocy():
    """Właściciel ma nie klikać w nocy, więc rozwiązaniem nie jest częstsze
    pukanie. Sześć godzin to najkrótszy odstęp, który mieści się w jednej nocy
    najwyżej raz, a zdarzeniu ze środka nocy daje przypomnienie nad ranem."""
    assert KS_PRZYPOMNIENIE_H >= 6


def _symuluj(godzin: int) -> list[float]:
    """Kiedy poszłyby powiadomienia, gdyby zadanie chodziło co 10 minut.

    Zwraca godziny (od zadziałania) wysłanych powiadomień. Symulacja, a nie
    pojedynczy odczyt, bo pytanie brzmi „ile ich będzie na dobę" — i to jest
    liczba, która decyduje o tym, czy odbiorca nauczy się je pomijać.
    """
    wyslane: list[float] = []
    ostatnie_min: float | None = None
    for krok in range(0, godzin * 60 // PRZEBIEG_MIN + 1):
        wiek = krok * PRZEBIEG_MIN
        od = None if ostatnie_min is None else wiek - ostatnie_min
        if ocen_kill_switch(wiek_min=wiek, od_zgloszenia_min=od) is not None:
            wyslane.append(wiek / 60)
            ostatnie_min = wiek
    return wyslane


def test_pierwsza_doba_to_kilka_powiadomien_a_nie_sto_czterdziesci_cztery():
    """Zadanie chodzi 144 razy na dobę. Tyle samo alarmów to ten sam skutek co
    cisza: odbiorca uczy się je pomijać."""
    doba = _symuluj(24)
    assert doba[0] == 0.0, "nowe zdarzenie musi iść natychmiast"
    assert 2 <= len(doba) <= 6, f"na dobę wyszłoby {len(doba)} powiadomień: {doba}"


def test_po_tygodniu_czujka_NADAL_sie_odzywa():
    """KANAREK NA BŁĄD, KTÓRY TU BYŁ: cisza bez końca.

    Stan, który stoi, nie jest zmianą — więc reguła „zgłaszaj zmianę" milczy
    o nim na zawsze. Tydzień zatrzymanego handlu bez ani jednego przypomnienia
    to dokładnie ta cisza, nieodróżnialna od spokoju.
    """
    tydzien = _symuluj(24 * 7)
    assert len(tydzien) >= 8, f"po tygodniu tylko {len(tydzien)} zgłoszeń"
    # Zadna przerwa dluzsza niz doba plus jeden przebieg: przypomnienie
    # o zamrozonym kapitale ma przychodzic codziennie, tak jak przy delistingu.
    przerwy = [b - a for a, b in zip(tydzien, tydzien[1:])]
    assert max(przerwy) <= KS_PRZYPOMNIENIE_DLUGIE_H + PRZEBIEG_MIN / 60, \
        f"przerwa {max(przerwy):.1f} h — czujka zamilkła na dłużej niż dobę"


# --- 2. dwa kanaly, dwa pytania: log o STANIE, kod wyjscia o ZDARZENIU -------
#
# Ta czesc przeszla dwie rundy poprawek w przeciwnych kierunkach (patrz
# docstring modulu) i dlatego kazdy test ponizej ma powiedziec nie tylko CO ma
# byc, ale i KTORY z dwoch kanalow za to odpowiada. Testy ida cala sciezka
# czujki, bo oba sygnaly powstaja na jej koncu, a nie w zadnej czystej funkcji.

TERAZ = datetime(2026, 9, 9, 7, 20, tzinfo=timezone.utc)


class AtrapaBazy:
    """Minimalna atrapa: rozpoznaje zapytania po fragmencie, nie udaje bazy.

    `pomiar_wiek_s` / `pomiar_nigdy` opisuja stan `risk.limit_usage` widziany
    przez czujke ciszy pomiaru. Domyslnie pomiar jest SWIEZY i kompletny, zeby
    testy kill-switcha mierzyly kill-switcha, a nie druga czujke w tym samym
    przebiegu.
    """

    def __init__(self, zdarzenia, od_zgloszenia_min=None, odbiorcy=(1, 2),
                 pomiar_wiek_s=120, pomiar_nigdy=None,
                 od_zgloszenia_pomiar_min=None, zgloszenia=None, teraz=TERAZ):
        self.zdarzenia = zdarzenia
        self.od_zgloszenia_min = od_zgloszenia_min
        self.odbiorcy = odbiorcy
        self.pomiar_wiek_s = pomiar_wiek_s
        self.pomiar_nigdy = pomiar_nigdy
        self.od_zgloszenia_pomiar_min = od_zgloszenia_pomiar_min
        # HISTORIA POWIADOMIEN JAKO DATY, a nie jako gotowa odpowiedz.
        #
        # `od_zgloszenia_min` wystarcza do mierzenia rytmu, ale nie da sie nim
        # zbudowac NAWROTU: pytanie brzmi wtedy nie „jak dawno", tylko „czy
        # tamto zgloszenie w ogole dotyczylo TEGO zdarzenia". Odpowiada na nie
        # granica `created_at > k.triggered_at` w zapytaniu czujki, wiec atrapa
        # musi ja odtworzyc, zamiast oddawac liczbe niezaleznie od parametrow.
        self.zgloszenia = zgloszenia
        self.teraz = teraz
        self.zapytania: list[str] = []

    async def fetch_all(self, sql, params=None):
        self.zapytania.append(sql)
        if "risk.killswitch_event" in sql:
            return list(self.zdarzenia)
        if "risk.limit_usage" in sql:
            return [{"engine": "crypto", "mode": "demo",
                     "wiek_s": self.pomiar_wiek_s, "nigdy": self.pomiar_nigdy,
                     "status": "running", "puls_silnika_min": 1,
                     "puls_dozorcy_min": 0}]
        if "notify.notification" in sql:
            # Agregat po zbiorze pustym oddaje wiersz z NULL-em, a nie zero
            # wierszy - i czujka musi umiec odczytac wlasnie to.
            if "risk.pomiar_zamilkl" in sql:
                return [{"od_zgloszenia_min": self.od_zgloszenia_pomiar_min}]
            if self.zgloszenia is not None:
                po = params[1]
                pasujace = [d for d in self.zgloszenia if d > po]
                return [{"od_zgloszenia_min": (
                    None if not pasujace
                    else (self.teraz - max(pasujace)).total_seconds() / 60)}]
            return [{"od_zgloszenia_min": self.od_zgloszenia_min}]
        if 'app."user"' in sql:
            return [{"id": u} for u in self.odbiorcy]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")


def zdarzenie(wiek_min: float, **kw) -> dict:
    return {"id": 2, "engine": "crypto", "mode": "demo",
            "reason": "Kapitał 100063.09 spadł do progu 100563.00",
            "triggered_at": TERAZ, "wiek_min": wiek_min, **kw}


def uruchom(atrapa, **kw):
    """Zwraca (kod wyjścia, lista wysłanych powiadomień)."""
    wyslane = []

    async def notify(user_id, code, tytul, tresc, **reszta):
        wyslane.append({"user_id": user_id, "code": code, "tytul": tytul,
                        "tresc": tresc, **reszta})

    kod = asyncio.run(sprawdz_ryzyko(atrapa.fetch_all, notify, **kw))
    return kod, wyslane


def test_stan_ZNANY_i_zgloszony_NIE_maluje_zadania_na_czerwono(capsys):
    """KANAREK NA POMIAR Z PRODUKCJI 2026-09-10.

    Bezpiecznik aktywny od 2190 minut, zgłoszony 1100 minut temu, przypomnienie
    co 24 h — więc kanał powiadomień milczy z rytmu. Dokładnie w tej sytuacji
    zadanie padało, i to co dziesięć minut: 144 padnięte Joby na dobę, 144 pody
    w stanie Error, mail z każdego i ArgoCD trwale Degraded.

    Alarm bez nowej informacji nie jest alarmem. Powtórzony 144 razy uczy
    przewijać całą kategorię — także ten jeden Job, w którym czujka padła
    naprawdę.
    """
    a = AtrapaBazy([zdarzenie(2190)], od_zgloszenia_min=1100)
    kod, wyslane = uruchom(a)
    assert wyslane == [], "to nie jest moment na powiadomienie — rytm trwa"
    assert kod == 0, (
        "nic nowego się nie stało, a zadanie pada — przy stanie trwającym "
        "półtorej doby to 144 padnięte Joby dziennie o jednej znanej rzeczy")


def test_ale_STAN_zostaje_WYPISANY_przy_kazdym_przebiegu(capsys):
    """Zero z tej czujki nie ma prawa być nieme.

    Zadanie zielone I log bez śladu to stan nieodróżnialny od spokoju w OBU
    kanałach naraz — czyli gorzej niż przed obiema rundami poprawek. Cisza
    dotyczy wyłącznie kodu wyjścia i niczego więcej.
    """
    a = AtrapaBazy([zdarzenie(2190)], od_zgloszenia_min=1100)
    uruchom(a)
    log = capsys.readouterr().out
    assert "KILL-SWITCH crypto/demo" in log, \
        "zatrzymany handel nie zostawia śladu w logu zielonego zadania"
    assert "2190 min temu" in log, "log nie mówi, jak długo handel stoi"
    assert "kill-switch aktywny" in log, \
        "log nie mówi, DLACZEGO kod wyjścia jest zerem mimo znaleziska"


def test_PRZYPOMNIENIE_jednak_maluje_zadanie_na_czerwono():
    """Argument „powiadomienie da się przespać i zgubić" zostaje w mocy.

    Drugi, niezależny od kanału powiadomień ślad jest wart tyle samo co
    wcześniej — ale wart jest go ZGŁOSZENIE, nie każdy z 144 przebiegów doby.
    Po dobie bez zwolnienia idzie przypomnienie i razem z nim czerwony Job:
    jeden na dobę, czyli tyle, ile jest zdarzeń do zauważenia.
    """
    a = AtrapaBazy([zdarzenie(30 * 60)],
                   od_zgloszenia_min=KS_PRZYPOMNIENIE_DLUGIE_H * 60 + 5)
    kod, wyslane = uruchom(a)
    assert wyslane, "po dobie bez zwolnienia przypomnienie musi pójść"
    assert kod == 1, "poszło powiadomienie, a monitoring klastra tego nie widzi"


def test_kod_wyjscia_niezerowy_takze_w_trybie_suchym():
    """`--sucho` wyłącza WYSYŁKĘ, a nie werdykt.

    Ścieżka alarmowa wywołana na żądanie ma dać ten sam kod wyjścia co
    prawdziwa, inaczej nie da się jej sprawdzić inaczej niż na produkcji.
    """
    a = AtrapaBazy([zdarzenie(237)], od_zgloszenia_min=None)
    kod, wyslane = uruchom(a, sucho=True)
    assert (kod, wyslane) == (1, [])


def test_ILE_CZERWONYCH_JOBOW_NA_DOBE_przy_stanie_ktory_stoi():
    """Liczba, przez którą runda 2 upadła — mierzona wprost, a nie z opisu.

    Zadanie chodzi 144 razy na dobę. Symulacja liczy, ile z tych przebiegów
    kończy się kodem niezerowym przy kill-switchu, którego nikt nie zwalnia.
    Górna granica to nie estetyka: przy `backoffLimit: 0` każdy taki przebieg
    zostawia poda w stanie Error, mail z monitoringu i aplikację ArgoCD
    w stanie Degraded.

    Dolna granica jest równie ważna i pilnuje rundy 1: zero czerwonych Jobów na
    dobę znaczyłoby, że zatrzymany system melduje zdrowie bez przerwy.
    """
    czerwone, ostatnie_min = 0, None
    for krok in range(24 * 60 // PRZEBIEG_MIN):
        wiek = 48 * 60 + krok * PRZEBIEG_MIN        # druga doba blokady
        od = None if ostatnie_min is None else wiek - ostatnie_min
        a = AtrapaBazy([zdarzenie(wiek)], od_zgloszenia_min=od)
        kod, wyslane = uruchom(a)
        if wyslane:
            ostatnie_min = wiek
        czerwone += 1 if kod else 0
    assert 1 <= czerwone <= 3, (
        f"{czerwone} padniętych Jobów na dobę o jednym znanym zdarzeniu")


def test_brak_aktywnego_zdarzenia_to_ZERO():
    """Czujka, która zawsze kończy się porażką, jest tak samo bezużyteczna jak
    ta, która zawsze kończy się powodzeniem."""
    a = AtrapaBazy([])
    assert uruchom(a) == (0, [])


def test_czujka_pyta_o_WIEK_ostatniego_zgloszenia_a_nie_o_jego_ISTNIENIE():
    """Sedno pierwszej przyczyny.

    `SELECT 1 ... LIMIT 1` odpowiada na pytanie „czy kiedykolwiek zgłoszono",
    a to jest pytanie, na które po pierwszym powiadomieniu odpowiedź brzmi TAK
    już na zawsze. Rytm wymaga pytania „jak dawno".
    """
    a = AtrapaBazy([zdarzenie(237)], od_zgloszenia_min=60)
    uruchom(a)
    # WYRAZNIE o zapytanie kill-switcha: od czasu dolozenia czujki ciszy pomiaru
    # przebieg zadaje DWA pytania do notify.notification i `[0]` trafialoby
    # w to drugie - test przechodzilby, mierzac nie to, co opisuje.
    pytanie = [s for s in a.zapytania
               if "notify.notification" in s and "killswitch.triggered" in s]
    assert pytanie, "czujka nie pyta o historię powiadomień o kill-switchu"
    assert "max(created_at)" in pytanie[0], \
        "bez wieku ostatniego zgłoszenia rytm przypominania nie ma z czego wyjść"


def test_pierwsze_zgloszenie_idzie_do_WSZYSTKICH_i_nie_udaje_przypomnienia():
    a = AtrapaBazy([zdarzenie(3)], od_zgloszenia_min=None)
    kod, wyslane = uruchom(a)
    assert kod == 1
    assert [w["code"] for w in wyslane] == ["killswitch.triggered"] * 2
    assert wyslane[0]["severity"] == "critical"
    assert wyslane[0]["data"]["przypomnienie"] is False
    assert "NADAL" not in wyslane[0]["tytul"]


def test_przypomnienie_PRZEDSTAWIA_SIE_jako_przypomnienie():
    """Powtórka nieodróżnialna od nowego alarmu każe odbiorcy sprawdzać od zera,
    czy to nie druga awaria — a przy trzeciej powtórce nauczy go nie sprawdzać
    wcale."""
    a = AtrapaBazy([zdarzenie(30 * 60)],
                   od_zgloszenia_min=KS_PRZYPOMNIENIE_DLUGIE_H * 60 + 5)
    kod, wyslane = uruchom(a)
    assert kod == 1
    assert wyslane, "po dobie bez zwolnienia przypomnienie musi pójść"
    assert wyslane[0]["data"]["przypomnienie"] is True
    assert "NADAL" in wyslane[0]["tytul"]
    assert "30 h" in wyslane[0]["tytul"], "przypomnienie nie mówi, jak długo stoi"
    assert "PRZYPOMNIENIE" in wyslane[0]["tresc"]


@pytest.mark.parametrize("wiek_h,od_h,ma_pojsc", [
    (2, KS_PRZYPOMNIENIE_H - 1, False),          # mlode zdarzenie, rytm trwa
    (7, KS_PRZYPOMNIENIE_H, True),               # mlode zdarzenie, rytm minal
    (30, KS_PRZYPOMNIENIE_H, False),             # stare: obowiazuje rytm dobowy
    (30, KS_PRZYPOMNIENIE_DLUGIE_H, True),
])
def test_caly_przebieg_trzyma_sie_rytmu(wiek_h, od_h, ma_pojsc):
    """Rytm ma działać w czujce, a nie tylko w czystej funkcji: werdykt
    rozłączony od wysyłki jest równie cichy jak jego brak.

    I TEN SAM RYTM RZĄDZI KODEM WYJŚCIA. Dwa kanały rozstrzygane osobno to dwa
    mechanizmy do rozjechania — a rozjazd byłby widoczny wyłącznie dla kogoś,
    kto porównałby `kubectl get jobs` z historią powiadomień.
    """
    a = AtrapaBazy([zdarzenie(wiek_h * 60)], od_zgloszenia_min=od_h * 60)
    kod, wyslane = uruchom(a)
    assert bool(wyslane) is ma_pojsc
    assert bool(kod) is ma_pojsc, \
        "kod wyjścia rozjechał się z tłumieniem powiadomień"


# --- 3. nawrót: zwolniony, a potem wystrzelił znowu --------------------------
#
# Odkad kod wyjscia idzie za tlumieniem powiadomien, KAZDA dziura w tlumieniu
# jest dziura w obu kanalach naraz. Dopoki kod wyjscia byl niezalezny (runda 2),
# taka dziura byla zamaskowana - zadanie i tak swiecilo sie na czerwono, wiec
# nikt by jej nie zauwazyl. Dlatego rozroznienie „ten sam epizod" od „nowy
# epizod" przestalo byc ozdoba i ma tu wlasne testy.

def test_NAWROT_po_zwolnieniu_to_zdarzenie_NOWE():
    """Kill-switch zwolniony ręcznie, a potem wystrzelił drugi raz.

    To jest INNA strata z INNEGO powodu i musi obudzić tak samo jak pierwsza.
    Gdyby tłumienie pytało tylko „czy kiedykolwiek zgłoszono kill-switcha na
    tym silniku", przypomnienie sprzed dziesięciu godzin uciszyłoby CUDZE
    zdarzenie — w powiadomieniach i w `kubectl get jobs` naraz.

    Rozróżnienie jest za darmo i bez drugiego mechanizmu: nowy epizod to nowy
    wiersz w `risk.killswitch_event` (`_trigger_killswitch` dopisuje, nigdy nie
    odświeża starego), a zapytanie o historię powiadomień jest ograniczone jego
    `triggered_at`. Zgłoszenia poprzedniego epizodu są od niego starsze, więc
    nie pasują.
    """
    poprzedni_epizod = TERAZ - timedelta(hours=10)
    a = AtrapaBazy([zdarzenie(5, triggered_at=TERAZ - timedelta(minutes=5))],
                   zgloszenia=[poprzedni_epizod])
    kod, wyslane = uruchom(a)
    assert kod == 1, "drugi kill-switch uciszony przez alarm o pierwszym"
    assert wyslane, "brak powiadomienia o NOWYM zatrzymaniu handlu"
    assert wyslane[0]["data"]["przypomnienie"] is False, \
        "nowe zdarzenie przedstawia się jako przypomnienie o poprzednim"


def test_ten_SAM_epizod_zgloszony_po_zadzialaniu_juz_nie_budzi():
    """Druga strona tego samego warunku — inaczej „nawrót" znaczyłby „zawsze".

    Zgłoszenie młodsze od `triggered_at` dotyczy TEGO epizodu, więc obowiązuje
    rytm: cisza w kanale powiadomień i zero w kodzie wyjścia.
    """
    a = AtrapaBazy([zdarzenie(300, triggered_at=TERAZ - timedelta(hours=5))],
                   zgloszenia=[TERAZ - timedelta(hours=4)])
    kod, wyslane = uruchom(a)
    assert (kod, wyslane) == (0, [])


def test_zwolnienie_pozostaje_RECZNE_i_nietkniete():
    """Ta zmiana dotyczy WYŁĄCZNIE widoczności stanu.

    Automatyczne wznowienie po obsunięciu to najprostsza droga do powtórzenia
    tej samej straty, więc `min_equity` ma regułę ręczną — i czujka nie ma
    prawa niczego tu skracać.
    """
    zrodlo = Path("services/engine/dozor.py").read_text()
    cialo = zrodlo[zrodlo.index("async def sprawdz_ryzyko"):]
    cialo = cialo[:cialo.index("\n# Ile moze minac od ostatniej udanej kopii")]
    assert "WHERE k.released_at IS NULL" in cialo, \
        "czujka przestała pomijać zdarzenia zwolnione ręcznie"
    for zakazane in ("UPDATE", "INSERT", "DELETE"):
        assert zakazane not in cialo, \
            f"czujka pisze do bazy ({zakazane}) — ma tylko patrzeć"
