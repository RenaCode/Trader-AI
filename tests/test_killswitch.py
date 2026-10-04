"""Kill-switch: cały łańcuch, ogniwo po ogniwie.

DLACZEGO OSOBNY PLIK

Kill-switch to jedyny mechanizm w tym systemie, którego awaria nie objawia się
niczym. Silnik po jego zadziałaniu wygląda dokładnie tak samo jak silnik, który
nie ma sygnału do wejścia: zero decyzji `enter`, żadnego błędu w logu, heartbeat
bije dalej.

Łańcuch ma pięć ogniw i każde jest tu sprawdzane osobno:

    1. limit przekroczony  -> werdykt żąda kill-switcha
    2. żądanie             -> wiersz w risk.killswitch_event
    3. aktywny             -> wejścia wstrzymane
    4. aktywny             -> WYJŚCIA NADAL DZIAŁAJĄ
    5. człowiek            -> zwolnienie, z powodem i śladem

Ogniwo 5 nie istniało do 2026-09-07. Kolumny `released_at`, `released_by`
i `release_note` były w schemacie od migracji 0007, ale router ryzyka miał
wyłącznie GET i nikt nie pisał do tej tabeli. Kill-switch był drzwiami
jednokierunkowymi — a powiadomienie o jego zadziałaniu odsyłało do zakładki
w interfejsie, która pokazywała komunikat „odblokowanie wymaga ręcznego
potwierdzenia" i nie dawała czym potwierdzić.
"""
import asyncio
import re
from decimal import Decimal
from pathlib import Path

import pytest

from services.engine.risk_rules import DEFAULT_LIMITS, RiskState, evaluate_account


def konto(equity, peak, day_ago=None, killswitch=None) -> RiskState:
    return RiskState(equity=Decimal(str(equity)), peak=Decimal(str(peak)),
                     day_ago=Decimal(str(day_ago if day_ago is not None else equity)),
                     exposure=Decimal("0"), per_instrument={},
                     orders_last_hour=0, killswitch=killswitch)


# ------------------------------------------------- ogniwo 1: limit -> żądanie

@pytest.mark.parametrize("nazwa,stan", [
    ("obsunięcie", konto(85_000, 100_000)),
    ("strata dzienna", konto(97_000, 100_000, day_ago=100_000)),
    ("próg kapitału", konto(94_000, 96_000)),
])
def test_kazdy_limit_krytyczny_zada_killswitcha(nazwa, stan):
    """Trzy limity mają akcję `killswitch`. Limit, który blokuje, ale nie żąda
    zatrzymania, zostawia system w stanie 'nie wchodzi i nie wiadomo dlaczego'."""
    v = evaluate_account(stan, DEFAULT_LIMITS)
    assert v.verdict == "block", f"{nazwa}: miało zablokować"
    assert v.trigger_killswitch, f"{nazwa}: zablokowało, ale nie zażądało kill-switcha"


def test_zdrowe_konto_NIE_zada_killswitcha():
    """Kanarek do poprzedniego. Gdyby żądanie szło zawsze, testy wyżej
    przechodziłyby, a system zatrzymywałby się na pierwszym cyklu."""
    v = evaluate_account(konto(100_000, 100_000), DEFAULT_LIMITS)
    assert v.verdict == "allow" and not v.trigger_killswitch


# --------------------------------------- ogniwo 2 i 3: aktywny -> blokada wejść

def test_aktywny_killswitch_blokuje_zanim_cokolwiek_zostanie_policzone():
    """Kill-switch ma pierwszeństwo przed każdym innym limitem. Inaczej system
    mógłby zmniejszyć zlecenie i wysłać je mimo zatrzymania."""
    v = evaluate_account(konto(100_000, 100_000, killswitch={"reason": "test"}),
                         DEFAULT_LIMITS)
    assert v.verdict == "block"
    assert "Kill-switch" in (v.reason or "")


def test_aktywny_killswitch_NIE_zada_kolejnego():
    """Bez tego każdy cykl dokładałby wiersz do risk.killswitch_event, a dozór
    zgłaszałby to samo zdarzenie w kółko — alert, który przychodzi zawsze,
    przestaje cokolwiek znaczyć."""
    v = evaluate_account(konto(50_000, 100_000, killswitch={"reason": "test"}),
                         DEFAULT_LIMITS)
    assert not v.trigger_killswitch, "aktywny kill-switch nie może żądać kolejnego"


# ---------------------------------------------- ogniwo 4: wyjścia nadal działają
#
# Sprawdzane WYKONANIEM `run_once` (wejście wstrzymane, wyjście idzie dalej,
# także na giełdę): `tests/test_run_once.py`, audyt Fable U-1.


# ------------------------------------------------- ogniwo 5: zwolnienie ręczne

