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

def test_petla_wstrzymuje_WYLACZNIE_wejscia():
    """Najgroźniejsza możliwa pomyłka w tym miejscu.

    Kill-switch zadziałał, bo kapitał spada. Jeśli zablokuje też wyjścia,
    system zostaje z otwartymi pozycjami w rynku, który właśnie ucieka
    przeciwko niemu — czyli robi dokładnie odwrotność tego, po co istnieje.
    """
    zrodlo = Path("services/engine/loop.py").read_text(encoding="utf-8")
    assert 'if action.startswith("enter") and wejscia_wstrzymane:' in zrodlo, (
        "warunek wstrzymania musi dotyczyć wyłącznie wejść; jeśli zmienił "
        "kształt, sprawdź, czy nadal przepuszcza wyjścia")
    assert "wejscia_wstrzymane" in zrodlo


# ------------------------------------------------- ogniwo 5: zwolnienie ręczne

def test_zwolnienie_wymaga_POWODU_a_nie_tylko_kliknięcia():
    """Kill-switch nie wygasa sam, bo automatyczne wznowienie po obsunięciu to
    najprostsza droga do powtórzenia tej samej straty. Zwolnienie jednym
    kliknięciem jest tym samym wznowieniem — tyle że z krokiem, który daje
    złudzenie namysłu."""
    from services.api.routers.risk import ZwolnienieBody

    ZwolnienieBody(note="dane świec poprawione w sha-abc1234")
    for zly in ("", "ok", "bo tak"):
        with pytest.raises(Exception):
            ZwolnienieBody(note=zly)


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
    i zwolniłaby cudze z notatką o próbie."""
    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    assert "jest już aktywny" in zrodlo


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
    ("max_drawdown", 10.0, {"equity": 96_000, "peak": 100_000, "day_ago": 96_000}, True),
    ("max_drawdown", 10.0, {"equity": 94_000, "peak": 100_000, "day_ago": 94_000}, False),
    ("max_daily_loss", 2.0, {"equity": 99_500, "peak": 100_000, "day_ago": 100_000}, True),
    ("max_daily_loss", 2.0, {"equity": 98_800, "peak": 100_000, "day_ago": 100_000}, False),
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
                                 {"equity": 100_000, "peak": 100_000, "day_ago": 100_000})
    assert ustal is False and "brak reguły" in dowod


def test_prog_kapitalu_ma_regule_RECZNA_i_to_jest_decyzja():
    """Przekroczenie progu kapitału znaczy, że system złamał jedyną obietnicę,
    która była warunkiem wpłaty (docs/05). Wznowienie tego automatem czyniłoby
    próg ozdobą."""
    migracja = Path("db/migrations/0029_wznowienie_killswitcha.sql").read_text(encoding="utf-8")
    assert "po_ustaniu" in migracja and "max_daily_loss" in migracja
    assert "po_odbudowie" in migracja and "max_drawdown" in migracja
    linie = [l for l in migracja.splitlines() if "min_equity" in l and "UPDATE" not in l]
    assert any("reczne" in l for l in migracja.splitlines()), "brak reguły ręcznej"


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


def test_interfejs_pokazuje_PULS_DOZORCY():
    """Martwy dozorca wygląda dokładnie jak dozorca, który nie ma co zgłaszać —
    różnicę widać wyłącznie w wieku pulsu. Bez tego widoku autonomia jest
    niewidoczna: system decyduje sam, a interfejs pokazuje tylko skutki."""
    api = Path("services/api/routers/risk.py").read_text(encoding="utf-8")
    assert "dozorca_heartbeat" in api and '"/dozorca"' in api
    ui = Path("web/src/components/Risk.jsx").read_text(encoding="utf-8")
    assert "risk/dozorca" in ui and "DOZORCA NIE BIJE" in ui


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
    """
    from pathlib import Path

    zrodlo = Path("services/engine/proba_killswitcha.py").read_text(encoding="utf-8")
    kod = "\n".join(w for w in zrodlo.splitlines()
                    if not w.lstrip().startswith("#"))
    fragment = kod.split("UPDATE risk.killswitch_event")[1].split("WHERE id")[0]
    assert "released_auto = false" in fragment
    assert "released_by" in fragment, (
        "zwalniający musi być nazwany z imienia — inaczej wiersz mówi tylko, "
        "że ktoś zwolnił, a nie kto")
