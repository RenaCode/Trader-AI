"""Pętla procesu tła należy do serwisu, nie do powłoki.

CO SIĘ STAŁO

Pięć podów chodziło na komendzie kształtu
`sh -c "while true; do python ... || true; sleep N; done"`. Trzy skutki, każdy
zły i **żaden widoczny**:

* `|| true` połykał KAŻDY niezerowy wynik, więc awaria nie restartowała
  kontenera ani nie podnosiła licznika restartów. Pod stał „Running", k3s
  meldował zdrowie, a proces mógł nie robić nic od dni;
* proces startował od nowa co cykl, więc nie miał pojęcia o awariach
  poprzednich — pięć padniętych cykli z rzędu wyglądało jak pięć udanych;
* w pętli wewnętrznej (tam, gdzie już była) jedno połączenie z bazą żyło cały
  proces: po restarcie bazy każdy kolejny cykl padał na tym samym zerwanym
  gnieździe, w nieskończoność — proces martwy i wyglądający na żywy.

Testy poniżej pilnują trzech niezależnych rzeczy: że kształt nie wróci do
chartu, że moduł ma własną pętlę, i że ta pętla naprawdę **kończy proces** po
serii awarii oraz **łączy się na każdy cykl**.
"""
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from psycopg.rows import dict_row, tuple_row

KORZEN = Path(__file__).resolve().parents[1]
VALUES = yaml.safe_load((KORZEN / "charts" / "trader" / "values.yaml").read_text())
WORKERY = VALUES["workers"]
WLACZONE = sorted(n for n, w in WORKERY.items() if w.get("enabled"))

# `python sciezka/plik.py` w komendzie - takze wewnatrz `sh -c`.
WZOR_MODULU = re.compile(r"\bpython3?\s+(?!-)([\w./-]+\.py)\b")


def komenda(nazwa: str) -> str:
    return " ".join(WORKERY[nazwa]["command"])


def test_sa_jakies_workery():
    """Kanarek: gdyby sekcja zmieniła nazwę, reszta testów przechodziłaby pusto
    i nie pilnowałaby niczego."""
    assert WORKERY, "brak sekcji `workers` w values.yaml — test niczego nie sprawdza"
    assert WLACZONE, "żaden worker nie jest włączony — test niczego nie sprawdza"


# --- kształt komendy -------------------------------------------------------
@pytest.mark.parametrize("nazwa", sorted(WORKERY))
def test_zadna_komenda_nie_ma_petli_w_shellu(nazwa):
    """Także dla workerów WYŁĄCZONYCH.

    Martwy wpis z `while true` w środku to nie martwy kod, tylko wzorzec do
    skopiowania przy następnym włączeniu.
    """
    cmd = komenda(nazwa).lower()
    for zakazane, powod in (
        ("while true", "pętla należy do modułu, nie do powłoki"),
        ("|| true", "połyka niezerowy wynik: awaria nie restartuje poda"),
        ("; done", "pozostałość po pętli powłoki"),
        ("for ", "pętla po horyzontach należy do modułu"),
    ):
        assert zakazane not in cmd, f"worker `{nazwa}`: {zakazane!r} — {powod}"


@pytest.mark.parametrize("nazwa", sorted(WORKERY))
def test_wolane_moduly_istnieja(nazwa):
    for sciezka in WZOR_MODULU.findall(komenda(nazwa)):
        assert (KORZEN / sciezka).is_file(), \
            f"worker `{nazwa}` woła nieistniejący plik {sciezka}"


@pytest.mark.parametrize("nazwa", WLACZONE)
def test_wlaczony_worker_ma_wlasna_petle_w_module(nazwa):
    """Worker to proces DŁUGOŻYJĄCY. Skoro powłoka go już nie zapętla, pętla
    musi być w module — inaczej pod kończyłby się po jednym cyklu i wpadał
    w restart co kilkanaście sekund."""
    moduly = WZOR_MODULU.findall(komenda(nazwa))
    assert moduly, f"worker `{nazwa}`: nie rozpoznano wołanego modułu"
    for sciezka in moduly:
        zrodlo = (KORZEN / sciezka).read_text()
        assert "while True" in zrodlo, \
            f"worker `{nazwa}`: {sciezka} nie ma własnej pętli"