def test_zwolnienie_wymaga_POWODU_a_nie_tylko_kliknięcia():
    """Kill-switch nie wygasa sam, bo automatyczne wznowienie po obsunięciu to
    najprostsza droga do powtórzenia tej samej straty. Zwolnienie jednym
    kliknięciem jest tym samym wznowieniem — tyle że z krokiem, który daje
    złudzenie namysłu."""
    from services.api.routers.risk import ZwolnienieBody

    ZwolnienieBody(note="dane świec poprawione w sha-abc1234", haslo="x")
    for zly in ("", "ok", "bo tak"):
        with pytest.raises(Exception):
            ZwolnienieBody(note=zly, haslo="x")


def test_endpoint_zwolnienia_ISTNIEJE_i_pisze_do_wlasciwych_kolumn():
    """Ogniwo, którego brakowało. Kolumny były od 0007, ścieżki zapisu nie."""
    zrodlo = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "@router.post(" in zrodlo, "router ryzyka miał wyłącznie GET"
    assert "UPDATE risk.killswitch_event" in zrodlo
    for kolumna in ("released_at", "released_by", "release_note"):
        assert kolumna in zrodlo, f"zwolnienie nie zapisuje {kolumna}"


def test_zwolnienie_jest_chronione_i_zostawia_slad():
    """Zwolnienie kill-switcha pozwala systemowi znowu ryzykować pieniądze.
    Bez CSRF, bez sesji i bez wpisu w audycie byłoby to jedyne takie miejsce
    w aplikacji."""
    zrodlo = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "require_csrf" in zrodlo
    assert "current_user" in zrodlo
    assert "risk.killswitch_released" in zrodlo, "brak wpisu w dzienniku audytu"


def test_powtorzone_zwolnienie_jest_bledem_a_nie_cisza():
    """409, nie 200. Powtórzone zwolnienie znaczy, że ktoś widzi inny stan niż
    baza; ciche potwierdzenie zostawiłoby go w tym przekonaniu."""
    zrodlo = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    # Szukamy WYWOLANIA, nie liczby: "409" wystepuje tez w komentarzu obok,
    # wiec sam ciag przechodzil takze po usunieciu warunku. Sprawdzone
    # wstawieniem bledu - test milczal.
    assert "HTTPException(409" in zrodlo, "powtórzone zwolnienie nie zwraca 409"


def test_interfejs_ma_czym_zwolnic():
    """Powiadomienie mówi 'Zwolnienie: zakładka Ryzyko w interfejsie'.
    Zakładka pokazywała komunikat o ręcznym potwierdzeniu i nie miała
    kontrolki — instrukcja odsyłała do rzeczy, której nie było."""
    zrodlo = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    assert "/risk/killswitch/" in zrodlo and "release" in zrodlo, (
        "zakładka Ryzyko nie wywołuje zwolnienia")
    assert "Powód zwolnienia" in zrodlo, "brak pola na powód"


def test_nowy_limit_ma_nazwe_w_interfejsie():
    """Limit bez tłumaczenia wyświetla się surową nazwą kolumny i wygląda jak
    usterka — a to jedyne miejsce, w którym widać, gdzie stoi podłoga."""
    zrodlo = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    assert "min_equity:" in zrodlo, "próg kapitału bez nazwy w interfejsie"


# ------------------------------------------------------------ próba pożarowa
#
# Skrypt .sh na VPS-ie zastąpiony modułem w obrazie (2026-09-07). Plik na dysku
# rozjeżdża się z kodem, który testuje: obraz idzie przez wdrożenie, plik nie
# idzie nigdzie. Do tego próba uruchamiana ręcznie zostanie uruchomiona raz.

def test_proba_przywraca_prog_w_FINALLY_a_nie_na_koncu_bloku():
    """Próba podnosi próg kapitału powyżej bieżącego stanu, żeby wywołać
    prawdziwe zadziałanie. Zostawiony podniesiony próg zatrzymałby silnik
    NA STAŁE — czyli spowodowałby dokładnie tę awarię, przed którą kill-switch
    ma chronić.

    Przywracanie musi więc iść przez `finally`: przerwanie, wyjątek i sukces
    prowadzą wtedy do tej samej ścieżki wyjścia.
    """
    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    assert "finally:" in zrodlo
    assert zrodlo.index("try:") < zrodlo.index("podnoszę"), (
        "podniesienie progu poza blokiem try — wyjątek w tym oknie zostawia "
        "silnik zatrzymany")


def test_proba_odmawia_przy_juz_aktywnym_killswitchu():
    """Gdyby ruszyła przy zastanym zdarzeniu, nie odróżniłaby swojego od cudzego
    i zwolniłaby cudze z notatką o próbie. Odmowa nie jest jednak wynikiem
    pomiaru — patrz testy niżej."""
    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    assert "jest już aktywny" in zrodlo


