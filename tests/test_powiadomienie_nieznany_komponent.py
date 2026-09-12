"""Alarm o komponencie, którego nie zna enum `ref.engine`, MA DOJŚĆ.

DLACZEGO TO JEST OSOBNY PLIK

`notify.notification.engine` ma typ `ref.engine` — enum SILNIKÓW HANDLOWYCH
('crypto','equity'). Nie ma w nim dozorcy ryzyka ani workerów danych i celowo
mieć nie będzie: migracje 0029, 0034 i 0035 wyprowadziły ich puls do osobnych
tabel właśnie po to, żeby nie dopisywać do enuma czegoś, co nie handluje.

Ale każdy z tych procesów może umrzeć i o śmierci każdego trzeba powiadomić.
Do 2026-09-12 wstawienie takiej nazwy do kolumny `engine` kończyło CAŁY
`notify()` wyjątkiem `InvalidTextRepresentation` — czyli powiadomienie o awarii
ginęło z powodu literówki we własnych metadanych.

Zmierzone na produkcji (Job trader-zywotnosc-29819810, 2026-09-12): czujka
POPRAWNIE wykryła, że dozorca ryzyka nie bije od 887 minut, i padła na zapisie
powiadomienia. Nikt się nie dowiedział. Detekcja działała, dostawa nie —
a alarm, który nie doszedł, jest gorszy niż brak czujki, bo tworzy złudzenie
nadzoru.

`services/engine/dozor.py` naprawia to u siebie (nazwa procesu idzie w `data`,
kolumna zostaje pusta) i tego pilnuje `tests/test_dozor.py`. Ten plik pilnuje
warstwy niżej: że KAŻDY przyszły wołający — nie tylko ten jeden, dziś
poprawiony — dostanie dostarczony alert zamiast wyjątku.

ATRAPA ZNA TYP KOLUMNY. Wartości enuma czyta z migracji 0001, a nie przepisuje
ich tutaj: druga kopia rozjechałaby się z bazą przy pierwszym rozszerzeniu typu
i cisza tego pliku przestałaby cokolwiek znaczyć.
"""
import asyncio
import json
import re
from pathlib import Path

import psycopg
import pytest

from services.api import notifications


def _enum_silnikow() -> set[str]:
    sql = Path("db/migrations/0001_extensions_and_reference.sql").read_text()
    blok = re.search(r"CREATE TYPE ref\.engine\s+AS ENUM\s*\(([^)]*)\)", sql)
    assert blok, "nie znaleziono definicji ref.engine w migracji 0001"
    wartosci = set(re.findall(r"'([^']+)'", blok.group(1)))
    assert wartosci, "definicja ref.engine sparsowana na pusto"
    return wartosci


class AtrapaBazy:
    """Tyle Postgresa, ile trzeba: typ kolumny `engine` i nic poza tym."""

    def __init__(self, *, kanaly=None, always_on=True):
        self.silniki = _enum_silnikow()
        self.kanaly = kanaly or []
        self.always_on = always_on
        self.wstawione = []            # wiersze, ktore FAKTYCZNIE weszly
        self.proby = 0                 # ile razy sprobowano INSERT-a
        self.dostawy = []

    async def fetch_one(self, sql, params=None):
        if "notify.event_type" in sql:
            return {"code": params[0], "default_severity": "critical",
                    "always_on": self.always_on, "default_cooldown_s": 0,
                    "label": "test"}
        if "notify.preference" in sql:
            return None
        if "INSERT INTO notify.notification" in sql:
            self.proby += 1
            *_, dane, engine = params
            if engine is not None and engine not in self.silniki:
                raise psycopg.errors.InvalidTextRepresentation(
                    f'invalid input value for enum ref.engine: "{engine}"')
            wiersz = {"severity": params[2], "title": params[3],
                      "body": params[4], "data": json.loads(dane),
                      "engine": engine}
            self.wstawione.append(wiersz)
            return {"id": 7}
        if "notify.settings" in sql:
            return None
        if "notify.notification" in sql:        # sprawdzenie czestotliwosci
            return None
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")

    async def fetch_all(self, sql, params=None):
        if "notify.channel" in sql:
            return self.kanaly
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")

    async def execute(self, sql, params=None):
        if "notify.delivery" in sql:
            self.dostawy.append(params)


class FalszywySekret:
    """`notify()` tworzy SecretBox zawsze, także bez kanałów — a ten wymaga
    SECRETS_KEY, którego zestaw testowy nie ma i mieć nie powinien."""

    def decrypt(self, _):
        return b"{}"