def test_lista_horyzontow_cech_zgadza_sie_z_trenerem():
    """Rozjazd nie objawia się awarią.

    Trener uczy się wtedy na cechach, które przestały przychodzić, a wynik
    degraduje się po cichu z każdą dobą. Zdarzyło się to 2026-09-04: drugi
    horyzont doszedł do trenera, a nie doszedł do liczenia cech, i cechy 1h
    stanęły na 21 godzin.
    """
    cmd = WORKERY["features"]["command"]
    bar = cmd[cmd.index("--bar") + 1]
    cechy = sorted(b.strip() for b in bar.split(","))
    trener = sorted(VALUES["trainerCron"]["horyzonty"])
    assert cechy == trener, (
        f"cechy liczone dla {cechy}, a trener uczy się na {trener} — "
        f"tor bez cech degraduje się po cichu")


# --- parsowanie listy świec ------------------------------------------------
def test_parsuj_bary_przyjmuje_liste():
    from services.ingest.features import parsuj_bary

    assert parsuj_bary("4h,1h") == ["4h", "1h"]
    assert parsuj_bary(" 4h , 1h ") == ["4h", "1h"]
    assert parsuj_bary("4h") == ["4h"]
    assert parsuj_bary("4h,4h") == ["4h"], "powtórzenie liczyłoby ten sam horyzont dwa razy"


def test_parsuj_bary_konczy_proces_na_nieznanej_swiecy():
    """Literówka w charcie ma zabić pod od razu, a nie liczyć w kółko cechy
    dla jednego z dwóch horyzontów."""
    from services.ingest.features import parsuj_bary

    with pytest.raises(SystemExit) as e:
        parsuj_bary("4h,1H")
    assert "1H" in str(e.value)


# --- atrapy bazy -----------------------------------------------------------
class Stop(BaseException):
    """Awaryjne wyjście z pętli testowej.

    Dziedziczy z BaseException CELOWO: gdyby licznik awarii przestał kończyć
    proces, `except Exception` w module połknąłby zwykły wyjątek i test wisiałby
    w nieskończonej pętli zamiast paść.
    """


class AtrapaPolaczenia:
    def __init__(self, fabryka=dict_row):
        self.fabryka = fabryka
        self.rollbacki = 0
        # Zapytania puszczone PROSTO z połączenia (`conn.execute`), a nie przez
        # kursor — tak zapisuje się puls workera (services/ingest/puls.py).
        # Atrapa bez tej metody kończyłaby udany cykl `AttributeError`, czyli
        # zamieniałaby brak pulsu w awarię cyklu.
        self.zapytania = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.zapytania.append((sql, params))
        return AtrapaKursora(self.fabryka)

    def rollback(self):
        self.rollbacki += 1

    def commit(self):
        pass

    def cursor(self, row_factory=None):
        return AtrapaKursora(row_factory or self.fabryka)


class AtrapaKursora:
    """Oddaje wiersz W KSZTAŁCIE, o który poprosił kursor — jak psycopg."""

    def __init__(self, fabryka):
        self.fabryka = fabryka

    def execute(self, *a, **kw):
        return self

    def fetchone(self):
        if self.fabryka is dict_row:
            return {"lo": None, "hi": None}
        return (None, None)

    def fetchall(self):
        return []


# --- kształt wiersza nie może zależeć od tego, kto otworzył połączenie ------
def test_backfill_czyta_krotke_takze_z_polaczenia_dict_row():
    """`history_worker` otwiera połączenie z `dict_row` (potrzebuje go funding)
    i podaje je obu zadaniom.

    `backfill_symbol` rozpakowuje krotkę: `have_from, have_until = fetchone()`.
    Na wierszu słownikowym rozpakowanie UDAJE SIĘ BEZ BŁĘDU — tyle że na
    NAZWACH kolumn. Backfill dostawał wtedy „datę" będącą napisem i wywracał
    się dopiero w porównaniu głęboko niżej.
    """
    from services.ingest import backfill

    conn = AtrapaPolaczenia(fabryka=dict_row)
    # `since` w przyszłości: zakres jest pusty, więc nic nie leci na giełdę
    # i adapter nie jest w ogóle potrzebny.
    since = datetime.now(timezone.utc) + timedelta(hours=1)
    ile = asyncio.run(backfill.backfill_symbol(None, conn, 1, "BTCUSDT", "1h", since))
    assert ile == 0