# ----------------------------------- trzy wyniki próby, nie dwa
#
# Do 2026-09-10 próba znała dwa zakończenia: ZALICZONA i NIEZALICZONA. Sytuacja
# „kill-switch już stoi, więc nie ma czego wywołać ze stanu spoczynku" wpadała
# do drugiego worka, czyli była zgłaszana jako AWARIA ZABEZPIECZENIA. To błąd
# rozpoznania: „nie dało się zmierzyć" to nie to samo co „zmierzono i wyszło
# źle". Kosztował dwa kolejne czerwone przebiegi CronJoba, aplikację ArgoCD
# w stanie Degraded i mail o awarii — dokładnie wtedy, gdy człowiek i tak
# wiedział o kill-switchu. A prawdziwa awaria łańcucha wyglądałaby identycznie
# i utonęłaby w tych powtórkach.


def _baza_proby(*, aktywnych=1, dni_blokady=0.0, dni_od_potwierdzenia=None,
                zdarzenie=None, powiadomien=0, wstrzymane=1, wejscia=0):
    """Atrapa bazy dla próby: odpowiada po fragmencie zapytania.

    Nie udaje Postgresa — oddaje dokładnie te wiersze, o które próba pyta,
    i wywraca się na zapytaniu, którego nie zna. Ciche `None` na nieznanym
    zapytaniu zamieniłoby każdy test poniżej w test ścieżki błędu.
    """
    async def fetch_one(sql, params=None):
        p = " ".join(sql.split())
        if "exec.equity_point" in p:
            return {"equity": 10_000}
        if "count(*)" in p and "released_at IS NULL" in p:
            return {"n": aktywnych, "dni_blokady": dni_blokady}
        if "released_auto = false" in p and "released_by IS NULL" in p:
            return {"dni": dni_od_potwierdzenia}
        # Zapytanie o powiadomienia ZAWIERA podzapytanie do killswitch_event,
        # wiec musi byc rozpoznane PRZED nim - inaczej atrapa oddaje wiersz
        # zdarzenia tam, gdzie proba czeka na licznik.
        if "notify.notification" in p:
            return {"n": powiadomien}
        # To samo dotyczy ogniwa 3: pyta o `journal.decision`, ale granice okna
        # bierze podzapytaniem z `risk.killswitch_event`. Domyslnie jeden slad
        # wstrzymania i zero wejsc, czyli ogniwo 3 potwierdzone - inaczej kazdy
        # test nizej mierzylby przy okazji sciezke braku pomiaru.
        if "journal.decision" in p:
            return {"wstrzymane": wstrzymane, "wejscia": wejscia}
        if "risk.killswitch_event" in p:
            return zdarzenie
        if "risk.limit_def" in p:
            return {"value": 8_000}
        raise AssertionError(f"atrapa nie zna zapytania: {p}")

    async def fetch_all(sql, params=None):
        return []

    wykonane = []

    async def execute(sql, params=None):
        wykonane.append((" ".join(sql.split()), params))

    return fetch_one, fetch_all, execute, wykonane


def test_proba_przy_aktywnym_killswitchu_jest_NIEROZSTRZYGNIETA_nie_niezaliczona(capsys):
    """Kill-switch już stoi: próby NIE DA SIĘ przeprowadzić, bo nie ma czego
    wywołać ze stanu spoczynku. To nie jest awaria zabezpieczenia i nie ma
    prawa świecić na czerwono — kod wyjścia zero.

    Ale nie może też przejść jako sukces: nikt w tym cyklu nie sprawdził, czy
    bezpiecznik działa. Komunikat musi to powiedzieć wprost.
    """
    from services.engine.proba_killswitcha import uruchom

    fetch_one, fetch_all, execute, wykonane = _baza_proby(
        aktywnych=1, dni_od_potwierdzenia=0.5)
    kod = asyncio.run(uruchom(fetch_all, fetch_one, execute))

    assert kod == 0, "niemożność pomiaru zgłoszona jako awaria zabezpieczenia"
    wyjscie = capsys.readouterr()
    tekst = wyjscie.out + wyjscie.err
    assert "NIEROZSTRZYGNIĘTA" in tekst
    assert "NIE ZOSTAŁO W TYM CYKLU SPRAWDZONE" in tekst, (
        "wynik zerowy bez tego zdania to zamiecenie sprawy pod dywan: "
        "przebieg wygląda jak potwierdzenie, że bezpiecznik działa")
    assert not wykonane, "próba ruszyła limit mimo że nie mogła się wykonać"


def test_zabezpieczenie_ktore_NIE_zadzialalo_daje_kod_niezerowy(monkeypatch, capsys):
    """Kanarek do poprzedniego. Gdyby rozdział trzech wyników poszedł za
    daleko, próba przestałaby krzyczeć także wtedy, gdy silnik naprawdę nie
    uruchomił kill-switcha — czyli zabezpieczenie byłoby zepsute po cichu.
    """
    from services.engine import proba_killswitcha as pk

    monkeypatch.setattr(pk, "CZEKAJ_SILNIK_S", 0)      # bez czekania na cykl
    fetch_one, fetch_all, execute, _ = _baza_proby(aktywnych=0, zdarzenie=None)
    kod = asyncio.run(pk.uruchom(fetch_all, fetch_one, execute))

    assert kod != 0, "silnik nie zatrzymał handlu, a próba to przemilczała"
    assert "NIEZALICZONA" in capsys.readouterr().err


