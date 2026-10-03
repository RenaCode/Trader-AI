"""„SPRAWDŹ POŁĄCZENIE" NIE MOŻE WYŁĄCZAĆ DZIAŁAJĄCEGO KLUCZA GIEŁDY.

CO BYŁO ZŁE

`POST /api/credentials/{id}/test` kończył się instrukcją

    active = (%s AND NOT COALESCE(%s, false))        -- check.ok, can_withdraw

czyli każde `ok = False` gasiło klucz. A `ok = False` wraca również dla
zwykłej awarii transportu: `BybitAdapter.check_connection` oddaje
`ConnectionCheck(ok=False, message="Nie udalo sie polaczyc: …")` przy
timeoucie, 5xx i zerwanym połączeniu (`bybit.py:637` i `647`).

`active` nie jest notatką o ostatniej próbie — jest BRAMKĄ HANDLU:
`loop.py:2173` filtruje `AND c.active` i przy braku klucza kończy
`sys.exit("egzekucja na giełdzie wymaga aktywnego klucza…")`, `fabryka.py:60`
tak samo dla danych rynkowych. Interfejs (`Settings.jsx`, „Zapisane klucze")
ma tylko „Sprawdź połączenie" i „Usuń", więc jedynym wyjściem był kolejny,
tym razem udany, test. Jedno kliknięcie diagnostyczne w złej minucie
zatrzymywało handel — przyciskiem opisanym w nagłówku modułu jako odczyt.

DLACZEGO TEN TEST WYKONUJE SQL, A NIE PORÓWNUJE JEGO TREŚCI

Wada siedzi w WARTOŚCI wyliczanej przez `CASE`/`AND`, a nie w nazwach kolumn.
Atrapa, która sprawdza, czy w instrukcji jest jakiś napis, przeszłaby dla
każdego wyrażenia o właściwym kształcie i dla żadnego nie powiedziałaby, co
naprawdę wyjdzie. `SilnikSQL` wykonuje więc prawdziwą instrukcję prawdziwym
silnikiem SQL (sqlite w pamięci) i czyta stan kolumny PO niej. Tłumaczenie
jest mechaniczne — `app.` znika, `now()` na `CURRENT_TIMESTAMP`, `%s` na `?` —
i nie dotyka wyrażenia, o które chodzi.
"""
from __future__ import annotations

import asyncio
import sqlite3


from services.api.routers import credentials as modul
from services.exchanges.base import ConnectionCheck

KLUCZ = 42


