"""Luki drobne z wyglądu, groźne w skutkach — i dlatego zostawiane.

Każda z nich była już raz zgłoszona i przeżyła zgłoszenie, bo w opisie wygląda
na kosmetykę. Ten plik pilnuje, żeby nie wróciły po cichu.

WYBÓR KLUCZA API. `loop.zbuduj_adapter` naprawiono — bierze NAJNOWSZY aktywny
klucz. Ten sam wzorzec `ORDER BY c.id LIMIT 1` (czyli: najstarszy) został
w dwóch innych procesach, które rozmawiają z giełdą. Rozjazd jest niewidoczny
aż do rotacji: przez chwilę aktywne są dwa klucze i wtedy każdy proces
wybiera własny rachunek.

W uzgodnieniu waży to najwięcej. To narzędzie istnieje po to, żeby wykryć
różnicę między naszym obrazem stanu a obrazem giełdy — pytane starym kluczem
patrzy na INNY rachunek niż ten, którym handluje silnik, więc samo produkuje
rozjazd, który miało wykrywać. Albo, groźniej, melduje zgodność pustego
rachunku, gdy na handlowanym coś zostało otwarte.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
if str(KORZEN) not in sys.path:
    sys.path.insert(0, str(KORZEN))

from services.exchanges.base import ExchangeError  # noqa: E402
from services.exchanges.mozliwosci import wybierz_klucz  # noqa: E402

TERAZ = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _klucz(id_: int, godzin_temu: int, hint: str) -> dict:
    return {"id": id_, "key_hint": hint,
            "created_at": TERAZ - timedelta(hours=godzin_temu),
            "key_enc": hint.encode(), "secret_enc": (hint + "-sekret").encode(),
            "demo_host": "https://api-demo.bybit.eu",
            "live_host": "https://api.bybit.eu"}


# ----------------------------------------------------- wybór klucza: sam wybór

def test_wybor_bierze_najnowszy_klucz_a_nie_najnizsze_id():
    """Rotacja nadaje nowemu kluczowi wyższe `id` tylko przypadkiem.

    Klucz wgrany ponownie po usunięciu wpisu, przywrócony z kopii albo
    zaimportowany razem z historią ma `id` niższe niż ten, który zastępuje.
    Wybór po `id` cofa wtedy proces na rachunek, z którego już nie handlujemy.
    """
    stary = _klucz(9, godzin_temu=48, hint="stary")
    nowy = _klucz(2, godzin_temu=1, hint="nowy")
    cred, _ = wybierz_klucz([stary, nowy], "demo")
    assert cred["key_hint"] == "nowy"
    # Kolejność wejściowa nie może zmieniać wyniku — inaczej wybór zależałby
    # od tego, jak baza akurat ułożyła wiersze.
    assert wybierz_klucz([nowy, stary], "demo")[0]["key_hint"] == "nowy"


def test_remis_czasu_rozstrzyga_id_wiec_wybor_jest_powtarzalny():
    """Dwa klucze wgrane w tej samej milisekundzie mają dawać ten sam wybór
    w każdym procesie i po każdym restarcie. Losowa kolejność znaczyłaby, że
    silnik handluje z jednego rachunku, a uzgodnienie sprawdza drugi."""
    a = _klucz(4, godzin_temu=3, hint="a")
    b = _klucz(5, godzin_temu=3, hint="b")
    assert wybierz_klucz([a, b], "demo")[0]["key_hint"] == "b"
    assert wybierz_klucz([b, a], "demo")[0]["key_hint"] == "b"


def test_dwa_aktywne_klucze_sa_slyszalne():
    """Wybór jest jednoznaczny, ale sam stan nie jest zaplanowany: albo
    zapomniano dezaktywować stary klucz, albo ktoś wgrał klucz do złego trybu.
    Cisza w tym miejscu była połową poprzedniego błędu."""
    cred, ostrzezenie = wybierz_klucz(
        [_klucz(1, 48, "stary"), _klucz(2, 1, "nowy")], "live")
    assert cred["key_hint"] == "nowy"
    assert "2 aktywnych kluczy" in ostrzezenie
    assert "live" in ostrzezenie          # tryb, bo to on bywa pomylony
    assert "nowy" in ostrzezenie          # KTÓRY klucz wygrał, nie samo „kilka"


def test_jeden_klucz_nie_ostrzega_a_brak_klucza_nie_wybiera():
    """Ostrzeżenie przy każdym przebiegu przestaje być ostrzeżeniem."""
    assert wybierz_klucz([_klucz(1, 5, "jedyny")], "demo")[1] is None
    assert wybierz_klucz([], "demo") == (None, None)


# ------------------------------------------- wybór klucza: użycie w procesach

class _AdapterAtrapa:
    """Zapamiętuje, JAKIM kluczem został zbudowany — o to jest cały spór."""

    ostatni: "_AdapterAtrapa | None" = None

    def __init__(self, *, api_key, api_secret, mode, host):
        self.api_key, self.api_secret, self.mode, self.host = (
            api_key, api_secret, mode, host)
        _AdapterAtrapa.ostatni = self

    async def fetch_balances(self):
        return []

    async def _get(self, sciezka, params=None):
        raise ExchangeError("host tego nie wystawia", code="404")

    async def close(self):
        pass


class _SecretBoxAtrapa:
    def decrypt(self, dane: bytes) -> str:
        return dane.decode()


def _podloz(monkeypatch, wiersze_klucza, *, adapter_cls=None, symbol=None):
    """Podmienia bazę, szyfrowanie i adapter — zostaje sama sprawdzana logika."""
    import services.api.db as db
    import services.api.security as security
    import services.exchanges.bybit as bybit

    async def fetch_all(sql, params=None):
        return list(wiersze_klucza) if "api_credential" in sql else []

    async def fetch_one(sql, params=None):
        if "ref.instrument" in sql:
            return {"symbol": symbol} if symbol else None
        # Wiersze kluczy przychodzą w kolejności, w jakiej oddałaby je baza
        # dla `ORDER BY c.id` — czyli najstarszy pierwszy. Dzięki temu powrót
        # do `fetch_one(... LIMIT 1)` nie przechodzi tu po cichu, tylko wybiera
        # stary klucz i test to widzi.
        if "api_credential" in sql:
            return wiersze_klucza[0] if wiersze_klucza else None
        return None

    async def nic():
        return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa/atrapa")
    monkeypatch.setattr(db, "open_pool", nic)
    monkeypatch.setattr(db, "close_pool", nic)
    monkeypatch.setattr(db, "fetch_all", fetch_all)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(security, "SecretBox", _SecretBoxAtrapa)
    monkeypatch.setattr(bybit, "BybitAdapter", adapter_cls or _AdapterAtrapa)
    _AdapterAtrapa.ostatni = None


@pytest.mark.parametrize("modul, argv", [
    ("services.engine.uzgodnienie", ["uzgodnienie", "--mode", "demo", "--sucho"]),
    ("services.exchanges.mozliwosci", ["mozliwosci", "--mode", "demo"]),
])
def test_procesy_gieldowe_pytaja_najnowszym_kluczem(monkeypatch, modul, argv):
    """Sprawdzenie stanu i sprawdzenie hosta muszą trafiać na TEN SAM rachunek,
    z którego handluje silnik.

    Uzgodnienie pytane starym kluczem porównuje nasz dziennik z cudzym stanem:
    zgłasza pozycje-widma, których nie ma, albo melduje zgodność pustego
    rachunku, gdy na handlowanym coś zostało otwarte. Narzędzie do wykrywania
    rozjazdu samo staje się jego źródłem.
    """
    import importlib

    m = importlib.import_module(modul)
    # Kolejność jak z bazy przy `ORDER BY c.id`: najstarszy klucz ma niższe
    # `id`, więc stary wzorzec wybrałby właśnie jego.
    _podloz(monkeypatch, [_klucz(1, godzin_temu=72, hint="stary"),
                          _klucz(7, godzin_temu=1, hint="nowy")])
    monkeypatch.setattr(sys, "argv", argv)

    asyncio.run(m.main())

    assert _AdapterAtrapa.ostatni is not None, "adapter w ogóle nie powstał"
    assert _AdapterAtrapa.ostatni.api_key == "nowy"
    assert _AdapterAtrapa.ostatni.api_secret == "nowy-sekret"


@pytest.mark.parametrize("modul, argv", [
    ("services.engine.uzgodnienie", ["uzgodnienie", "--mode", "demo", "--sucho"]),
    ("services.exchanges.mozliwosci", ["mozliwosci", "--mode", "demo"]),
])
def test_procesy_gieldowe_mowia_o_dwoch_aktywnych_kluczach(
        monkeypatch, capsys, modul, argv):
    """Niedokończona rotacja ma być słyszalna w każdym procesie, który jej
    dotyczy — inaczej dowiadujemy się o niej w dniu wygaśnięcia starego klucza,
    a objaw wygląda jak awaria uwierzytelnienia bez przyczyny."""
    import importlib

    m = importlib.import_module(modul)
    _podloz(monkeypatch, [_klucz(1, godzin_temu=72, hint="stary"),
                          _klucz(7, godzin_temu=1, hint="nowy")])
    monkeypatch.setattr(sys, "argv", argv)

    asyncio.run(m.main())

    out = capsys.readouterr().out
    assert "2 aktywnych kluczy" in out
    assert "nowy" in out


# ======================================================================
# CENNIK GIEŁDY: sprawdzenie istniało, tylko nikt go nie uruchamiał
#
# `stawki_gieldy` i `rozjazd_stawek` porównują prowizje naliczane przez giełdę
# z tablicą CENNIK — i były narzędziem CLI, którego nie wołało nic cyklicznego.
#
# Runda kosztuje 0,200%, a mediana zwrotu na transakcję jest rzędu dziesiątych
# procenta, więc cała przewaga mieści się w kilku punktach bazowych. Zmiana
# progu VIP albo regulaminu przelicza błędnie KAŻDĄ transakcję do chwili, w
# której ktoś zapyta hosta wprost — a rozjazd nie objawia się niczym: backtest
# dalej pokazuje przewagę, prawdziwy wynik po prostu jest inny.
# ======================================================================

SZABLONY = KORZEN / "charts" / "trader" / "templates"
CRON_YAML = (SZABLONY / "zywotnosc-cron.yaml").read_text(encoding="utf-8")
MIGRACJA_ROL = (KORZEN / "db" / "migrations" / "0034_role_procesow.sql").read_text(
    encoding="utf-8")


def _bez_komentarzy(tekst: str, znak: str) -> str:
    """Uzasadnienie w komentarzu nie jest ani uruchomieniem, ani uprawnieniem."""
    return "\n".join(l.split(znak)[0] for l in tekst.splitlines())


def _linia_cennika() -> str:
    (linia,) = [l for l in _bez_komentarzy(CRON_YAML, "#").splitlines()
                if "services/exchanges/mozliwosci.py" in l]
    return linia


def test_cennik_jest_sprawdzany_cyklicznie_a_nie_tylko_recznie():
    """Narzędzie, które trzeba PAMIĘTAĆ uruchomić, nie jest sprawdzeniem.

    Rozjazd stawek fałszuje wszystkie liczby naraz, włącznie z tymi, którymi
    sprawdzalibyśmy pomyłkę — więc nic o nim nie przypomni.
    """
    assert "services/exchanges/mozliwosci.py" in _bez_komentarzy(CRON_YAML, "#"), \
        "cennik znów nie jest przez nic wołany"
    linia = _linia_cennika()
    assert "--stawki" in linia, \
        "pełne badanie hosta kończy się kodem 1 na każdym hoście bez sald"
    # Bez dopisania do RC rozjazd byłby linijką w logu poda, którego o trzeciej
    # w nocy nikt nie czyta — czyli tą samą ciszą, co brak sprawdzenia.
    assert "|| RC=$?" in linia


# CENNIKI, KTÓRYCH TEN CRON SPRAWDZIĆ NIE UMIE — i dlaczego, z nazwy.
#
# Lista jest WĄSKA CELOWO. Nie jest zwolnieniem z pomiaru, tylko zapisem, że
# pomiaru nie ma: pusty wyjątek „bo na razie nie da się sprawdzić" rozrósłby się
# do listy, na której nikt już nie pamięta, co jest zweryfikowane.
CENNIKI_BEZ_AUTOMATU = {
    "stock": (
        "services/exchanges/mozliwosci.py pyta WYŁĄCZNIE Bybita: ma zaszyte "
        "`WHERE v.code = 'bybit'` i buduje `BybitAdapter`. Alpaca nie wystawia "
        "przy tym odpowiednika `/v5/account/fee-rate` — zero prowizji jest tam "
        "warunkiem umowy, nie polem w API. Realny koszt sprzedaży to opłaty "
        "regulacyjne, a te `koszty.karta_rundy` wymienia jako NIEZMIERZONE "
        "i zamknie je odczyt `exec.fill` po pierwszych sprzedażach — nie cennik."
    ),
}


def test_cron_pyta_o_ten_sam_cennik_ktorym_liczy_silnik():
    """Dwa `--kind` w dwóch plikach to rozjazd przy pierwszej zmianie.

    Sprawdzenie cennika `spot` przy silniku liczącym `spot_eu` meldowałoby
    zgodność, nie dotykając ani jednej liczby wchodzącej do wyniku — czyli
    byłoby gorsze niż brak sprawdzenia, bo wygląda jak sprawdzenie.

    OD 2026-09-24 SILNIKÓW SĄ DWA, więc `--kind` też jest dwa. Warunek „dokładnie
    jeden cennik" przestał opisywać system: pilnował liczby, a nie pokrycia.
    Teraz test żąda, żeby KAŻDY cennik, którym liczy któraś pętla decyzyjna, był
    albo sprawdzany cyklicznie, albo wymieniony w `CENNIKI_BEZ_AUTOMATU` razem
    z powodem. Cennik, który po cichu wypadł z obu list, kończy test na czerwono.
    """
    import re as _re

    import yaml

    values = yaml.safe_load((KORZEN / "charts" / "trader" / "values.yaml")
                            .read_text(encoding="utf-8"))
    komendy = " ".join(str(w.get("command", ""))
                       for w in values["workers"].values() if isinstance(w, dict))
    kindy_silnikow = set(_re.findall(r"--kind[ ',\"]+(\w+)", komendy))
    assert kindy_silnikow, "żadna pętla decyzyjna nie podaje `--kind`"

    linia = _linia_cennika()
    sprawdzane = set(_re.findall(r"--kind[ ',\"]+(\w+)", linia))
    bez_pokrycia = kindy_silnikow - sprawdzane - set(CENNIKI_BEZ_AUTOMATU)
    assert not bez_pokrycia, (
        f"cennik {sorted(bez_pokrycia)} wchodzi do wyniku silnika, a nic go nie "
        f"weryfikuje wobec giełdy i nie ma go wśród jawnych wyjątków")

    # Wyjątek, który przestał być potrzebny, ma zniknąć — inaczej lista rośnie
    # w jedną stronę i po roku nie opisuje już niczego.
    zbedne = set(CENNIKI_BEZ_AUTOMATU) & sprawdzane
    assert not zbedne, f"{sorted(zbedne)} jest już sprawdzany cyklicznie — zdejmij z wyjątków"


def test_czujka_ma_prawo_czytac_klucze_gieldy():
    """Stawki są na endpoincie PRYWATNYM, więc sprawdzenie wymaga klucza.

    Cron chodzi rolą `trader_czujka`, która wyłącznie czyta. Gdyby nie miała
    prawa do `app.api_credential`, wpięcie tej linii byłoby obejściem decyzji
    z migracji 0034, a nie domknięciem luki.
    """
    import re as _re

    sql = _bez_komentarzy(MIGRACJA_ROL, "--")
    assert _re.search(r"GRANT SELECT ON app\.api_credential TO [^;]*trader_czujka",
                      sql), "czujka straciła prawo odczytu kluczy — cron pyta na próżno"
    # Klucz w bazie jest zaszyfrowany, więc samo prawo odczytu nie wystarcza.
    assert "SECRETS_KEY" in CRON_YAML
    # I ani jednego prawa więcej: sprawdzenie stawek to czysty odczyt.
    assert "INSERT" not in sql.split("app.api_credential")[1].split(";")[0]


# ------------------------------------------------------- sama treść sprawdzenia

class _AdapterZeStawkami(_AdapterAtrapa):
    """Host, który stawki wystawia — z podłożoną odpowiedzią `/v5/account/fee-rate`."""

    maker = "0.001"
    taker = "0.0025"

    async def _get(self, sciezka, params=None):
        return {"list": [{"makerFeeRate": self.maker, "takerFeeRate": self.taker}]}


class _AdapterOdmawiajacy(_AdapterAtrapa):
    """Klucz bez prawa odczytu konta albo host bez endpointu — jeden objaw."""

    async def _get(self, sciezka, params=None):
        raise ExchangeError("brak uprawnienia do odczytu konta", code="10005")


def _sprawdz_cennik(adapter, symbol, kind="spot_eu"):
    from services.exchanges.mozliwosci import sprawdz_cennik

    async def fetch_one(sql, params=None):
        return {"symbol": symbol} if symbol else None

    return asyncio.run(sprawdz_cennik(adapter, fetch_one, kind))


def test_sprawdzenie_cennika_liczy_wedlug_podanego_kind():
    """`--kind` musi naprawdę dochodzić do CENNIKA, a nie tylko stać w komendzie.

    Podmiot unijny nalicza takera 0,250%, globalny 0,100% — to nie jest różnica
    zaokrąglenia, tylko więcej niż cała runda kosztowa.
    """
    from decimal import Decimal

    a = _AdapterZeStawkami(api_key="k", api_secret="s", mode="demo", host="h")
    symbol, zmierzone, rozjazdy = _sprawdz_cennik(a, "BTCUSDT", kind="spot_eu")
    assert symbol == "BTCUSDT" and zmierzone["taker"] == Decimal("0.0025")
    assert rozjazdy == []

    (opis,) = _sprawdz_cennik(a, "BTCUSDT", kind="spot")[2]
    assert "taker" in opis


def test_odmowa_hosta_nie_jest_zgodnoscia_ani_tracebackiem():
    """Wyjątek przerwałby zadanie czujki na linii przed pomiarem poślizgu,
    a milczące zero powiedziałoby „cennik sprawdzony" o przebiegu, który nie
    dostał ani jednej liczby."""
    a = _AdapterOdmawiajacy(api_key="k", api_secret="s", mode="demo", host="h")
    _, zmierzone, (opis,) = _sprawdz_cennik(a, "BTCUSDT")
    assert zmierzone == {}
    assert "NIESPRAWDZONY" in opis
    assert "10005" in opis or "uprawnieni" in opis   # POWÓD, nie sam werdykt


def test_puste_uniwersum_nie_melduje_zgodnego_cennika():
    """Nie ma na czym zapytać — to jest inny wynik niż zgodność."""
    a = _AdapterZeStawkami(api_key="k", api_secret="s", mode="demo", host="h")
    symbol, zmierzone, (opis,) = _sprawdz_cennik(a, None)
    assert symbol is None and zmierzone == {}
    assert "NIESPRAWDZONY" in opis


@pytest.mark.parametrize("taker, kod", [("0.0025", 0), ("0.0040", 1)])
def test_kod_wyjscia_sprawdzenia_stawek(monkeypatch, capsys, taker, kod):
    """Kod wyjścia jest jedyną rzeczą, którą cron przekłada na RC — więc to on,
    a nie treść komunikatu, decyduje, czy rozjazd będzie w ogóle widoczny."""
    from services.exchanges import mozliwosci

    class _Host(_AdapterZeStawkami):
        pass

    _Host.taker = taker
    _podloz(monkeypatch, [_klucz(1, 5, "jedyny")],
            adapter_cls=_Host, symbol="BTCUSDT")
    monkeypatch.setattr(sys, "argv",
                        ["mozliwosci", "--stawki", "--kind", "spot_eu"])

    assert asyncio.run(mozliwosci.main()) == kod
    out = capsys.readouterr().out
    assert ("ROZJAZD STAWEK" in out) is bool(kod)
    # Liczby przy KAŻDYM przebiegu, nie tylko przy rozjeździe: stawka pełznąca
    # pod progiem jest widoczna, zanim próg przekroczy.
    assert "0.2500%" in out or "0.4000%" in out


def test_status_instrumentow_jest_ODSWIEZANY_I_OCENIANY_cyklicznie():
    """Mechanizm wykrywania delistingu powstał razem z 32 kanarkami i NIE BYŁ
    WOŁANY PRZEZ NIC — ten sam wzorzec, który tropimy w tym projekcie:
    zaprojektowane, okolumnowane, niewpięte.

    Potrzebne są OBA kroki. Bez odświeżenia kolumna statusu zamarza na
    „Trading" i codziennie zapewnia, że wszystko gra; bez oceny nikt tej
    kolumny nie czyta.

    KROKI SĄ W OSOBNYCH ZADANIACH od 2026-09-08 i to jest istota poprawki:
    odświeżanie ZAPISUJE, więc musi chodzić na roli z prawem zapisu, a ocena
    jest odczytem i zostaje w czujce chodzącej na roli tylko do odczytu.
    Trzymanie ich razem kończyło się `permission denied` maskowanym przez
    `|| true` — zadanie meldowało powodzenie, nie odświeżając niczego.
    """
    from pathlib import Path

    szablony = Path("charts/trader/templates")
    wszystko = "\n".join(
        "\n".join(w for w in p.read_text(encoding="utf-8").splitlines()
                  if w.strip() and not w.strip().startswith("#"))
        for p in sorted(szablony.glob("*.yaml")))

    assert "services/ingest/instrumenty.py" in wszystko, (
        "bez odświeżenia status zamarza i czujka po 26 h zgłosi 'nie sprawdzano'")
    assert "dozor.py --co instrumenty" in wszystko, (
        "bez oceny nikt nie czyta zebranego statusu")

    czujka = (szablony / "zywotnosc-cron.yaml").read_text(encoding="utf-8")
    linie = [w.strip() for w in czujka.splitlines()
             if w.strip() and not w.strip().startswith("#")]
    assert not any("services/ingest/instrumenty.py" in w for w in linie), (
        "odświeżanie wróciło do czujki, która chodzi na roli bez prawa zapisu")


def test_odswiezenie_instrumentow_ODROZNIA_stan_bramki_od_AWARII():
    """`instrumenty.py` zwracało 1 w trzech różnych sytuacjach naraz: gdy
    instrument nie ma kompletu reguł (stan bramki egzekucji — na podmiocie
    unijnym NORMALNY, bo 24 par USDT on nie oferuje), gdy wykryto wycofanie,
    i gdy poleciał nieprzechwycony wyjątek.

    Wołający miał więc do wyboru maskować wszystko albo nic — i maskował
    wszystko przez `|| true`. Dlatego `permission denied for table instrument`
    wyglądał identycznie jak „brakuje reguł" i przez 10 godzin nikt nie
    odświeżał statusu, a zadanie kończyło się powodzeniem.
    """
    import ast
    from pathlib import Path

    zrodlo = Path("services/ingest/instrumenty.py").read_text(encoding="utf-8")
    drzewo = ast.parse(zrodlo)
    glowna = next(w for w in drzewo.body
                  if isinstance(w, (ast.AsyncFunctionDef, ast.FunctionDef))
                  and w.name == "main")
    # Stale modulu (`KOD_BRAK_REGUL`, `KOD_WYCOFANIE`) rozwiazane do wartosci -
    # `main` zwraca nazwy, nie literaly (U-15).
    stale = {c.targets[0].id: c.value.value for c in drzewo.body
             if isinstance(c, ast.Assign) and isinstance(c.value, ast.Constant)
             and isinstance(c.targets[0], ast.Name)}
    kody = {w.value.value if isinstance(w.value, ast.Constant) else stale[w.value.id]
            for w in ast.walk(glowna)
            if isinstance(w, ast.Return) and (
                isinstance(w.value, ast.Constant)
                or (isinstance(w.value, ast.Name) and w.value.id in stale))}
    assert 1 not in kody, (
        "kod 1 z `main` jest nieodróżnialny od nieprzechwyconego wyjątku — "
        "awaria wygląda wtedy tak samo jak normalny stan bramki")
    assert {3, 4} <= kody, "brak osobnych kodów dla braku reguł i dla wycofania"

    # Komentarze ODSIANE: opisują właśnie tę wadę, więc test na samej treści
    # pliku łapałby własne uzasadnienie zamiast polecenia.
    zadanie = "\n".join(
        w for w in Path("charts/trader/templates/instrumenty-cron.yaml")
        .read_text(encoding="utf-8").splitlines()
        if w.strip() and not w.strip().startswith("#"))
    assert '[ "$RC" = 3 ] && exit 0' in zadanie, (
        "zadanie nie odsiewa stanu bramki — będzie wiecznie czerwone")
    assert "|| true" not in zadanie, (
        "maskowanie wszystkiego jest dokładnie tą wadą, która ukryła brak "
        "uprawnień na dziesięć godzin")


def test_zaden_modul_nie_ma_definicji_ZA_STRAZNIKIEM_main():
    """Definicja po `if __name__ == "__main__"` istnieje przy imporcie,
    a NIE ISTNIEJE przy uruchomieniu skryptu — bo `main()` startuje, zanim
    interpreter do niej dojdzie.

    Testy tego nie łapią z definicji: import wykonuje cały plik, uruchomienie
    zatrzymuje się na `main()`. Zdarzyło się to naprawdę — `mozliwosci.py`
    przechodził wszystkie testy i wywracał się na produkcji z `NameError`,
    w zadaniu dozoru wołanym co dziesięć minut.
    """
    import ast
    from pathlib import Path

    winne = []
    for plik in sorted(Path("services").rglob("*.py")):
        drzewo = ast.parse(plik.read_text(encoding="utf-8"))
        straznicy = [w for w in drzewo.body
                     if isinstance(w, ast.If)
                     and "__main__" in ast.dump(w.test)]
        if not straznicy:
            continue
        granica = straznicy[-1].lineno
        po = [w for w in drzewo.body
              if w.lineno > granica
              and isinstance(w, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Assign))]
        if po:
            winne.append(f"{plik}: {[getattr(w, 'name', '?') for w in po]}")

    assert not winne, (
        "te definicje nie istnieją przy uruchomieniu skryptu:\n" + "\n".join(winne))