def test_proba_ZWALNIA_wlasne_zdarzenie_gdy_padnie_w_polowie(monkeypatch):
    """Próba, która padła na ogniwie 4, nie ma prawa zostawić handlu wstrzymanego.

    Kill-switch wywołany przez próbę nie opisuje żadnego stanu rachunku: powstał
    dlatego, że `min_equity` stało przez chwilę powyżej kapitału. Próg wraca
    w `finally`, więc przyczyna znika — a zdarzenie z `released_at IS NULL`
    ZATRZYMUJE HANDEL (`evaluate_account`, punkt 1). Silnik stoi przez artefakt
    własnej próby, a nie przez decyzję ryzyka.

    Od rozdziału na trzy wyniki kosztuje to podwójnie: następne dobowe przebiegi
    widzą aktywny kill-switch i kończą się jako NIEROZSTRZYGNIĘTE z kodem ZERO
    przez `MAX_DNI_BEZ_POTWIERDZENIA` dób. CronJob świeci na zielono przy
    silniku, który nie handluje — a kanał, który miał o tym powiedzieć (dozór),
    jest dokładnie tym ogniwem, którego awaria to spowodowała.
    """
    from services.engine import proba_killswitcha as pk

    monkeypatch.setattr(pk, "CZEKAJ_DOZOR_S", 0)      # ogniwo 4 pada od razu
    fetch_one, fetch_all, execute, wykonane = _baza_proby(
        aktywnych=0,
        zdarzenie={"id": 77, "reason": "kapital ponizej progu",
                   "limit_name": "min_equity"},
        powiadomien=0)                                 # dozor milczy

    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 1, (
        "brak powiadomienia to NIEZALICZONA")

    zwolnienia = [(s, p) for s, p in wykonane
                  if "UPDATE risk.killswitch_event SET released_at" in s]
    assert zwolnienia, (
        "proba wywolala kill-switcha, padla na ogniwie 4 i NIE ZWOLNILA go — "
        "handel stoi przez zdarzenie, ktorego przyczyna juz nie istnieje")
    assert zwolnienia[-1][1][-1] == 77, "zwolnic wolno WYLACZNIE wlasne zdarzenie"


def test_wiele_dob_bez_potwierdzenia_SAMO_staje_sie_awaria(capsys):
    """Trzeci wynik nie może być workiem bez dna. Bezpiecznik niesprawdzony od
    tygodnia to osobne ryzyko — po przekroczeniu progu sama cisza jest awarią
    i musi dać kod niezerowy, mimo że próby nadal nie da się przeprowadzić."""
    from services.engine.proba_killswitcha import MAX_DNI_BEZ_POTWIERDZENIA, uruchom

    fetch_one, fetch_all, execute, _ = _baza_proby(
        aktywnych=1, dni_od_potwierdzenia=MAX_DNI_BEZ_POTWIERDZENIA + 1)
    kod = asyncio.run(uruchom(fetch_all, fetch_one, execute))

    assert kod != 0, (
        f"kill-switch aktywny od ponad {MAX_DNI_BEZ_POTWIERDZENIA} dób, "
        f"bezpiecznik przez ten czas ani razu niesprawdzony — i cisza")
    assert "NIESPRAWDZONE" in capsys.readouterr().err


def test_swieze_potwierdzenie_NIE_alarmuje(capsys):
    """Kanarek do progu. Alarm bijący od pierwszego dnia blokady zostanie
    wyłączony w tydzień i wtedy nie zadziała także w tym jednym przypadku,
    w którym miał."""
    from services.engine.proba_killswitcha import MAX_DNI_BEZ_POTWIERDZENIA, uruchom

    fetch_one, fetch_all, execute, _ = _baza_proby(
        aktywnych=1, dni_od_potwierdzenia=MAX_DNI_BEZ_POTWIERDZENIA - 1)
    assert asyncio.run(uruchom(fetch_all, fetch_one, execute)) == 0
    assert "NIESPRAWDZONE" not in capsys.readouterr().err


def test_brak_sladu_poprzedniej_proby_liczy_sie_od_POCZATKU_blokady():
    """System bez ani jednej zaliczonej próby w dzienniku (świeże wdrożenie,
    albo taki, w którym próba nigdy nie doszła do końca). Gdyby brak śladu
    znaczył „zero dób", licznik nigdy by nie ruszył i próg byłby martwy.
    Dolną granicą ciszy jest wtedy wiek najstarszego czynnego zdarzenia."""
    from services.engine.proba_killswitcha import MAX_DNI_BEZ_POTWIERDZENIA, uruchom

    fetch_one, fetch_all, execute, _ = _baza_proby(
        aktywnych=1, dni_od_potwierdzenia=None,
        dni_blokady=MAX_DNI_BEZ_POTWIERDZENIA + 2)
    assert asyncio.run(uruchom(fetch_all, fetch_one, execute)) != 0


