"""Dwie czujki, które zostały poza wspólnym tłumieniem — i co to kosztowało.

Znalezisko nr 4 audytu 2026-09-23. `dozor.py` naprawił tę wadę w pięciu
czujkach (`sprawdz_zywotnosc`, `sprawdz_ryzyko`, `sprawdz_cisze_pomiaru`,
`sprawdz_instrumenty`, `sprawdz_puls_workerow`), wszystkie kończą się przez
`kod_wyjscia(zgloszone, trwale, ...)`. Dwie zostały pominięte i kończyły się
`return len(powody)` bez ani jednego zapytania do `notify.notification`:

    sprawdz_swiezosc   -> engine.down
    sprawdz_egzekucje  -> engine.down (zawieszone plany) i order.rejected

KATALOGOWY COOLDOWN TEGO NIE ŁAPIE. `engine.down` jest `always_on`
(migracja 0011:112), a `services/api/notifications.py` sprawdza cooldown
wyłącznie dla zdarzeń BEZ tej flagi — `if cooldown and not et["always_on"]`.
Flaga pomyślana jako „tego nie wolno wyciszyć użytkownikowi" działała więc
jak „tego nie wolno wyciszyć NIKOMU, nigdy".

ARYTMETYKA AWARII. Zadanie chodzi co 10 minut (`values.yaml`,
`zywotnoscCron.schedule`), czyli 144 razy na dobę. Worker `market`, który
stanął w piątek wieczorem, dawał do poniedziałku ~430 maili o wadze
`critical`, ~430 padniętych Jobów, pody w stanie Error i aplikację ArgoCD
trwale Degraded — dokładnie tę awarię, którą docstring `dozor.py` opisuje
jako zmierzoną na produkcji 2026-09-10.

CZEGO NIE ŁAPAŁY ISTNIEJĄCE TESTY. `tests/test_dozor.py` sprawdza wyłącznie
ścieżkę „brak zastrzeżeń → 0". Czyste `ocen_swiezosc` i `ocen_egzekucje` są
pokryte dobrze, ale one z definicji nie widzą tłumienia — ono żyje w otoczce.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from services.engine import dozor

TERAZ = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _stend_swiezosci(*, wiek_min, ostatni, powiadomiono_po_ostatnim):
    """Atrapa `fetch_all` dla `sprawdz_swiezosc` + licznik wysłanych alarmów.

    `powiadomiono_po_ostatnim` decyduje, czy zapytanie o tłumienie znajdzie
    wiersz — czyli czy ta awaria była już zgłoszona PO ostatnim wpisie danych.
    Atrapa oddaje wyłącznie kolumny, o które pyta prawdziwy `SELECT`.
    """
    wyslane = []

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        if "notify.notification" in p:
            return [{"?column?": 1}] if powiadomiono_po_ostatnim else []
        if 'app."user"' in p:
            return [{"id": 1}]
        if "market.candle" in p:
            return [
                {"co": "market.candle (1h, źródło świecy 4h)",
                 "minut_swiecy": 60, "wiek_min": wiek_min, "ostatni": ostatni},
                {"co": "feat.feature_value", "minut_swiecy": 240,
                 "wiek_min": 5, "ostatni": TERAZ},
            ]
        raise AssertionError(f"atrapa nie zna zapytania: {p}")

    async def notify(user_id, kod, tytul, tresc, **kw):
        wyslane.append((kod, tytul, kw.get("data")))
        return 1

    return fetch_all, notify, wyslane


# ------------------------------------------------------------------ świeżość

def test_swiezosc_NIE_powtarza_alarmu_o_tej_samej_stojacej_dacie():
    """Sedno wady: potok stanął raz, a alarm szedł 144 razy na dobę.

    Dane stoją od trzech dni, powiadomienie o tym poszło zaraz po ostatnim
    wpisie. Ten przebieg nie ma NIC NOWEGO do powiedzenia: ani mail, ani
    czerwony Job nie niosą tu informacji, której nie ma w tamtym mailu.
    Stan zostaje wypisany w logu — i to jest cała różnica między ciszą, która
    coś znaczy, a ciszą, która jest zamieceniem sprawy pod dywan.
    """
    fetch_all, notify, wyslane = _stend_swiezosci(
        wiek_min=4320, ostatni=TERAZ - timedelta(days=3),
        powiadomiono_po_ostatnim=True)

    kod = asyncio.run(dozor.sprawdz_swiezosc(fetch_all, notify, bar="4h"))

    assert not wyslane, (
        "czujka wysłała kolejny mail `critical` o awarii, która jest już "
        "zgłoszona — 144 takie na dobę uczą kasować maile od monitoringu")
    assert kod == 0, (
        "kod wyjścia niezerowy przy stanie JUŻ ZGŁOSZONYM to 144 czerwone "
        "Joby na dobę i ArgoCD trwale Degraded — ta sama wada w drugim kanale")


def test_swiezosc_ZGLASZA_awarie_pierwszy_raz():
    """Kanarek do poprzedniego. Tłumienie, które tłumi także pierwsze
    zgłoszenie, jest gorsze niż jego brak: awaria przestaje istnieć."""
    fetch_all, notify, wyslane = _stend_swiezosci(
        wiek_min=4320, ostatni=TERAZ - timedelta(days=3),
        powiadomiono_po_ostatnim=False)

    kod = asyncio.run(dozor.sprawdz_swiezosc(fetch_all, notify, bar="4h"))

    assert wyslane, "pierwsze zgłoszenie awarii nie poszło"
    assert wyslane[0][0] == "engine.down"
    assert kod != 0, "nowe zgłoszenie musi pomalować zadanie na czerwono"


def test_swiezosc_tlumi_PER_ZRODLO_a_nie_zbiorczo():
    """Stojące świece i stojące cechy to dwie awarie w dwóch różnych podach.

    Klucz idzie do `data->>'zrodlo'`, a nie do tytułu: tytuł jest zdaniem po
    polsku i zmienia się razem z liczbami. Bez klucza jedno powiadomienie
    tłumiłoby drugie — czyli druga awaria milczałaby dlatego, że pierwsza
    zdarzyła się wcześniej.
    """
    fetch_all, notify, wyslane = _stend_swiezosci(
        wiek_min=4320, ostatni=TERAZ - timedelta(days=3),
        powiadomiono_po_ostatnim=False)
    asyncio.run(dozor.sprawdz_swiezosc(fetch_all, notify, bar="4h"))

    assert wyslane, "brak zgłoszenia"
    dane = wyslane[0][2]
    assert dane and dane.get("zrodlo") == "market.candle (1h, źródło świecy 4h)", (
        "powiadomienie bez klucza źródła w `data` — tłumienie nie ma czym "
        "odróżnić dwóch awarii i wygasi tę drugą")


# ----------------------------------------------------------------- egzekucja

def _stend_egzekucji(*, zawieszonych, ostatni_plan, powiadomiono):
    wyslane = []

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        if "notify.notification" in p:
            return [{"?column?": 1}] if powiadomiono else []
        if 'app."user"' in p:
            return [{"id": 1}]
        if "journal.planned_order" in p:
            return [{"n": zawieszonych, "ostatni": ostatni_plan}]
        if "max(submitted_at)" in p:
            return [{"ostatnie_zlecenie": None}]
        # REGULA O SKUTKU, NIE O STATUSIE: pozycja otwarta, przy ktorej zadna
        # sprzedaz sie nie wypelnila. Sprawdzana PRZED galeziami `exec.order`,
        # bo to zapytanie dotyka obu tabel — dopasowanie po szerszym markerze
        # oddaloby mu liczniki z innego pytania.
        if "exec.position" in p:
            return []
        if "exec.order" in p and "reject_reason" in p:
            return []
        if "exec.order" in p and "client_order_id" in p:
            return []
        if "exec.order" in p:
            return [{"przyjete": 5, "odrzucone": 0, "nieznane": 0,
                     "bez_kontaktu": 0}]
        raise AssertionError(f"atrapa nie zna zapytania: {p}")

    async def notify(user_id, kod, tytul, tresc, **kw):
        wyslane.append((kod, tytul))
        return 1

    return fetch_all, notify, wyslane


def test_egzekucja_NIE_powtarza_alarmu_o_tych_samych_zawieszonych_planach():
    """Trzy plany utknęły w piątek. Bez tłumienia to ~430 maili `critical`
    i ~430 padniętych Jobów do poniedziałku — o trzech planach, o których
    wiadomo od pierwszego maila."""
    fetch_all, notify, wyslane = _stend_egzekucji(
        zawieszonych=3, ostatni_plan=TERAZ - timedelta(days=3),
        powiadomiono=True)

    kod = asyncio.run(dozor.sprawdz_egzekucje(fetch_all, notify))

    assert not wyslane, "kolejny mail o awarii, która jest już zgłoszona"
    assert kod == 0, "czerwony Job przy stanie JUŻ ZGŁOSZONYM"


def test_egzekucja_ZGLASZA_nowe_zawieszone_plany():
    """Kanarek. Nowy plan, który utknął, przesuwa `max(valid_until)` do przodu,
    więc stare powiadomienie przestaje go tłumić — nawrót rozpoznaje się bez
    drugiego mechanizmu, tak samo jak przy `last_beat_at` w żywotności."""
    fetch_all, notify, wyslane = _stend_egzekucji(
        zawieszonych=3, ostatni_plan=TERAZ - timedelta(hours=5),
        powiadomiono=False)

    kod = asyncio.run(dozor.sprawdz_egzekucje(fetch_all, notify))

    assert wyslane and wyslane[0][0] == "engine.down"
    assert kod != 0


def test_obie_czujki_koncza_sie_przez_wspolne_kod_wyjscia():
    """Reguła `dozor.py` brzmi: kod wyjścia niezerowy WTEDY I TYLKO WTEDY, gdy
    czujka zgłosiła coś NOWEGO. `return len(powody)` łamie ją po cichu — a
    pilnuje jej jedna funkcja właśnie po to, żeby nie dało się jej obejść
    w pojedynczej czujce.

    Test patrzy w źródło, bo to jedyne miejsce, w którym ta wada jest widoczna
    jako wada, a nie jako liczba.
    """
    import inspect

    for funkcja in (dozor.sprawdz_swiezosc, dozor.sprawdz_egzekucje):
        zrodlo = inspect.getsource(funkcja)
        kod = "\n".join(w for w in zrodlo.splitlines()
                        if not w.lstrip().startswith("#"))
        assert "kod_wyjscia(" in kod, (
            f"{funkcja.__name__} liczy kod wyjścia u siebie — zero z takiego "
            f"miejsca bywa nieme, a niezero powtarza się 144 razy na dobę")
        assert "return len(powody)" not in kod, (
            f"{funkcja.__name__}: `return len(powody)` nie zna tłumienia")
        assert "notify.notification" in kod, (
            f"{funkcja.__name__} nie pyta ani razu o to, czy już zgłaszała — "
            f"a bez tego pytania tłumienia po prostu nie ma")