def test_funding_czyta_slownik_takze_z_polaczenia_tuple_row():
    """Odwrotna strona tej samej pułapki: `backfill` w funding.py czyta
    `row["lo"]`, więc krotka odziedziczona po połączeniu dałaby TypeError
    dopiero na produkcji."""
    from services.ingest import funding

    conn = AtrapaPolaczenia(fabryka=tuple_row)
    since = datetime.now(timezone.utc) + timedelta(hours=1)
    ile = asyncio.run(funding.backfill(None, conn, 1, "BTCUSDT.P", since))
    assert ile == 0


# --- pętla cech ------------------------------------------------------------
def uruchom_cechy(monkeypatch, capsys, *, wyniki, argv):
    """Odpala `features.main` z atrapą bazy i sterowanym wynikiem przebiegu.

    `wyniki` to lista wartości na kolejne wywołania `przebieg`: liczba = sukces,
    wyjątek = awaria. Po wyczerpaniu listy leci `Stop`, żeby zepsuty licznik
    awarii kończył test, a nie zawieszał go na zawsze.
    """
    from services.ingest import features

    polaczenia = []
    kolejka = list(wyniki)

    def fake_connect(url, **kw):
        polaczenia.append(url)
        return AtrapaPolaczenia()

    def fake_przebieg(conn, h, **kw):
        if not kolejka:
            raise Stop("pętla nie zakończyła procesu")
        v = kolejka.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(features.psycopg, "connect", fake_connect)
    monkeypatch.setattr(features, "przebieg", fake_przebieg)
    monkeypatch.setattr(sys, "argv", ["features.py", *argv])
    kod = features.main()
    return kod, polaczenia, capsys.readouterr().out


def test_cechy_koncza_proces_po_pieciu_awariach(monkeypatch, capsys):
    """Wpis w logu NIE JEST widocznością — logu poda nikt nie czyta. Restart
    poda jest: rośnie licznik restartów i pod wpada w CrashLoopBackOff."""
    kod, polaczenia, out = uruchom_cechy(
        monkeypatch, capsys,
        wyniki=[RuntimeError("baza padła")] * 5,
        argv=["--bar", "4h", "--interval", "0"])
    assert kod == 1, "pięć awarii pod rząd nie zakończyło procesu"
    assert "kończę proces" in out
    assert len(polaczenia) == 5, (
        f"połączenie otwarte {len(polaczenia)} raz(y) na 5 cykli — musi być "
        f"otwierane NA KAŻDY CYKL, inaczej po restarcie bazy proces zapętla się "
        f"na zerwanym gnieździe")


def test_cechy_zeruja_licznik_po_udanym_cyklu(monkeypatch, capsys):
    """Cztery awarie, sukces, cztery awarie — to nie jest osiem pod rząd.

    Bez zerowania proces ginąłby po serii rozrzuconej w czasie, czyli przy
    zwykłym, przejściowym zrywaniu połączenia.
    """
    # Proces MA dożyć do wyczerpania listy — wtedy atrapa rzuca `Stop`. Gdyby
    # licznik się nie zerował, `main` zwróciłby 1 po piątej awarii i `Stop`
    # nigdy by nie poleciał.
    with pytest.raises(Stop):
        uruchom_cechy(monkeypatch, capsys,
                      wyniki=[RuntimeError("x")] * 4 + [7] + [RuntimeError("x")] * 4,
                      argv=["--bar", "4h", "--interval", "0"])
    out = capsys.readouterr().out
    assert "cykl nieudany (5/5)" not in out, \
        "licznik nie zerował się po udanym cyklu — proces ginąłby od awarii " \
        "rozrzuconych w czasie, czyli od zwykłego zrywania połączenia"


