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
Każdy test poniżej trzyma jedną z tych przyczyn zamkniętą.
"""
import asyncio
from datetime import datetime, timezone
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


# --- 2. przyczyna druga: kod wyjścia opisywał zdarzenie, nie stan ------------
#
# Ta wada jest osobna od samego powiadomienia i wazniejsza: widzi ja monitoring
# klastra, a nie tylko czlowiek z telefonem. Testy ponizej ida cala sciezka
# czujki, bo kod wyjscia powstaje na jej koncu, a nie w zadnej czystej funkcji.

TERAZ = datetime(2026, 9, 9, 7, 20, tzinfo=timezone.utc)


class AtrapaBazy:
    """Minimalna atrapa: rozpoznaje zapytania po fragmencie, nie udaje bazy."""

    def __init__(self, zdarzenia, od_zgloszenia_min=None, odbiorcy=(1, 2)):
        self.zdarzenia = zdarzenia
        self.od_zgloszenia_min = od_zgloszenia_min
        self.odbiorcy = odbiorcy
        self.zapytania: list[str] = []

    async def fetch_all(self, sql, params=None):
        self.zapytania.append(sql)
        if "risk.killswitch_event" in sql:
            return list(self.zdarzenia)
        if "notify.notification" in sql:
            # Agregat po zbiorze pustym oddaje wiersz z NULL-em, a nie zero
            # wierszy - i czujka musi umiec odczytac wlasnie to.
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


def test_kod_wyjscia_NIEZEROWY_takze_gdy_powiadomienie_milczy():
    """KANAREK NA POMIAR Z PRODUKCJI.

    Bezpiecznik aktywny od 237 minut, zgłoszony wcześniej, więc kanał
    powiadomień milczy z rytmu — i dokładnie w tej sytuacji zadanie kończyło się
    POWODZENIEM. Zatrzymany system meldował zdrowie w `kubectl get jobs`.
    """
    a = AtrapaBazy([zdarzenie(237)], od_zgloszenia_min=60)
    kod, wyslane = uruchom(a)
    assert wyslane == [], "to nie jest moment na powiadomienie — rytm trwa"
    assert kod == 1, "zatrzymany handel, a zadanie kończy się powodzeniem"


def test_kod_wyjscia_niezerowy_takze_po_dobach_ciszy():
    """Stan zatrzymany nie ma daty ważności: ani po godzinie, ani po dobie
    zadanie nie może zzielenieć samo."""
    a = AtrapaBazy([zdarzenie(72 * 60)], od_zgloszenia_min=60)
    kod, wyslane = uruchom(a)
    assert (kod, wyslane) == (1, [])


def test_kod_wyjscia_niezerowy_takze_w_trybie_suchym():
    """`--sucho` wyłącza wysyłkę, a nie stan systemu."""
    a = AtrapaBazy([zdarzenie(237)], od_zgloszenia_min=None)
    kod, wyslane = uruchom(a, sucho=True)
    assert (kod, wyslane) == (1, [])


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
    pytanie = [s for s in a.zapytania if "notify.notification" in s]
    assert pytanie, "czujka nie pyta o historię powiadomień"
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
    rozłączony od wysyłki jest równie cichy jak jego brak."""
    a = AtrapaBazy([zdarzenie(wiek_h * 60)], od_zgloszenia_min=od_h * 60)
    kod, wyslane = uruchom(a)
    assert kod == 1, "aktywny bezpiecznik zawsze maluje zadanie na czerwono"
    assert bool(wyslane) is ma_pojsc


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