def test_slad_poprzednich_prob_czytany_z_TROJKI_KOLUMN_a_nie_z_notatki():
    """Wiek ostatniego potwierdzenia da się policzyć bez nowej tabeli: każda
    zaliczona próba zostawia zwolnienie o `released_auto = false` i pustym
    `released_by`, a taka para nie powstaje inaczej — usługa wznowienia pisze
    `released_auto = true`, człowiek przez API zostawia `released_by`.

    Rozpoznawanie tego po treści notatki działałoby do pierwszej zmiany
    sformułowania — to ten sam błąd, przed którym ostrzega migracja 0029."""
    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    assert "released_auto = false AND released_by IS NULL" in \
        " ".join(kod.split()), "ślad próby wyszukiwany inaczej niż po kolumnach"
    assert "release_note LIKE" not in kod and "release_note ILIKE" not in kod


def test_proba_traktuje_brak_powiadomienia_jako_NIEZALICZONA():
    """Cichy kill-switch jest gorszy niż jego brak, bo daje złudzenie kontroli.
    Próba kończąca się sukcesem mimo braku powiadomienia sprawdzałaby połowę
    mechanizmu i raportowała całość."""
    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    assert "nikt się o tym nie dowiedział" in zrodlo
    assert zrodlo.count("raise Niezaliczona") >= 4, (
        "każde ogniwo ma mieć własne niepowodzenie, nie jedno zbiorcze")


def test_proba_uzywa_limitu_o_regule_RECZNEJ():
    """Próba mierzy czas od zadziałania do powiadomienia. Gdyby użyła limitu
    wznawianego automatycznie, dozorca zwolniłby zdarzenie w połowie pomiaru
    i próba mierzyłaby własny wyścig."""
    from services.engine.proba_killswitcha import LIMIT_PROBY

    assert LIMIT_PROBY == "min_equity"
    migracja = Path("db/migrations/0029_wznowienie_killswitcha.sql").read_text(encoding="utf-8")
    assert "wznowienie = 'reczne'" in migracja and "min_equity" in migracja


def test_proba_na_koncie_innym_niz_demo_wymaga_zgody():
    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    assert "PROBA_NA_LIVE" in zrodlo


# ============================================================ dozorca i wznowienie

def test_kazdy_limit_krytyczny_niesie_SWOJA_NAZWE():
    """Reguła wznowienia musi wiedzieć, KTÓRY limit zadziałał. Powód jest
    zdaniem po polsku i zmienia się razem z liczbami — wyciąganie z niego nazwy
    działałoby do pierwszej zmiany sformułowania."""
    assert evaluate_account(konto(97_000, 100_000, day_ago=100_000),
                            DEFAULT_LIMITS).trigger_limit == "max_daily_loss"
    assert evaluate_account(konto(94_000, 96_000),
                            DEFAULT_LIMITS).trigger_limit == "min_equity"
    bez_progu = {k: v for k, v in DEFAULT_LIMITS.items() if k != "min_equity"}
    assert evaluate_account(konto(85_000, 100_000),
                            bez_progu).trigger_limit == "max_drawdown"


@pytest.mark.parametrize("limit,limit_pct,stan,oczekiwane", [
    # Histereza: wznowienie przy POŁOWIE limitu, nie na jego granicy.
    ("max_drawdown", 10.0, {"wynik": 96_000, "szczyt": 100_000, "doba": 96_000}, True),
    ("max_drawdown", 10.0, {"wynik": 94_000, "szczyt": 100_000, "doba": 94_000}, False),
    ("max_daily_loss", 2.0, {"wynik": 99_500, "szczyt": 100_000, "doba": 100_000}, True),
    ("max_daily_loss", 2.0, {"wynik": 98_800, "szczyt": 100_000, "doba": 100_000}, False),
])
def test_histereza_wznawia_ciasniej_niz_zatrzymuje(limit, limit_pct, stan, oczekiwane):
    """Bez histerezy system oscylowałby wokół granicy: wznowienie przy tym samym
    progu, przy którym zatrzymuje, znaczy zatrzymanie i wznowienie przy każdym
    drgnięciu kapitału."""
    from services.engine.wznowienie import warunek_ustal

    ustal, dowod = warunek_ustal(limit, limit_pct, stan)
    assert ustal is oczekiwane, f"{limit}: {dowod}"