def _podepnij(monkeypatch, baza):
    monkeypatch.setattr(notifications, "fetch_one", baza.fetch_one)
    monkeypatch.setattr(notifications, "fetch_all", baza.fetch_all)
    monkeypatch.setattr(notifications, "execute", baza.execute)
    monkeypatch.setattr(notifications, "SecretBox", FalszywySekret)
    return baza


NIEZNANY = "dozorca-ryzyka:crypto:demo"


def test_kanarek_atrapa_LAPIE_znany_blad(monkeypatch):
    """Bez tego cisza tego pliku znaczyłaby „atrapa przyjmuje wszystko".

    Karmimy ją dokładnie tą wartością, na której czujka padła w klastrze —
    i osobno wartością prawdziwą, bo atrapa łapiąca wszystko nie łapie nic.
    """
    baza = AtrapaBazy()
    assert "crypto" in baza.silniki
    assert NIEZNANY not in baza.silniki, \
        "enum silników handlowych zna identyfikator procesu nadzoru — wtedy " \
        "to już nie jest enum silników"

    wstaw = "INSERT INTO notify.notification (...)"
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        asyncio.run(baza.fetch_one(wstaw, (1, "e", "critical", "t", "b",
                                           "{}", NIEZNANY)))
    asyncio.run(baza.fetch_one(wstaw, (1, "e", "critical", "t", "b",
                                       "{}", "crypto")))
    asyncio.run(baza.fetch_one(wstaw, (1, "e", "critical", "t", "b",
                                       "{}", None)))
    assert len(baza.wstawione) == 2


def test_alert_o_NIEZNANYM_komponencie_JEDNAK_DOCHODZI(monkeypatch):
    """Sedno: zdarzenie ma wylądować w bazie, a `notify()` zwrócić jego id.

    Przed poprawką to wywołanie kończyło się wyjątkiem i powiadomienia nie było
    w ogóle — ani w dzwonku, ani w WhatsAppie.
    """
    baza = _podepnij(monkeypatch, AtrapaBazy())
    nid = asyncio.run(notifications.notify(
        1, "engine.down", "Proces nadzoru przestał odpowiadać", "treść",
        engine=NIEZNANY, data={"wiek_min": 887.0}))

    assert nid == 7, "alarm nie doszedł — a to jest gorsze niż brak czujki"
    assert baza.proby == 2, "zapis nie został ponowiony po odmowie bazy"
    (wiersz,) = baza.wstawione
    assert wiersz["engine"] is None, \
        "nazwa komponentu znowu idzie do kolumny typu ref.engine"
    assert wiersz["data"]["komponent"] == NIEZNANY, \
        "nazwa komponentu przepadła — alarm nie mówi, CO zamilkło"
    assert wiersz["data"]["wiek_min"] == 887.0, \
        "degradacja zjadła dane wołającego"


def test_nazwa_komponentu_dociera_TAM_GDZIE_CZLOWIEK_CZYTA(monkeypatch):
    """`data` widać w dzwonku i w webhooku, ale nie w WhatsAppie — a to tam
    trafia alarm krytyczny o trzeciej w nocy. Nazwa musi być w treści."""
    baza = _podepnij(monkeypatch, AtrapaBazy())
    asyncio.run(notifications.notify(1, "engine.down", "tytuł", "treść",
                                     engine=NIEZNANY))
    (wiersz,) = baza.wstawione
    assert f"Komponent: {NIEZNANY}" in wiersz["body"]
    assert wiersz["body"].startswith("treść"), "degradacja nadpisała treść"


def test_pusta_tresc_tez_dostaje_nazwe_komponentu(monkeypatch):
    baza = _podepnij(monkeypatch, AtrapaBazy())
    asyncio.run(notifications.notify(1, "engine.down", "tytuł", engine=NIEZNANY))
    (wiersz,) = baza.wstawione
    assert wiersz["body"] == f"Komponent: {NIEZNANY}"


def test_usterka_zostaje_GLOSNA_w_logu(monkeypatch, capsys):
    """Degradacja nie ma prawa być cicha. Alarm dochodzi, ale wołający jest
    zepsuty i ktoś musi to zobaczyć — inaczej „działa" utrwala usterkę."""
    _podepnij(monkeypatch, AtrapaBazy())
    asyncio.run(notifications.notify(1, "engine.down", "tytuł", "treść",
                                     engine=NIEZNANY))
    log = capsys.readouterr().out
    assert NIEZNANY in log and "ref.engine" in log, \
        "kolumna została zdegradowana bez śladu w logu procesu"
    assert "engine.down" in log, "log nie mówi, KTÓREGO zdarzenia dotyczy"


