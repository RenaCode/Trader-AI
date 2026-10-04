"""Alarmy krytyczne do bramki RenaCode (decyzja właściciela 2026-10-04).

CO TEN PLIK PILNUJE

1. Alarm krytyczny z `notify()` wychodzi do bramki, ostrzeżenie i informacja
   nie — panel zostaje bez zmian.
2. Alarm, który miga, nie zasypuje WhatsAppa: ten sam alarm co 1 h, 2 h, 4 h,
   potem co 6 h, a wszystkich najwyżej LIMIT_NA_GODZINE na godzinę.
3. NIEUDANA WYSYŁKA NIGDY NIE PRZERYWA WOŁAJĄCEGO. Martwa bramka, zły klucz,
   zerwane połączenie, padnięty zapis śladu — `przekaz` oddaje status, nie
   wyjątek, a `notify()` oddaje id powiadomienia. Alarm, który wywraca czujkę,
   jest gorszy niż jego brak: razem z nim ginie wszystko, co czujka miała
   sprawdzić dalej.
4. Ślad w `notify.delivery` (kind 'bramka') dla każdego wyniku — to z niego
   liczone są powtórki i to on odpowiada na „dlaczego nie dostałem alarmu".
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from services.api import bramka, notifications

KLUCZ = "klucz-testowy-nie-prawdziwy"


class Slad:
    """`fetch_one`/`execute` z licznikiem powtórek podanym z góry."""

    def __init__(self, ten_sam=0, od_ostatniej_min=None, w_godzinie=0,
                 padnij_na_odczycie=False, padnij_na_zapisie=False,
                 nieudanych=0, od_nieudanej_min=None):
        self.stan = {"ten_sam": ten_sam, "od_ostatniej_min": od_ostatniej_min,
                     "w_godzinie": w_godzinie, "nieudanych": nieudanych,
                     "od_nieudanej_min": od_nieudanej_min}
        self.padnij_na_odczycie = padnij_na_odczycie
        self.padnij_na_zapisie = padnij_na_zapisie
        self.zapytania = []
        self.dostawy = []

    async def fetch_one(self, sql, params=None):
        self.zapytania.append((sql, params))
        if self.padnij_na_odczycie:
            raise RuntimeError("baza nie odpowiada")
        assert "notify.delivery" in sql and "'bramka'" in sql, sql
        return self.stan

    async def execute(self, sql, params=None):
        if self.padnij_na_zapisie:
            raise RuntimeError("permission denied for table delivery")
        assert "INSERT INTO notify.delivery" in sql and "'bramka'" in sql, sql
        nid, status, proby, blad, _ = params
        self.dostawy.append({"nid": nid, "status": status, "proby": proby, "blad": blad})


def klient(status=200, odp=None, wyjatek=None):
    """httpx z podstawionym transportem. Zwraca (klient, lista zapytań)."""
    zapytania = []

    def handler(req):
        zapytania.append(req)
        if wyjatek:
            raise wyjatek
        tresc = odp if odp is not None else {"ok": True, "kanal": "whatsapp", "bledy": {},
                                             "odbiorcy": {"doszlo": 2, "wszystkich": 2}}
        return httpx.Response(status, json=tresc)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), zapytania


def przekaz(slad, k=None, *, severity="critical", title="Kill-switch zadziałał (crypto)",
            body="Limit obsunięcia przekroczony", engine="crypto", dane=None):
    async def _():
        try:
            return await bramka.przekaz(
                41, "killswitch.triggered", severity, title, body, engine, dane,
                fetch_one=slad.fetch_one, execute=slad.execute, klient=k)
        finally:
            if k is not None:
                await k.aclose()
    return asyncio.run(_())


@pytest.fixture
def z_kluczem(monkeypatch):
    monkeypatch.setenv("BRAMKA_KLUCZ", KLUCZ)
    monkeypatch.delenv("BRAMKA_URL", raising=False)


# ------------------------------------------------------------------ rytm

def test_przerwa_rosnie_z_liczba_wyslanych_i_ma_sufit():
    assert [bramka.wymagana_przerwa_min(n) for n in range(6)] == [0, 60, 120, 240, 360, 360]


def test_pierwszy_alarm_idzie_od_razu():
    assert bramka.ocen_powtorke(0, None, 0) is None


def test_powtorka_przed_przerwa_jest_tlumiona_a_po_niej_idzie():
    assert bramka.ocen_powtorke(1, 10, 1) is not None
    assert bramka.ocen_powtorke(1, 61, 1) is None
    assert bramka.ocen_powtorke(3, 200, 3) is not None
    assert bramka.ocen_powtorke(3, 241, 3) is None


def test_NIEUDANA_proba_tez_ma_rytm_krotszy_niz_wysylka():
    """Przegląd 2026-10-04: przy martwej bramce każde `notify()` tego samego
    alarmu próbowało od nowa i czekało na timeout. Nieudana próba ma teraz
    własną przerwę - krótszą, bo alarm nadal nie doszedł do człowieka."""
    assert [bramka.przerwa_po_bledzie_min(n) for n in range(7)] == \
        [0, 10, 20, 40, 80, 160, 320]
    assert bramka.przerwa_po_bledzie_min(9) == bramka.MAKS_PRZERWA_MIN
    assert "NIE przeszedł" in bramka.ocen_powtorke(0, None, 0, 1, 3)
    assert bramka.ocen_powtorke(0, None, 0, 1, 11) is None
    assert bramka.ocen_powtorke(0, None, 0, 2, 15) is not None


def test_nieudana_proba_tlumiona_bez_zapytania_do_bramki(z_kluczem):
    slad = Slad(nieudanych=1, od_nieudanej_min=2)
    k, zapytania = klient()
    assert przekaz(slad, k) == "suppressed_cooldown"
    assert zapytania == [] and "NIE przeszedł" in slad.dostawy[0]["blad"]


def test_polaczenie_ma_KROTKI_termin_a_odpowiedz_dlugi():
    assert bramka.TIMEOUT.connect == 3.0 and bramka.TIMEOUT.read == bramka.TIMEOUT_S


def test_limit_godzinowy_tlumi_takze_NOWY_alarm():
    """Burza różnych alarmów (np. po restarcie bazy) to też spam."""
    assert bramka.ocen_powtorke(0, None, bramka.LIMIT_NA_GODZINE) is not None
    assert bramka.ocen_powtorke(0, None, bramka.LIMIT_NA_GODZINE - 1) is None


# ------------------------------------------------------------------ wysyłka

def test_bez_klucza_nic_nie_wychodzi_i_nic_nie_jest_pytane(monkeypatch):
    monkeypatch.delenv("BRAMKA_KLUCZ", raising=False)
    slad = Slad()
    k, zapytania = klient()
    assert przekaz(slad, k) == "brak-klucza"
    assert zapytania == [] and slad.zapytania == [] and slad.dostawy == []


def test_alarm_niesie_klucz_temat_priorytet_i_rodzaj(z_kluczem):
    slad = Slad()
    k, zapytania = klient()
    assert przekaz(slad, k) == "sent"
    assert len(zapytania) == 1
    q = zapytania[0]
    assert str(q.url) == bramka.URL_DOMYSLNY
    assert q.headers["X-Api-Key"] == KLUCZ
    cialo = json.loads(q.content)
    assert cialo["temat"] == "Kill-switch zadziałał (crypto)"
    assert cialo["priorytet"] == "alarm"
    assert cialo["rodzaj"] == "crypto"
    assert cialo["tresc"].startswith("Limit obsunięcia przekroczony")
    # Przypis na końcu mówi, skąd to jest i gdzie szukać pełnej treści.
    assert cialo["tresc"].rstrip().endswith("panel trader.renacode.com)")
    assert "#41" in cialo["tresc"]
    assert slad.dostawy == [{"nid": 41, "status": "sent", "proby": 1, "blad": None}]


def test_klucz_NIE_trafia_do_logu(z_kluczem, capsys):
    przekaz(Slad(), klient(status=401, odp={"ok": False, "blad": "nieznany klucz"})[0])
    przekaz(Slad(), klient()[0])
    assert KLUCZ not in capsys.readouterr().out


def test_komponent_spoza_enuma_jest_rodzajem(z_kluczem):
    k, zapytania = klient()
    przekaz(Slad(), k, engine=None, dane={"komponent": "dozorca-ryzyka:crypto:demo"})
    assert json.loads(zapytania[0].content)["rodzaj"] == "dozorca-ryzyka:crypto:demo"


def test_dluga_tresc_jest_przycieta(z_kluczem):
    k, zapytania = klient()
    przekaz(Slad(), k, body="x" * 10000)
    tresc = json.loads(zapytania[0].content)["tresc"]
    assert len(tresc) < bramka.MAKS_TRESC + 200
    assert "pełna treść w panelu" in tresc


def test_sukces_czesciowy_jest_sukcesem_z_niedoreczeniem_w_sladzie(z_kluczem):
    slad = Slad()
    k, _ = klient(odp={"ok": True, "kanal": "whatsapp",
                       "odbiorcy": {"doszlo": 1, "wszystkich": 2},
                       "bledy": {"whatsapp": ["...200: timeout"]}})
    assert przekaz(slad, k) == "sent"
    assert slad.dostawy[0]["status"] == "sent"
    assert "1/2" in slad.dostawy[0]["blad"] and "timeout" in slad.dostawy[0]["blad"]


def test_powtorka_jest_tlumiona_bez_zapytania_do_bramki(z_kluczem):
    slad = Slad(ten_sam=1, od_ostatniej_min=5, w_godzinie=1)
    k, zapytania = klient()
    assert przekaz(slad, k) == "suppressed_cooldown"
    assert zapytania == []
    assert slad.dostawy[0]["status"] == "suppressed_cooldown"
    assert "5 min temu" in slad.dostawy[0]["blad"]


def test_licznik_powtorek_pyta_o_TEN_alarm(z_kluczem):
    slad = Slad()
    przekaz(slad, klient()[0], title="Worker X zamilkł", engine=None)
    sql, params = slad.zapytania[0]
    assert "x.status = 'sent'" in sql and "x.status = 'failed'" in sql
    assert params == ("killswitch.triggered", "Worker X zamilkł", None)


# ------------------------------------------------- awaria nie przerywa wołającego

@pytest.mark.parametrize("opis,k", [
    ("bramka 502 - żaden kanał nie przyjął",
     lambda: klient(status=502, odp={"ok": False, "bledy": {"email": "Mailgun 500"}})[0]),
    ("zły klucz", lambda: klient(status=401, odp={"blad": "nieznany klucz"})[0]),
    ("200 bez ok", lambda: klient(odp={"ok": False})[0]),
    ("bramka nie odpowiada", lambda: klient(wyjatek=httpx.ConnectError("timeout"))[0]),
])
def test_nieudana_wysylka_daje_failed_a_nie_wyjatek(z_kluczem, opis, k):
    slad = Slad()
    assert przekaz(slad, k()) == "failed", opis
    assert slad.dostawy and slad.dostawy[0]["status"] == "failed", opis
    assert slad.dostawy[0]["blad"], "ślad bez powodu nie odpowie na pytanie dlaczego"


def test_padniety_odczyt_sladu_nie_przerywa(z_kluczem):
    assert przekaz(Slad(padnij_na_odczycie=True), klient()[0]) == "failed"


def test_padniety_zapis_sladu_nie_przerywa_ani_nie_cofa_wysylki(z_kluczem):
    k, zapytania = klient()
    assert przekaz(Slad(padnij_na_zapisie=True), k) == "sent"
    assert len(zapytania) == 1


# ------------------------------------------------------------------ notify()

class Baza:
    """Tyle `notify()`, ile trzeba: typ zdarzenia, jeden kanał w aplikacji."""

    def __init__(self, sev="critical"):
        self.sev = sev
        self.dostawy = []

    async def fetch_one(self, sql, params=None):
        if "notify.event_type" in sql:
            return {"code": params[0], "default_severity": self.sev, "always_on": True,
                    "default_cooldown_s": 0, "label": "test"}
        if "INSERT INTO notify.notification" in sql:
            return {"id": 99}
        return None

    async def fetch_all(self, sql, params=None):
        if "notify.channel" in sql:
            return [{"id": 1, "kind": "inapp", "config_enc": None, "label": "W aplikacji"}]
        raise AssertionError(sql)

    async def execute(self, sql, params=None):
        self.dostawy.append(sql)


class FalszywySekret:
    """`notify()` tworzy SecretBox zawsze, a zestaw testowy nie ma SECRETS_KEY."""

    def decrypt(self, _):
        return b"{}"


class SekretBezKlucza:
    def __init__(self):
        raise RuntimeError("brak SECRETS_KEY")


def _notify(monkeypatch, baza, sev=None, sekret=None, wolania=None):
    wolania = [] if wolania is None else wolania

    async def atrapa(*a, **kw):
        wolania.append((a, kw))
        return "sent"

    monkeypatch.setattr(notifications, "fetch_one", baza.fetch_one)
    monkeypatch.setattr(notifications, "fetch_all", baza.fetch_all)
    monkeypatch.setattr(notifications, "execute", baza.execute)
    monkeypatch.setattr(notifications.bramka, "przekaz", atrapa)
    monkeypatch.setattr(notifications, "SecretBox", sekret or FalszywySekret)
    nid = asyncio.run(notifications.notify(1, "killswitch.triggered", "T", "B",
                                           engine="crypto", severity=sev))
    return nid, wolania


@pytest.mark.parametrize("sev,idzie", [("critical", True), ("warning", False), ("info", False)])
def test_do_bramki_idzie_tylko_alarm_krytyczny(monkeypatch, sev, idzie):
    nid, wolania = _notify(monkeypatch, Baza(), sev=sev)
    assert nid == 99
    assert bool(wolania) is idzie, sev


def test_powaga_powiadomienia_a_nie_domyslna_typu_rozstrzyga(monkeypatch):
    """`killswitch.triggered` bywa wysyłany jako `warning` (automatyczne
    zwolnienie) — to nie jest alarm na telefon."""
    _, wolania = _notify(monkeypatch, Baza(sev="critical"), sev="warning")
    assert not wolania


def test_alarm_idzie_do_bramki_ZANIM_kanaly_uzytkownika_moga_paść(monkeypatch):
    """`SecretBox()` rzuca bez SECRETS_KEY — bramka nie może od tego zależeć."""
    wolania = []
    with pytest.raises(RuntimeError, match="SECRETS_KEY"):
        _notify(monkeypatch, Baza(), sekret=SekretBezKlucza, wolania=wolania)
    assert len(wolania) == 1


def test_notify_przekazuje_id_i_tresc_z_dzwonka(monkeypatch):
    _, wolania = _notify(monkeypatch, Baza())
    (a, kw), = wolania
    assert a[:6] == (99, "killswitch.triggered", "critical", "T", "B", "crypto")
    assert kw["fetch_one"] is not None and kw["execute"] is not None


def test_notify_z_martwa_bramka_oddaje_id(monkeypatch, z_kluczem):
    """Pełna ścieżka bez atrapy `przekaz`: bramka nie istnieje w teście, więc
    połączenie pada — a powiadomienie i tak jest zapisane i zwrócone."""
    baza = Baza()
    monkeypatch.setattr(notifications, "fetch_one", baza.fetch_one)
    monkeypatch.setattr(notifications, "fetch_all", baza.fetch_all)
    monkeypatch.setattr(notifications, "execute", baza.execute)
    monkeypatch.setattr(notifications, "SecretBox", FalszywySekret)
    monkeypatch.setenv("BRAMKA_URL", "http://127.0.0.1:9/v1/wyslij")
    nid = asyncio.run(notifications.notify(1, "killswitch.triggered", "T", "B",
                                           engine="crypto"))
    assert nid == 99


# ------------------------------------------------------------- prawdziwa baza

TEST_DB_URL = __import__("os").environ.get("TEST_DB_URL")


@pytest.mark.skipif(not TEST_DB_URL, reason="ustaw TEST_DB_URL na jednorazową bazę")
@pytest.mark.parametrize("rola", ["trader_app", "trader_dozorca", "trader_czujka",
                                  "trader_silnik", "trader_postmortem"])
def test_BAZA_slad_i_licznik_powtorek_dzialaja_na_roli_wolajacego(rola):
    """Każda rola, która woła `notify()`, musi zapisać ślad `bramka` i policzyć
    powtórki — inaczej tłumienie po cichu nie działa (wyjątek łapie `przekaz`)."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        with conn.transaction(force_rollback=True):
            uid = conn.execute(
                "INSERT INTO app.\"user\" (login, password_hash) VALUES ('test-bramki', 'x')"
                " RETURNING id").fetchone()["id"]
            nid = conn.execute(
                "INSERT INTO notify.notification (user_id, event_code, severity, title)"
                " VALUES (%s, 'killswitch.triggered', 'critical', 'T') RETURNING id",
                (uid,)).fetchone()["id"]
            conn.execute(f"SET LOCAL ROLE {rola}")
            conn.execute(bramka.SQL_SLAD, (nid, "sent", 1, None, "sent"))
            conn.execute(bramka.SQL_SLAD, (nid, "failed", 1, "ConnectError", "failed"))
            stan = conn.execute(bramka.SQL_POWTORKI,
                                ("killswitch.triggered", "T", None)).fetchone()
            assert stan["ten_sam"] == 1 and stan["w_godzinie"] == 1
            assert float(stan["od_ostatniej_min"]) == 0
            assert stan["nieudanych"] == 1 and float(stan["od_nieudanej_min"]) == 0
            inny = conn.execute(bramka.SQL_POWTORKI,
                                ("killswitch.triggered", "T", "crypto")).fetchone()
            assert inny["ten_sam"] == 0 and inny["od_ostatniej_min"] is None
            assert inny["nieudanych"] == 0 and inny["w_godzinie"] == 1
            sent_at = conn.execute(
                "SELECT sent_at FROM notify.delivery WHERE notification_id = %s",
                (nid,)).fetchone()["sent_at"]
            assert sent_at is not None