def test_nieznany_limit_NIE_jest_wznawiany():
    """Domysł zachowawczy. Nowy limit krytyczny, o którym reguła nie wie, ma
    zatrzymać system do czasu, aż ktoś świadomie dopisze jego regułę — a nie
    wrócić do handlu dlatego, że nie było warunku, który by go zatrzymał."""
    from services.engine.wznowienie import warunek_ustal

    ustal, dowod = warunek_ustal("cos_nowego", 10.0,
                                 {"wynik": 100_000, "szczyt": 100_000, "doba": 100_000})
    assert ustal is False and "brak reguły" in dowod


def test_prog_kapitalu_ma_regule_RECZNA_i_to_jest_decyzja():
    """Przekroczenie progu kapitału znaczy, że system złamał jedyną obietnicę,
    która była warunkiem wpłaty (docs/05). Wznowienie tego automatem czyniłoby
    próg ozdobą."""
    migracja = Path("db/migrations/0029_wznowienie_killswitcha.sql").read_text(encoding="utf-8")
    assert "po_ustaniu" in migracja and "max_daily_loss" in migracja
    assert "po_odbudowie" in migracja and "max_drawdown" in migracja
    # Do 2026-10-03 stała tu lista `linie` liczona i nieużywana (ruff F841),
    # a asercja pytała o „reczne" GDZIEKOLWIEK w pliku - zaliczała ją sama
    # wartość domyślna kolumny. Pytamy o regułę TEGO limitu.
    assert re.search(r"SET wznowienie = 'reczne'\s+WHERE name = 'min_equity'",
                     migracja), "min_equity nie ma reguły ręcznej"


def test_licznik_wznowien_idzie_PRZED_warunkiem():
    """Gdyby licznik szedł po warunku, system przy trwającym problemie wznawiałby
    i zatrzymywał się w kółko, a licznik tylko opisywałby tę pętlę zamiast ją
    przerwać."""
    zrodlo = Path("services/engine/wznowienie.py").read_text(encoding="utf-8")
    assert zrodlo.index("max_wznowien") < zrodlo.index("ustal, dowod = warunek_ustal")


def test_dozorca_nie_ginie_od_pojedynczego_bledu():
    """Dozorca jest ostatnią linią. Wyjątek w jednym cyklu nie może zabić pętli —
    ale nie może też zostać przemilczany: gdy błąd jest trwały, heartbeat
    przestaje się odświeżać i czujka żywotności zgłasza martwego dozorcę."""
    zrodlo = Path("services/engine/dozorca.py").read_text(encoding="utf-8")
    assert "except Exception" in zrodlo and "conn.rollback()" in zrodlo
    assert "bij_heartbeat" in zrodlo


def test_czujka_zywotnosci_pilnuje_TAKZE_dozorcy():
    """Nadzorca, którego nikt nie nadzoruje, jest tylko przesunięciem problemu
    o jeden poziom: martwy dozorca wygląda dokładnie jak dozorca, który nie ma
    co zgłaszać."""
    zrodlo = Path("services/engine/dozor.py").read_text(encoding="utf-8")
    assert "risk.dozorca_heartbeat" in zrodlo, (
        "czujka żywotności nie sprawdza pulsu dozorcy")


def test_dozorca_uzywa_TEGO_SAMEGO_risk_kernela_co_silnik():
    """Druga implementacja reguł 'na potrzeby dozorcy' byłaby gwarancją rozjazdu
    przy pierwszej zmianie limitu — ten sam błąd, przed którym broni wspólny
    risk_rules między produkcją a symulacją."""
    zrodlo = Path("services/engine/dozorca.py").read_text(encoding="utf-8")
    assert "from services.engine.risk import RiskKernel" in zrodlo
    assert "check_account()" in zrodlo


# --------------------------------------------------- autonomia widoczna w UI

def test_interfejs_pokazuje_KTO_zwolnil():
    """System, który sam siebie zatrzymuje i sam wznawia, a w interfejsie
    wygląda tak samo jak zwolniony ręcznie, nie daje się rozliczyć. Po serii
    zatrzymań pierwsze pytanie brzmi „czy ktoś to przemyślał, czy poszło
    automatem"."""
    api = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "released_auto" in api, "API nie oddaje, kto zwolnił"
    ui = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    assert "released_auto" in ui and "automatycznie" in ui


def test_interfejs_pokazuje_REGULE_WZNOWIENIA_przy_limicie():
    """Interfejs mówił, że system się zatrzyma, i milczał o tym, czy sam wróci.
    To pierwsze pytanie, jakie zadaje się o autonomiczny bezpiecznik."""
    ui = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    for regula in ("reczne", "po_ustaniu", "po_odbudowie"):
        assert regula in ui, f"brak opisu reguły {regula}"
    api = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "ld.wznowienie" in api


