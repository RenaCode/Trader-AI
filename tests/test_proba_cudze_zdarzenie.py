"""Próba pożarowa nie ma prawa ruszyć CUDZEGO kill-switcha ani zostawić progu.

Dwie wady, obie znalezione w audycie 2026-09-23, obie o tym samym kształcie:
mechanizm, który chroni pieniądze, po cichu wyłącza sam siebie.

  1. ZWOLNIENIE CUDZEGO ZDARZENIA. `moje_zdarzenie = z["id"]` stało PRZED
     sprawdzeniem `limit_name`, a zapytanie brało DOWOLNE niezwolnione
     zdarzenie pary engine/mode. Prawdziwy `max_drawdown`, który zadziałał
     w 420-sekundowym oknie czekania, trafiał więc do `finally` i był tam
     zwalniany notatką „wywołane celowo przez próbę". Handel wracał bez
     decyzji człowieka.

  2. PRÓG ZOSTAJĄCY PO PRZERWANIU. Bez handlera SIGTERM CPython kończy proces
     bez rozwijania stosu, więc `finally` — jedyne miejsce, w którym wraca
     podniesiony `min_equity` — nie wykonuje się wcale.

DLACZEGO ISTNIEJĄCY `test_proba_ZWALNIA_wlasne_zdarzenie_gdy_padnie_w_polowie`
TEGO NIE ŁAPAŁ: podaje w atrapie `limit_name="min_equity"`, czyli zdarzenie
nieodróżnialne od własnego. Gałąź z obcym limitem nie była tam nigdy wykonana.
Tutaj atrapa oddaje zdarzenie o INNYM `limit_name` — i to jest cała różnica
między testem, który coś dowodzi, a testem, który tylko wygląda podobnie.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path

import pytest

from services.engine import proba_killswitcha as pk

KORZEN = Path(__file__).resolve().parents[1]


def _baza(*, zdarzenie_wlasne=None, zdarzenie_obce=None, powiadomien=1,
          wstrzymane=1, wejscia=0, opoznienie=0.0):
    """Atrapa bazy rozróżniająca zapytanie WŁASNE od zapytania o cokolwiek.

    Po poprawce próba pyta dwoma różnymi zapytaniami i to jest sedno sprawy:

      * zawężonym — `limit_name = %s` plus `triggered_at >=` chwilą
        podniesienia progu. Tu odpowiada `zdarzenie_wlasne`.
      * szerokim — dowolne czynne zdarzenie, TYLKO po to, żeby odróżnić
        „silnik nie zadziałał" od „zadziałał, ale na innym limicie".
        Tu odpowiada `zdarzenie_obce`.

    Atrapa oddaje wyłącznie kolumny, o które pyta PRAWDZIWY `SELECT` — kolumna
    podłożona ponad zapytanie czyni test niewrażliwym na to, czy kod ją w ogóle
    pobiera.
    """
    async def fetch_one(sql, params=None):
        p = " ".join(sql.split())
        if opoznienie:
            await asyncio.sleep(opoznienie)
        if "exec.equity_point" in p:
            return {"equity": 10_000}
        if "count(*)" in p and "released_at IS NULL" in p:
            return {"n": 0, "dni_blokady": 0.0}
        if "released_auto = false" in p and "released_by IS NULL" in p:
            return {"dni": 1.0}
        if "notify.notification" in p:
            return {"n": powiadomien}
        if "journal.decision" in p:
            return {"wstrzymane": wstrzymane, "wejscia": wejscia}
        if "risk.killswitch_event" in p:
            # Zapytanie wlasne rozpoznajemy po filtrze, ktory poprawka dodala.
            # Gdyby ktos go usunal, atrapa zacznie oddawac zdarzenie OBCE tam,
            # gdzie proba szuka swojego - czyli test zapali sie natychmiast.
            wlasne = "limit_name = %s" in p and "triggered_at >=" in p
            wiersz = zdarzenie_wlasne if wlasne else zdarzenie_obce
            if wiersz is None:
                return None
            # WYLACZNIE KOLUMNY WYMIENIONE W `SELECT`. Atrapa oddajaca kolumne,
            # ktorej prawdziwe zapytanie nie pobiera, przepuszcza kod siegajacy
            # po klucz, ktorego na produkcji nie bedzie - i to jest znany blad
            # tego projektu, nie hipoteza. Przy okazji dzieki temu ta sama
            # atrapa obsluguje oba zapytania proby (szerokie bierze dwie
            # kolumny, zawezone trzy) i kod SPRZED poprawki tak samo.
            wymienione = [k.strip() for k in re.search(
                r"SELECT (.+?) FROM risk\.killswitch_event", p).group(1).split(",")]
            return {k: v for k, v in wiersz.items() if k in wymienione}
        if "risk.limit_def" in p:
            return {"value": 8_000}
        raise AssertionError(f"atrapa nie zna zapytania: {p}")

    async def fetch_all(sql, params=None):
        return []

    wykonane = []

    async def execute(sql, params=None):
        wykonane.append((" ".join(sql.split()), params))

    return fetch_one, fetch_all, execute, wykonane


def _zwolnienia(wykonane):
    return [(s, p) for s, p in wykonane
            if "UPDATE risk.killswitch_event SET released_at" in s]


# ------------------------------------------------- znalezisko 1: cudze zdarzenie

def test_proba_NIE_ZWALNIA_zdarzenia_o_innym_limicie(monkeypatch, capsys):
    """Sedno wady nr 1, odtworzone co do scenariusza.

    Próba rusza przy czystym koncie (`aktywnych 0`), podnosi próg i czeka na
    cykl silnika. W tym oknie silnik wykrywa PRAWDZIWE obsunięcie kapitału
    i wstawia zdarzenie `max_drawdown`. Przed poprawką `ORDER BY triggered_at
    DESC LIMIT 1` zwracało właśnie je, `moje_zdarzenie` brało jego `id`,
    a `finally` zwalniał prawdziwy kill-switch z notatką twierdzącą, że
    zdarzenie zostało wywołane celowo przez próbę.

    Skutek był potrójny i każdy element osobno jest ciężki: handel wracał bez
    decyzji człowieka; zdarzenie znikało z `released_at IS NULL`, więc
    `dozor --co ryzyko` nigdy więcej o nim nie mówił; a trójka kolumn
    `released_auto = false` / `released_by IS NULL` liczyła to jako ZALICZONĄ
    próbę i odnawiała licznik ciszy.
    """
    # CZEKAJ_SILNIK_S > 0 i maly KROK_S, a nie zero: przy zerze petla
    # `_czekaj_na` nie wykonuje sie ANI RAZU i kod sprzed poprawki nie zdazylby
    # nawet odpytac bazy. Test, ktory na starej wersji nie dochodzi do wadliwej
    # linii, nie jest testem regresyjnym, tylko jej opisem.
    monkeypatch.setattr(pk, "CZEKAJ_SILNIK_S", 0.05)
    monkeypatch.setattr(pk, "KROK_S", 0.01)
    fetch_one, fetch_all, execute, wykonane = _baza(
        zdarzenie_wlasne=None,                       # swojego nie ma
        zdarzenie_obce={"id": 4242, "limit_name": "max_drawdown",
                        "reason": "obsuniecie kapitalu 12%"})

    kod = asyncio.run(pk.uruchom(fetch_all, fetch_one, execute))

    assert not _zwolnienia(wykonane), (
        "próba zwolniła zdarzenie, którego NIE wywołała — prawdziwy "
        "kill-switch zdjęty automatem, z notatką o próbie pożarowej")
    tekst = capsys.readouterr()
    assert kod == 0, (
        "prawdziwy kill-switch na innym limicie to BRAK MOŻLIWOŚCI POMIARU, "
        "a nie awaria łańcucha — czerwony Job codziennie uczy odklikiwać")
    assert "NIEROZSTRZYGNIĘTA" in tekst.err
    assert "max_drawdown" in tekst.err, (
        "komunikat musi nazywać limit, który naprawdę zadziałał — inaczej "
        "człowiek nie wie, że handel stoi z prawdziwego powodu")


def test_prog_WRACA_mimo_cudzego_zdarzenia(monkeypatch):
    """Kanarek do poprzedniego w drugą stronę: nietykanie cudzego zdarzenia nie
    może oznaczać zostawienia podniesionego progu. To dwie różne rzeczy i obie
    muszą być prawdziwe naraz."""
    monkeypatch.setattr(pk, "CZEKAJ_SILNIK_S", 0.05)
    monkeypatch.setattr(pk, "KROK_S", 0.01)
    fetch_one, fetch_all, execute, wykonane = _baza(
        zdarzenie_wlasne=None,
        zdarzenie_obce={"id": 4242, "limit_name": "max_drawdown",
                        "reason": "obsuniecie kapitalu 12%"})
    asyncio.run(pk.uruchom(fetch_all, fetch_one, execute))

    przywrocenia = [(s, p) for s, p in wykonane
                    if "UPDATE risk.limit_def" in s and "proba_do = NULL" in s]
    assert przywrocenia, "próg nie wrócił — silnik zostaje wstrzymany artefaktem"
    assert przywrocenia[-1][1][0] == 8_000, "wrócił inny próg niż sprzed próby"


def test_zapytanie_o_zdarzenie_jest_ZAWEZONE_do_limitu_i_okna():
    """Samo przestawienie przypisania pod sprawdzenie `limit_name` nie wystarcza.

    Zapytanie bez filtru po limicie i bez dolnej granicy czasu nadal zwracałoby
    cudze zdarzenie — tyle że próba kończyłaby się `Niezaliczona` zamiast
    zwolnieniem. Awaria byłaby lżejsza, ale diagnoza fałszywa: „silnik nie
    uruchomił kill-switcha" przy silniku, który go uruchomił.

    Granica dolna czytana jest ZEGAREM BAZY (`updated_at` tego samego UPDATE-u,
    który podniósł próg), a nie liczona w Pythonie — strefa pomylona o dwie
    godziny przy oknie kilkunastominutowym daje okno całkowicie rozminięte
    z próbą.
    """
    zrodlo = (KORZEN / "services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    kod = " ".join(w for w in zrodlo.splitlines()
                   if not w.lstrip().startswith("#"))
    kod = " ".join(kod.split())
    assert "AND limit_name = %s" in kod, (
        "zapytanie o zdarzenie próby bez filtru po limicie — złapie cudze")
    assert "AND triggered_at >= (SELECT updated_at FROM risk.limit_def" in kod, (
        "brak dolnej granicy czasu czytanej z bazy — zdarzenie sprzed próby "
        "nadal może się pod nią podszyć")


def test_przypisanie_wlasnego_id_stoi_PO_sprawdzeniu_limitu():
    """Kolejność dwóch linii jest tu całym zabezpieczeniem.

    `finally` czyta `moje_zdarzenie`, więc każda ścieżka między przypisaniem
    a sprawdzeniem zwalnia zdarzenie, którego przynależności nikt jeszcze nie
    potwierdził. Test patrzy na kolejność w kodzie, bo to jedyne miejsce,
    w którym ta wada istnieje.
    """
    zrodlo = (KORZEN / "services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    sprawdzenie = kod.index('if z["limit_name"] != LIMIT_PROBY')
    przypisanie = kod.index('moje_zdarzenie = z["id"]')
    assert sprawdzenie < przypisanie, (
        "`moje_zdarzenie` dostaje id ZANIM ktokolwiek sprawdził, czy to "
        "zdarzenie należy do próby — `finally` zwolni cudzy kill-switch")


# --------------------------------------------------- znalezisko 2: SIGTERM

def test_SIGTERM_w_polowie_proby_PRZYWRACA_prog(monkeypatch):
    """Jedyny sposób, w jaki ten Job bywa przerywany — i przez rok nie działał.

    `activeDeadlineSeconds` wysyła SIGTERM. Bez handlera CPython kończy proces
    BEZ rozwijania stosu, więc `finally` się nie wykonuje i podniesiony
    `min_equity` zostaje NA STAŁE. A `min_equity` ma regułę `reczne`
    (migracja 0029), więc `wznowienie.wznow` go nie rusza: silnik stoi
    bezterminowo, a każde ręczne zwolnienie kill-switcha kończy się ponownym
    wystrzeleniem w następnym cyklu, bo przyczyna — próg — nadal tam jest.

    Test wysyła PRAWDZIWY sygnał do własnego procesu, a nie woła handlera
    ręcznie: sprawdzana jest droga od `add_signal_handler` przez anulowanie
    zadania do bloku `finally`, a nie sam fakt, że funkcja istnieje.
    """
    # Proba ma byc PRZERWANA W POLOWIE, czyli w chwili sygnalu musi na czyms
    # stac. Ustawiamy ja wiec na czekaniu na ogniwo 4 (dozor milczy), z krokiem
    # skroconym do milisekund - przebieg jest wtedy ten sam co na produkcji,
    # tylko szybszy. Bez tego caly przebieg konczy sie przed wyslaniem sygnalu
    # i test mierzy sciezke sukcesu, nie sciezke przerwania.
    monkeypatch.setattr(pk, "KROK_S", 0.01)
    fetch_one, fetch_all, execute, wykonane = _baza(
        zdarzenie_wlasne={"id": 77, "reason": "kapital ponizej progu",
                          "limit_name": "min_equity"},
        powiadomien=0)

    async def scenariusz():
        zadanie = asyncio.ensure_future(
            pk.przeprowadz(fetch_all, fetch_one, execute))
        pk.zainstaluj_przerwanie(zadanie)
        # Sygnal po chwili: proba ma zdazyc podniesc prog i wejsc w czekanie.
        asyncio.get_running_loop().call_later(
            0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))
        with pytest.raises(asyncio.CancelledError):
            await zadanie

    asyncio.run(scenariusz())

    podniesienia = [(s, p) for s, p in wykonane
                    if "UPDATE risk.limit_def" in s and "proba_do = now()" in s]
    assert podniesienia, "próba nie zdążyła podnieść progu — scenariusz nie zaszedł"
    przywrocenia = [(s, p) for s, p in wykonane
                    if "UPDATE risk.limit_def" in s and "proba_do = NULL" in s]
    assert przywrocenia, (
        "SIGTERM przerwał próbę i podniesiony próg ZOSTAŁ — silnik stoi "
        "bezterminowo na limicie o regule ręcznej, czyli dokładnie ta awaria, "
        "przed którą kill-switch ma chronić")
    assert przywrocenia[-1][1][0] == 8_000, "wrócił inny próg niż sprzed próby"


def test_handler_lapie_SIGTERM_a_nie_tylko_SIGINT():
    """Kanarek: SIGINT łapie się „przy okazji" (KeyboardInterrupt i tak rozwija
    stos), więc handler obsługujący wyłącznie jego wyglądałby na poprawkę
    i nie zmieniałby niczego dla Kubernetesa."""
    import inspect
    zrodlo = inspect.getsource(pk.zainstaluj_przerwanie)
    assert "signal.SIGTERM" in zrodlo, (
        "handler nie obejmuje SIGTERM — a to jedyny sygnał, jaki wysyła "
        "`activeDeadlineSeconds`")


def test_przerwanie_ma_WLASNY_kod_wyjscia():
    """Kody 0/1/2 mówią o WYNIKU POMIARU. Przerwanie z zewnątrz nie jest
    wynikiem pomiaru i zlanie go z którymkolwiek z tamtych zamazuje jedyną
    informację, jaką taki przebieg niesie."""
    assert pk.KOD_PRZERWANA not in (0, 1, 2)
    assert pk.KOD_PRZERWANA == 128 + int(signal.SIGTERM)
    zrodlo = (KORZEN / "services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    assert "except asyncio.CancelledError" in zrodlo, (
        "`main` nie zamienia anulowania na kod wyjścia — CancelledError "
        "wyleci z `asyncio.run` jako ślad stosu, a Job dostanie kod 1, "
        "czyli 'łańcuch padł'")


def test_okno_proby_MIESCI_SIE_w_limicie_czasu_zadania():
    """Sam handler to ostrzeżenie, nie zabezpieczenie — po okresie karencji
    idzie SIGKILL, którego nie ominie żaden kod. Jeśli `activeDeadlineSeconds`
    jest KRÓTSZY niż `OKNO_PROBY_S`, chart sam zakłada, że próba bywa zabijana
    przed końcem własnego okna, i cała nadzieja wisi na 30 sekundach karencji.

    Ten sam warunek sprawdza `test_chart_nadzor.py` od strony chartu. Tutaj
    stoi od strony kodu, bo to `OKNO_PROBY_S` może urosnąć niepostrzeżenie.
    """
    szablon = (KORZEN / "charts/trader/templates/proba-cron.yaml").read_text(encoding="utf-8")
    limit = int(re.search(r"activeDeadlineSeconds:\s*(\d+)", szablon).group(1))
    assert limit > pk.OKNO_PROBY_S, (
        f"activeDeadlineSeconds={limit} przy OKNO_PROBY_S={pk.OKNO_PROBY_S} — "
        f"Job ginie zanim próba skończy, a wtedy wszystko zależy od tego, czy "
        f"`finally` zmieści się w okresie karencji")