# ------------------------------------------------- czy bramka zna klucz (dozór)

def test_url_stanu_z_url_wysylki():
    assert bramka.url_stanu(bramka.URL_DOMYSLNY) == \
        "http://bramka.bramka.svc.cluster.local/v1/stan"
    assert bramka.url_stanu("http://b:8080/") == "http://b:8080/v1/stan"


def test_stan_pyta_GET_z_kluczem_i_rzuca_na_odmowe(z_kluczem):
    zapytania = []

    def handler(req):
        zapytania.append(req)
        return httpx.Response(200, json={"kanal_auto": "whatsapp"})

    async def _(k):
        async with k:
            return await bramka.stan(klient=k)

    k = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(_(k)) == {"kanal_auto": "whatsapp"}
    assert zapytania[0].method == "GET" and zapytania[0].url.path == "/v1/stan"
    assert zapytania[0].headers["X-Api-Key"] == KLUCZ
    odmowa = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"blad": "zly albo brak X-Api-Key"})))
    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(_(odmowa))


def _kanaly(monkeypatch, stan):
    from services.engine import dozor
    from tests.test_dozor import _stend_kanalow
    monkeypatch.setattr(bramka, "stan", stan)
    fetch_all, notify, wolania = _stend_kanalow(luk=2)
    return asyncio.run(dozor.sprawdz_kanaly(fetch_all, notify)), wolania


def test_czujka_kanalow_UFA_bramce_dopiero_gdy_ta_zna_klucz(z_kluczem, monkeypatch, capsys):
    async def zna():
        return {"kanal_auto": "whatsapp"}

    assert _kanaly(monkeypatch, zna) == (0, [])
    assert "przyjmuje klucz" in capsys.readouterr().out


def test_klucz_odrzucany_przez_bramke_to_NIE_droga_poza_panel(z_kluczem, monkeypatch, capsys):
    """Do 2026-10-04 wystarczała zmienna w środowisku - klucz z literówką
    wyglądał tu identycznie jak działający."""
    async def odrzuca():
        raise RuntimeError("bramka zwróciła 401: zly albo brak X-Api-Key")

    luki, wolania = _kanaly(monkeypatch, odrzuca)
    assert luki == 2 and len(wolania) == 1
    assert "NIE PRZYJMUJE" in capsys.readouterr().out