def _bloki_warunkowe(zrodlo: str) -> list[tuple[str, str]]:
    """Pary (warunek, treść) dla `{warunek && ( ... )}` — tak ten front renderuje
    warunkowo. Nawiasy domykane licznikiem, nie wyrażeniem regularnym: treść
    bloku sama zawiera nawiasy i wyrażenie regularne ucięłoby ją w losowym
    miejscu, a strażnik zaczął by przechodzić z powodu błędu w sobie."""
    bloki = []
    for m in re.finditer(r"\{([^{}]{1,240}?)&&\s*\(", zrodlo):
        i = zrodlo.index("(", m.end() - 1)
        start, glebokosc = i, 0
        while i < len(zrodlo):
            if zrodlo[i] == "(":
                glebokosc += 1
            elif zrodlo[i] == ")":
                glebokosc -= 1
                if glebokosc == 0:
                    break
            i += 1
        bloki.append((m.group(1), zrodlo[start:i]))
    return bloki


def test_interfejs_pokazuje_PULS_DOZORCY():
    """Martwy dozorca wygląda dokładnie jak dozorca, który nie ma co zgłaszać —
    różnicę widać wyłącznie w wieku pulsu. Bez tego widoku autonomia jest
    niewidoczna: system decyduje sam, a interfejs pokazuje tylko skutki.

    DLACZEGO TEN STRAŻNIK NIE PILNUJE NAPISU

    Pilnował: sprawdzał literał „DOZORCA NIE BIJE". Napis przeszedł przez
    przeredagowanie ekranu i strażnik zaczął padać na kodzie LEPSZYM od tego,
    którego bronił — bo bronił sformułowania, a nie rzeczy. Gorszy wariant tej
    samej pomyłki jest cichy: literał zostaje w pliku w martwej gałęzi albo
    w komentarzu, strażnik świeci na zielono, a na ekranie nie ma nic.

    Znaczenie niosą trzy rzeczy i one są tu sprawdzane:
      * trzy ROZŁĄCZNE stany (żyje / nie bije / nie wiadomo) — awaria odczytu
        pulsu nie może wyglądać jak brak dozorcy ani jak dozorca zdrowy;
      * WIEK pulsu, i to najstarszy ze wszystkich dozorców — zdanie mówi
        o wszystkich, więc musi je opisywać ten, który bije najrzadziej;
      * ostrzeżenie w bloku WIDOCZNYM (`notice`/`form-error`), nie w szarym
        `hint` obok tytułu karty.
    """
    api = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "dozorca_heartbeat" in api and '"/dozorca"' in api
    # Bez tych dwóch pól w odpowiedzi front nie ma z czego rozróżnić stanów.
    for pole in ('"zyje"', '"sekund_temu"'):
        assert pole in api, f"API nie oddaje {pole} — puls jest nierozróżnialny"

    ui = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    zapytanie = re.search(
        r"const\s+(\w+)\s*=\s*useApi\(\s*[`'\"][^`'\"]*risk/dozorca", ui)
    assert zapytanie, "front w ogóle nie pyta o puls dozorcy"
    doz = zapytanie.group(1)

    # --- stan 1 i 2: żyje / nie bije ---------------------------------------
    assert "zyje" in ui, "front nie odróżnia dozorcy żywego od niebijącego"
    # --- stan 3: nie wiadomo ------------------------------------------------
    # Nieudany odczyt pulsu MUSI mieć własną gałąź. Bez sięgnięcia po kanał
    # błędu zapytania „nie wiem" zlewa się z „brak dozorcy" — czyli awaria
    # nadzoru wygląda jak świadoma decyzja, żeby nadzoru nie mieć.
    assert re.search(rf"\b{doz}\.error\b", ui), (
        "awaria odczytu pulsu nie ma własnego stanu — zlewa się z brakiem dozorcy")
    # --- trzeci stan musi też być rozłączny z pustą listą -------------------
    assert re.search(rf"\b{doz}\.data\b", ui), "front nie czyta listy dozorców"

    # --- wiek pulsu, i to NAJSTARSZY ---------------------------------------
    assert "sekund_temu" in ui, "interfejs nie pokazuje wieku pulsu"
    # `?.[0]` liczy sie tak samo jak `[0]` - to ten sam pierwszy wiersz.
    assert not re.search(rf"\b{doz}\.data\s*\??\.?\s*\[\s*0\s*\]", ui), (
        "wiek pulsu brany z PIERWSZEGO dozorcy po ORDER BY nazwa — zdanie mówi "
        "o wszystkich dozorcach, więc musi je opisywać najstarszy puls")
    assert re.search(r"(Math\.max|\.reduce\(|\.sort\()[^\n]*sekund_temu", ui) \
        or re.search(r"sekund_temu[^\n]*(Math\.max|\.reduce\(|\.sort\()", ui), (
            "wiek pulsu nie jest agregatem po wszystkich dozorcach")

    # --- ostrzeżenie w miejscu, które widać --------------------------------
    # Nazwy zmiennych nieistotne; liczy się, że stan „nie bije" jest wyprowadzony
    # z pola `zyje` i że TEN stan bramkuje blok o widocznej klasie.
    z_zyje = {m.group(1) for m in re.finditer(r"const\s+(\w+)\s*=[^;]*\bzyje\b", ui)}
    assert z_zyje, "stan „nie bije” nie jest nigdzie wyprowadzony z pola `zyje`"
    widoczne = [
        tresc for warunek, tresc in _bloki_warunkowe(ui)
        if any(nazwa in warunek for nazwa in z_zyje)
        and re.search(r'className="(notice|form-error)', tresc)
    ]
    assert widoczne, (
        "ostrzeżenie o niebijącym dozorcy nie trafia do widocznego bloku "
        "(notice/form-error) — szary `hint` obok tytułu karty to nie alarm")