def test_cechy_wypisuja_opisy_bledow_takze_przy_quiet(monkeypatch, capsys):
    """`--quiet` ma wyciszać RUTYNOWY POSTĘP, a nie awarie.

    Na produkcji ten sam kształt zostawiał sam licznik („błędów 11") bez ani
    jednego słowa o tym, czym te błędy były.
    """
    _, _, out = uruchom_cechy(
        monkeypatch, capsys,
        wyniki=[RuntimeError("kolumna nie istnieje")] * 5,
        argv=["--bar", "4h", "--interval", "0", "--quiet"])
    assert "kolumna nie istnieje" in out, \
        "--quiet wyciszył opis awarii, zostawiając sam licznik"


def test_cechy_licza_wszystkie_horyzonty_w_jednym_cyklu(monkeypatch, capsys):
    """Awaria jednego horyzontu nie może pominąć drugiego w tym samym cyklu —
    to jedyne, co było dobrego w `|| true`."""
    from services.ingest import features

    liczone = []

    def fake_connect(url, **kw):
        return AtrapaPolaczenia()

    def fake_przebieg(conn, h, **kw):
        liczone.append(h.bar)
        if h.bar == "4h":
            raise RuntimeError("4h padło")
        return 3

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(features.psycopg, "connect", fake_connect)
    monkeypatch.setattr(features, "przebieg", fake_przebieg)
    monkeypatch.setattr(sys, "argv",
                        ["features.py", "--bar", "4h,1h", "--once"])
    kod = features.main()
    assert liczone == ["4h", "1h"], "awaria 4h pominęła 1h w tym samym cyklu"
    assert kod == 1, "cykl z awarią zwrócił zero"


def test_backfill_cech_nie_wpada_w_petle(monkeypatch, capsys):
    """`--backfill --days 1400` to operacja ręczna na dziesiątki minut.

    Wpadnięcie z nią w pętlę domyślną oznaczałoby przeliczanie całej historii
    w kółko — a w logu wygląda to dokładnie jak długo trwający backfill.
    """
    from services.ingest import features

    wywolania = []

    def fake_przebieg(conn, h, **kw):
        wywolania.append(h.bar)
        # Bez tego zabezpieczenia zepsuty warunek `jednorazowo` ZAWIESILBY test
        # (przebiegi się udają, więc licznik awarii nigdy nie zakończy procesu),
        # a test, który wisi, nie jest testem, który pada.
        if len(wywolania) > 2:
            raise Stop("--backfill wpadł w pętlę")
        return 1

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(features.psycopg, "connect", lambda url, **kw: AtrapaPolaczenia())
    monkeypatch.setattr(features, "przebieg", fake_przebieg)
    monkeypatch.setattr(sys, "argv",
                        ["features.py", "--bar", "4h", "--backfill", "--days", "1400",
                         "--interval", "0"])
    assert features.main() == 0
    assert wywolania == ["4h"], "--backfill zapętlił się mimo braku --once"


# --- pętla historii --------------------------------------------------------
def test_historia_konczy_proces_po_pieciu_awariach(monkeypatch, capsys):
    from services.ingest import history_worker

    polaczenia = []

    async def pada(*a, **kw):
        raise RuntimeError("giełda nie odpowiada")

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(history_worker.psycopg, "connect",
                        lambda url, **kw: polaczenia.append(url) or AtrapaPolaczenia())
    monkeypatch.setattr(history_worker.swiece, "przebieg", pada)
    monkeypatch.setattr(history_worker.stawki, "przebieg", pada)
    monkeypatch.setattr(sys, "argv",
                        ["history_worker.py", "--interval", "0", "--quiet"])

    assert asyncio.run(history_worker.main()) == 1
    out = capsys.readouterr().out
    assert "kończę proces" in out
    assert "giełda nie odpowiada" in out, \
        "--quiet wyciszył opis awarii, zostawiając sam licznik"
    assert len(polaczenia) == 5, (
        f"połączenie otwarte {len(polaczenia)} raz(y) na 5 cykli — musi być "
        f"otwierane NA KAŻDY CYKL")