class SilnikSQL:
    """Jedna tabela `app.api_credential` w sqlite, plus tłumaczenie dialektu."""

    def __init__(self, *, active: bool = True, can_withdraw: bool = False):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE api_credential ("
            " id INTEGER PRIMARY KEY, active INTEGER, can_withdraw INTEGER,"
            " permissions TEXT, last_error TEXT, last_used_at TEXT)")
        self.db.execute(
            "INSERT INTO api_credential (id, active, can_withdraw, permissions,"
            " last_error, last_used_at) VALUES (?,?,?,?,?,?)",
            (KLUCZ, int(active), int(can_withdraw), None, None, None))
        self.instrukcje: list[str] = []

    async def execute(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.instrukcje.append(plaski)
        if "app.api_credential" not in plaski:
            return                       # np. dziennik audytu — nie ta tabela
        przetlumaczone = (plaski.replace("app.api_credential", "api_credential")
                               .replace("now()", "CURRENT_TIMESTAMP")
                               .replace("%s", "?"))
        self.db.execute(przetlumaczone, tuple(params or ()))

    def stan(self) -> dict:
        kolumny = ("id", "active", "can_withdraw", "permissions", "last_error")
        wiersz = self.db.execute(
            f"SELECT {', '.join(kolumny)} FROM api_credential WHERE id = ?",
            (KLUCZ,)).fetchone()
        return dict(zip(kolumny, wiersz))


class AtrapaAdaptera:
    def __init__(self, wynik: ConnectionCheck):
        self.wynik = wynik
        self.zamkniety = False

    async def check_connection(self):
        return self.wynik

    async def close(self):
        self.zamkniety = True


class PudelkoJawne:
    def decrypt(self, v):
        return bytes(v).decode()


def sprawdz(monkeypatch, wynik: ConnectionCheck, **stan_poczatkowy) -> SilnikSQL:
    """Woła `POST /credentials/{id}/test` z podłożonym wynikiem adaptera."""
    baza = SilnikSQL(**stan_poczatkowy)

    async def wiersz_klucza(sql, params=None):
        assert "app.api_credential" in sql
        return {"id": KLUCZ, "mode": "demo", "key_enc": b"k", "secret_enc": b"s",
                "venue_code": "bybit", "demo_host": "demo.example",
                "live_host": "live.example"}

    async def cisza(*a, **k):
        return None

    monkeypatch.setattr(modul, "fetch_one", wiersz_klucza)
    monkeypatch.setattr(modul, "execute", baza.execute)
    monkeypatch.setattr(modul, "audit", cisza)
    monkeypatch.setattr(modul, "SecretBox", PudelkoJawne)
    monkeypatch.setattr("services.exchanges.fabryka.zbuduj_adapter_gieldy",
                        lambda *a, **k: AtrapaAdaptera(wynik))

    asyncio.run(modul.test_credential(KLUCZ, None, {"id": 1}))
    return baza


# ============================== awaria transportu nie rusza bramki handlu

def test_zerwane_polaczenie_NIE_wylacza_klucza(monkeypatch):
    """Dokładnie ta odpowiedź, którą Bybit daje przy timeoucie."""
    baza = sprawdz(monkeypatch, ConnectionCheck(
        ok=False, message="Nie udalo sie polaczyc: timeout po 10 s"))

    assert baza.stan()["active"] == 1, (
        "jedno kliknięcie diagnostyczne przy chwilowej awarii sieci wyłączyło "
        "klucz, a `active` jest bramką handlu — silnik kończy `sys.exit` i nie "
        "ma w interfejsie przycisku, którym dałoby się go włączyć z powrotem")


def test_zerwane_polaczenie_ZAPISUJE_powod(monkeypatch):
    """Zostawienie `active` bez zmian nie może znaczyć „nic się nie stało"."""
    baza = sprawdz(monkeypatch, ConnectionCheck(
        ok=False, message="Nie udalo sie polaczyc: 502 Bad Gateway"))

    assert baza.stan()["last_error"] == "Nie udalo sie polaczyc: 502 Bad Gateway"


def test_awaria_nie_WLACZA_klucza_ktory_byl_wylaczony(monkeypatch):
    """Druga strona tej samej zasady: nieudany test nie jest też powodem, żeby
    klucz włączyć. „Bez zmian" znaczy bez zmian w obie strony."""
    baza = sprawdz(monkeypatch, ConnectionCheck(
        ok=False, message="Nie udalo sie polaczyc: connection reset"),
        active=False)

    assert baza.stan()["active"] == 0


# ============================== powody, które nadal gaszą klucz

def test_prawo_WYPLATY_dalej_wylacza_klucz(monkeypatch):
    """Klucz z prawem wypłaty nie może być aktywny — to samo wymusza CHECK
    w bazie (migracja 0007). Poprawka nie ma prawa tego rozluźnić."""
    baza = sprawdz(monkeypatch, ConnectionCheck(
        ok=True, message="ok", can_withdraw=True,
        permissions=["Trade", "Withdraw"]))

    assert baza.stan()["active"] == 0, (
        "klucz z prawem wypłaty został aktywny — to jest ta jedna rzecz, "
        "której ten UPDATE pilnuje od początku")


def test_udany_test_WLACZA_klucz_z_powrotem(monkeypatch):
    """Jedyna droga powrotu, jaką ma interfejs: „Sprawdź połączenie" musi
    włączać klucz, który wcześniej z jakiegoś powodu był wyłączony."""
    baza = sprawdz(monkeypatch, ConnectionCheck(
        ok=True, message="ok", can_withdraw=False, permissions=["Trade"]),
        active=False)

    assert baza.stan()["active"] == 1
    assert baza.stan()["last_error"] is None


def test_udany_test_zostawia_klucz_aktywny(monkeypatch):
    """Kanarek na najzwyklejszy przypadek — klucz działa i ma dalej działać."""
    baza = sprawdz(monkeypatch, ConnectionCheck(
        ok=True, message="ok", can_withdraw=False, permissions=["Trade"]))

    assert baza.stan()["active"] == 1


def test_host_bez_metadanych_klucza_nie_gasi_go_nieznanym_can_withdraw(monkeypatch):
    """`can_withdraw = None` znaczy „nie dało się odczytać uprawnień" (demo
    podmiotu unijnego, `bybit.py:648`). Brak odczytu nie jest odczytem
    mówiącym „ma prawo wypłaty"."""
    baza = sprawdz(monkeypatch, ConnectionCheck(
        ok=True, message="ok", can_withdraw=None, permissions=[]))

    assert baza.stan()["active"] == 1