def test_prog_zywotnosci_dozorcy_jest_wielokrotnoscia_interwalu():
    """Próg równy interwałowi zgłaszałby awarię przy każdym wdrożeniu.
    Próg zbyt luźny przestaje cokolwiek wykrywać."""
    from services.engine.dozorca import INTERWAL_S

    api = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "< 180" in api, "próg żywotności dozorcy nie jest w API"
    assert 180 >= 3 * INTERWAL_S, (
        f"próg 180 s przy interwale {INTERWAL_S} s to mniej niż trzy cykle")


def test_proba_pozarowa_NIE_udaje_wznowienia_automatycznego():
    """`released_auto` istnieje po to, żeby odpowiedzieć na jedno pytanie:
    czy system wrócił do handlu SAM.

    Ustawianie jej przez próbę zlewa dwa różne zdarzenia w jedno — a wtedy
    odpowiedź wymaga czytania treści notatki, czyli flaga przestaje cokolwiek
    rozstrzygać. Przy próbie dobowej takich wierszy przybywa jeden dziennie
    i po miesiącu zdominowałyby statystykę wznowień automatycznych.

    ZWALNIAJĄCYM NIE JEST JEDNAK CZŁOWIEK, więc `released_by` zostaje puste.
    Ta kolumna to bigint z kluczem obcym do app."user" — mieści się w niej
    identyfikator konta, nie nazwa procesu. Źródło zwolnienia rozstrzyga trójka
    kolumn: `released_auto` prawdziwe to usługa wznowienia, fałszywe z podanym
    `released_by` to człowiek, fałszywe bez niego to właśnie próba.
    """
    from pathlib import Path

    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    ogon = kod.split("UPDATE risk.killswitch_event")[1]
    fragment = ogon.split("WHERE id")[0]
    assert "released_auto = false" in fragment
    assert "released_by" not in fragment, (
        "próba zapisuje `released_by` — a to bigint wskazujący na konto "
        "człowieka. Napis nie wejdzie tam wcale, a ID właściciela byłoby "
        "fałszem: interfejs pokazuje ten login jako tego, kto pozwolił "
        "systemowi znowu ryzykować pieniądze")
    # Notatka jest już w argumentach wywołania, za treścią zapytania.
    assert "PRÓBA POŻAROWA" in ogon[:800], (
        "notatka musi nazywać źródło zwolnienia — bez `released_by` to ona "
        "jest jedynym miejscem, w którym stoi, co to było")


def test_proba_ZAPOWIADA_okno_w_tym_samym_zapisie_co_podniesienie_progu():
    """Czujka żywotności rozpoznaje zdarzenie próby po `risk.limit_def.proba_do`
    (migracja 0045). Znacznik musi wejść w JEDNYM UPDATE z wartością — osobny
    zapis zostawiałby chwilę, w której próg jest już podniesiony, silnik może
    już wywołać zdarzenie, a czujka nie ma po czym poznać, że to próba.
    W `finally` znacznik schodzi razem z progiem."""
    from services.engine import proba_killswitcha as pk

    fetch_one, fetch_all, execute, wykonane = _baza_proby(
        aktywnych=0,
        zdarzenie={"id": 5, "reason": "kapital ponizej progu",
                   "limit_name": "min_equity"},
        powiadomien=1)
    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 0

    podniesienia = [(s, p) for s, p in wykonane
                    if "UPDATE risk.limit_def SET value" in s and "proba_do = now()" in s]
    assert len(podniesienia) == 1, "podniesienie progu bez znacznika okna próby"
    sql, params = podniesienia[0]
    assert params[1] == pk.OKNO_PROBY_S
    assert pk.OKNO_PROBY_S > pk.CZEKAJ_SILNIK_S + pk.CZEKAJ_DOZOR_S, (
        "okno krótsze niż najdłuższa możliwa próba - czujka pod koniec próby "
        "widziałaby zwykły kill-switch")

    przywrocenia = [s for s, _ in wykonane
                    if "UPDATE risk.limit_def SET value" in s and "proba_do = NULL" in s]
    assert przywrocenia, "przywrócenie progu nie zdejmuje znacznika okna"