def test_historia_liczy_nieudany_rynek_jako_awarie_cyklu(monkeypatch, capsys):
    """Stawki jednego rynku wracają jako lista błędów, nie jako wyjątek.

    Gdyby cykl uznawał to za sukces, instrument, którego giełda nie zna, mógłby
    milczeć miesiącami przy zerowym liczniku restartów.
    """
    from services.ingest import history_worker

    async def swiece_ok(*a, **kw):
        return 12

    async def stawki_z_bledem(*a, **kw):
        return 0, 3, ["BTCUSDT.P: Symbol Invalid"]

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(history_worker.psycopg, "connect",
                        lambda url, **kw: AtrapaPolaczenia())
    monkeypatch.setattr(history_worker.swiece, "przebieg", swiece_ok)
    monkeypatch.setattr(history_worker.stawki, "przebieg", stawki_z_bledem)
    monkeypatch.setattr(sys, "argv", ["history_worker.py", "--once", "--quiet"])

    assert asyncio.run(history_worker.main()) == 1, \
        "nieudany rynek nie policzył się jako awaria cyklu"
    assert "Symbol Invalid" in capsys.readouterr().out


def test_historia_pobiera_swiece_mimo_awarii_fundingu(monkeypatch, capsys):
    """Dwa zadania w jednym procesie, ale nie w jednym `try`: awaria fundingu
    nie może skasować świec pobranych przed chwilą."""
    from services.ingest import history_worker

    zrobione = []

    async def swiece_ok(*a, **kw):
        zrobione.append("swiece")
        return 5

    async def funding_pada(*a, **kw):
        raise RuntimeError("funding padł")

    monkeypatch.setenv("DATABASE_URL", "postgresql://atrapa")
    monkeypatch.setattr(history_worker.psycopg, "connect",
                        lambda url, **kw: AtrapaPolaczenia())
    monkeypatch.setattr(history_worker.swiece, "przebieg", swiece_ok)
    monkeypatch.setattr(history_worker.stawki, "przebieg", funding_pada)
    monkeypatch.setattr(sys, "argv", ["history_worker.py", "--once"])

    assert asyncio.run(history_worker.main()) == 1
    assert zrobione == ["swiece"]
    assert "świece +5" in capsys.readouterr().out


# --- trener zostaje zadaniem cyklicznym ------------------------------------
def test_trener_nie_jest_workerem():
    """Trening trwa dziesiątki minut i musi dobiec końca niezależnie od wdrożeń.

    Pętla w Deploymencie ginęła przy każdym wdrożeniu — także takim, które
    trenera w ogóle nie dotyczyło. Dlatego jest CronJobem, a wpis w `workers`
    ma pozostać wyłączony: dwa źródła prawdy o parametrach treningu rozjechałyby
    się po cichu.
    """
    assert WORKERY["trainer"]["enabled"] is False, \
        "trener włączony jako worker — duplikuje CronJob (trainer-cron.yaml)"
    assert VALUES["trainerCron"]["enabled"] is True
    assert "--interval" not in komenda("trainer"), \
        "trener nie ma pętli: raz uruchomiony CronJob przeżywa restart Deploymentów"


# --- łańcuch świeca -> decyzja ---------------------------------------------
def _interwal(nazwa: str) -> int:
    cmd = WORKERY[nazwa]["command"]
    return int(cmd[cmd.index("--interval") + 1])


@pytest.mark.parametrize("nazwa", ["history", "features"])
def test_potok_cech_krypto_nie_doklada_wiecej_niz_piec_minut(nazwa):
    """Łańcuch market -> history -> features -> engine jest zegarowy: każde
    ogniwo dokłada losowe 0..interwał do zwłoki decyzji po zamknięciu świecy.

    Przy `history` 1200 s i `features` 900 s najgorszy przypadek wynosił 40 min,
    a kupno wypełniało się 76-95 pb nad ceną decyzji przy zwłoce 63-88 min
    (audyt Fable, F-4.1). 300 s na obu ogniwach to najgorszy przypadek 15 min;
    koszt to 4x więcej zapytań o świece 1h - pomijalne wobec limitu Bybita.
    """
    assert _interwal(nazwa) <= 300, \
        f"{nazwa} chodzi co {_interwal(nazwa)} s - zwłoka decyzji po świecy rośnie"