def test_PRAWIDLOWY_silnik_idzie_do_kolumny_i_nic_sie_nie_dokleja(monkeypatch):
    """Ścieżka normalna ma zostać nietknięta: jedno podejście, pełna kolumna,
    treść bez dopisków. Inaczej poprawka kupiłaby dostawę alarmu za cenę
    zaśmiecenia wszystkich pozostałych."""
    baza = _podepnij(monkeypatch, AtrapaBazy())
    asyncio.run(notifications.notify(1, "engine.down", "tytuł", "treść",
                                     engine="crypto", data={"a": 1}))
    assert baza.proby == 1, "ścieżka normalna ponawia zapis bez powodu"
    (wiersz,) = baza.wstawione
    assert wiersz["engine"] == "crypto"
    assert wiersz["body"] == "treść"
    assert "komponent" not in wiersz["data"]


def test_brak_silnika_tez_jest_sciezka_normalna(monkeypatch):
    baza = _podepnij(monkeypatch, AtrapaBazy())
    asyncio.run(notifications.notify(1, "engine.down", "tytuł", "treść"))
    assert baza.proby == 1
    assert baza.wstawione[0]["engine"] is None


def test_blad_NIE_Z_KOLUMNY_engine_LECI_DALEJ(monkeypatch):
    """Degradacja dotyczy JEDNEJ kolumny, a nie każdego odrzuconego zapisu.

    Gdyby ten `except` łapał szerzej, zamieniłby się w połykacz błędów: zły
    `severity` (enum `notify.severity`) albo zepsuty JSON znikałyby po cichu,
    a my wróciliśmy właśnie stamtąd.
    """
    baza = AtrapaBazy()

    async def zawsze_odmawia(sql, params=None):
        if "INSERT INTO notify.notification" in sql:
            raise psycopg.errors.InvalidTextRepresentation(
                'invalid input value for enum notify.severity: "bardzo pilne"')
        return await baza.fetch_one(sql, params)

    monkeypatch.setattr(notifications, "fetch_one", zawsze_odmawia)
    monkeypatch.setattr(notifications, "fetch_all", baza.fetch_all)
    monkeypatch.setattr(notifications, "execute", baza.execute)

    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        asyncio.run(notifications.notify(1, "engine.down", "tytuł", "treść"))


def test_ponowienie_ktore_TEZ_pada_nie_jest_zamiatane(monkeypatch):
    """Druga odmowa ma inną przyczynę niż kolumna `engine` — zgadywanie jej
    byłoby udawaniem, że wiemy. Wyjątek leci dalej."""
    baza = AtrapaBazy()
    proby = []

    async def dwie_odmowy(sql, params=None):
        if "INSERT INTO notify.notification" in sql:
            proby.append(params[-1])
            raise psycopg.errors.InvalidTextRepresentation("odmowa")
        return await baza.fetch_one(sql, params)

    monkeypatch.setattr(notifications, "fetch_one", dwie_odmowy)
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        asyncio.run(notifications.notify(1, "engine.down", "tytuł", "treść",
                                         engine=NIEZNANY))
    assert proby == [NIEZNANY, None], "ponowienie nie poszło z pustą kolumną"


def test_WEBHOOK_dostaje_to_samo_co_dzwonek(monkeypatch):
    """Ładunek webhooka ma opisywać stan ZAPISANY, a nie intencję wołającego.

    Odbiorca porównujący webhook z API zobaczyłby inaczej dwie różne prawdy
    o tym samym zdarzeniu.
    """
    ladunki = []

    async def zlap_webhook(config, payload):
        ladunki.append(payload)

    baza = _podepnij(monkeypatch, AtrapaBazy(kanaly=[
        {"id": 3, "kind": "webhook", "config_enc": b"x", "label": "hook"}]))
    monkeypatch.setattr(notifications, "_send_webhook", zlap_webhook)

    asyncio.run(notifications.notify(1, "engine.down", "tytuł", "treść",
                                     engine=NIEZNANY, data={"wiek_min": 887.0}))
    (ladunek,) = ladunki
    assert ladunek["engine"] is None, \
        "webhook mówi o silniku, którego baza nie zapisała"
    assert ladunek["data"]["komponent"] == NIEZNANY
    assert f"Komponent: {NIEZNANY}" in ladunek["body"]
    assert baza.dostawy, "brak śladu dostarczenia"
