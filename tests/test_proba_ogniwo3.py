"""Próba pożarowa ma MIERZYĆ każde ogniwo, o którym melduje.

TŁO, CZYLI DLACZEGO TO JEST OSOBNY PLIK

Do 2026-09-22 `proba_killswitcha.py` sprawdzała ogniwa 1-2 i 4, a na końcu
drukowała BEZWARUNKOWO „PRÓBA ZALICZONA: wszystkie ogniwa zadziałały".
Nie sprawdzane było ogniwo 3 — „kill-switch aktywny -> silnik wstrzymał
wejścia" — czyli JEDYNE z czterech, które dotyczy pieniędzy; pozostałe trzy
opisują zapis i powiadomienie.

Drugą wadą było ogniwo 4: liczyło `notify.notification` po samym
`event_code='killswitch.triggered'`, bez filtra po silniku i bez powiązania
z własnym zdarzeniem. Ten sam kod wysyłają `uzgodnienie.py` i `wznowienie.py`,
a dozór powtarza zgłoszenie zastanego zatrzymania co dziesięć minut — przy
oknie `CZEKAJ_DOZOR_S` (jedenaście minut) zaliczenie CUDZEGO powiadomienia
jako własnego nie było więc teoretyczne, tylko prawdopodobne.

Obie wady dają ten sam skutek i dlatego stoją w jednym pliku: JEDYNA dobowa
weryfikacja bezpiecznika melduje sukces, którego nie zmierzyła.

CZEGO TE TESTY NIE UDAJĄ

Atrapa niżej nie jest Postgresem. Dla ogniwa 4 interpretuje jednak FILTRY
zapytania i wiąże je z podanymi parametrami — inaczej test przechodziłby
z błędnego powodu: atrapa oddająca licznik bez oglądania parametrów zielenieje
tak samo dla zapytania z filtrem, jak i bez niego, czyli mierzyłaby samą siebie.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]

# Powiadomienie, jakie dozor zapisuje dla NASZEGO zdarzenia proby.
MOJE_ZDARZENIE = 77


def _atrapa(*, zdarzenie=None, powiadomienia=(), wstrzymane=0, wejscia=0):
    """Atrapa bazy dla próby. Oddaje wiersze, o które próba pyta.

    `powiadomienia` to lista krotek `(engine, event_id)` — atrapa liczy z nich
    tyle, ile PRZEPUSZCZA zapytanie wraz ze swoimi parametrami. Dzięki temu
    zapytanie bez filtra po silniku i po zdarzeniu naprawdę zalicza cudzy wpis,
    tak samo jak zaliczyłby go Postgres.
    """
    async def fetch_one(sql, params=None):
        p = " ".join(sql.split())
        params = tuple(params or ())
        if "exec.equity_point" in p:
            return {"equity": 10_000}
        if "count(*)" in p and "released_at IS NULL" in p:
            return {"n": 0, "dni_blokady": 0.0}
        if "released_auto = false" in p and "released_by IS NULL" in p:
            return {"dni": 1.0}
        if "notify.notification" in p:
            return {"n": _ile_powiadomien(p, params, powiadomienia)}
        if "journal.decision" in p:
            # Zwiazanie z parametrami, nie samo oddanie liczb: zapytanie bez
            # filtra po silniku albo o cudze zdarzenie ma tu paść, a nie cicho
            # dostac nasze wiersze.
            assert params[0] == "wstrzymanie_ryzykiem", (
                "ogniwo 3 pyta o inny powód holdu niż ten z loop.py")
            assert params[1:3] == ("crypto", "demo"), (
                "ogniwo 3 nie filtruje po parze engine/mode")
            assert params[3] == MOJE_ZDARZENIE, (
                "okno ogniwa 3 nie jest zakotwiczone na WŁASNYM zdarzeniu")
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


def _ile_powiadomien(p: str, params: tuple, dane) -> int:
    """Licznik powiadomień PRZEPUSZCZONYCH przez zapytanie i jego parametry.

    Filtry czytamy z treści zapytania, a wartości bierzemy z parametrów
    w kolejności, w jakiej stoją placeholdery — czyli dokładnie tak, jak
    zwiąże je psycopg. Zapytanie, które filtra nie ma, dostaje tu wszystko,
    co ma kod zdarzenia — i właśnie na tym polegała wada.
    """
    po_silniku = "engine::text = %s" in p
    po_zdarzeniu = "data->>'event_id' = %s" in p
    spodziewane = po_silniku + po_zdarzeniu + 1      # +1: granica okna czasu
    assert p.count("%s") == spodziewane == len(params), (
        f"zapytanie ma {p.count('%s')} placeholderów i {len(params)} "
        f"parametrów — psycopg odrzuciłby je na wiązaniu")

    i = 0
    engine = params[i] if po_silniku else None
    i += po_silniku
    event_id = params[i] if po_zdarzeniu else None

    n = 0
    for e, zid in dane:
        if po_silniku and e != engine:
            continue
        if po_zdarzeniu and str(zid) != str(event_id):
            continue
        n += 1
    return n


@pytest.fixture
def bez_czekania(monkeypatch):
    """Skraca oba oczekiwania do dwóch prób i zeruje sen między nimi."""
    from services.engine import proba_killswitcha as pk

    async def nie_spij(_s):
        return None

    monkeypatch.setattr(pk, "KROK_S", 1)
    monkeypatch.setattr(pk, "CZEKAJ_DOZOR_S", 2)
    monkeypatch.setattr(pk.asyncio, "sleep", nie_spij)
    return pk


ZDARZENIE = {"id": MOJE_ZDARZENIE, "reason": "kapitał poniżej progu",
             "limit_name": "min_equity"}
MOJE_POWIADOMIENIE = [("crypto", MOJE_ZDARZENIE)]


# ------------------------------------------------- ogniwo 3: co widać, a co nie

def test_ogniwo_3_JEST_ODPYTANE_a_nie_tylko_wypisane_w_naglowku(bez_czekania):
    """Kanarek na całą wadę. Nagłówek modułu obiecywał cztery ogniwa, a między
    etykietami „OGNIWO 1-2" i „OGNIWO 4" nie stało ani jedno zapytanie o to,
    czy silnik cokolwiek wstrzymał.

    Atrapa wywraca się na zapytaniu, którego nie zna, więc brak zapytania
    o `journal.decision` NIE objawi się tutaj wyjątkiem — objawi się tym, że
    próba zalicza przebieg, w którym silnik zapisał wejście (test niżej).
    Ten test pilnuje rzeczy słabszej, ale koniecznej: że zapytanie w ogóle
    idzie i że jest zakotwiczone na własnym zdarzeniu.
    """
    pk = bez_czekania
    pytania = []
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE, wstrzymane=1)

    oryginal = fetch_one

    async def podsluch(sql, params=None):
        pytania.append(" ".join(sql.split()))
        return await oryginal(sql, params)

    assert asyncio.run(pk.uruchom(fetch_all, podsluch, execute)) == 0
    assert any("journal.decision" in q for q in pytania), (
        "próba nie zapytała o ślad wstrzymania wejść — ogniwo 3 nadal jest "
        "tylko napisem w nagłówku modułu")


def test_WEJSCIE_po_zadzialaniu_bezpiecznika_to_NIEZALICZONA(bez_czekania):
    """NAJWAŻNIEJSZY TEST W TYM PLIKU.

    Silnik z aktywnym kill-switchem, który zapisał decyzję `enter_long`, NIE
    wstrzymał wejść — czyli bezpiecznik nie zrobił jedynej rzeczy, która chroni
    pieniądze. Przed poprawką ten sam przebieg kończył się zdaniem „wszystkie
    ogniwa zadziałały" i kodem ZERO, bo nikt o wejścia nie pytał.
    """
    pk = bez_czekania
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE,
        wstrzymane=0, wejscia=2)

    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 1, (
        "silnik wszedł w pozycję mimo aktywnego kill-switcha, a próba "
        "zameldowała sukces")


def test_wejscie_przewaza_nad_wstrzymaniem(bez_czekania):
    """Kanarek do poprzedniego: jeden ślad wstrzymania NIE unieważnia wejścia.

    Gdyby kolejność warunków była odwrotna, wystarczyłby jeden wstrzymany
    zamiar, żeby próba przymknęła oko na pozycję otwartą obok niego — a to
    jest gorsze niż brak pomiaru, bo wygląda na pomiar.
    """
    pk = bez_czekania
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE,
        wstrzymane=5, wejscia=1)
    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 1


def test_brak_zamiaru_wejscia_to_NIE_ZMIERZONO_a_nie_ZALICZONA(bez_czekania,
                                                               capsys):
    """Ślad wstrzymania powstaje TYLKO wtedy, gdy silnik w oknie próby w ogóle
    chciał wejść. Decyzje zapadają raz na świecę (4h), a zamiarów wejścia jest
    rzędu dwóch na dobę — więc „ani jednego" jest tu stanem CZĘSTYM.

    Taki przebieg nie potwierdza łańcucha i nie ma prawa nazwać się zaliczonym.
    Nie ma też prawa krzyczeć: próba świecąca na czerwono codziennie zostanie
    wyłączona w tydzień i wtedy nie sprawdza się już nic. Stąd wynik
    NIEROZSTRZYGNIĘTY z kodem zero i z nazwanym ogniwem w treści.
    """
    pk = bez_czekania
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE,
        wstrzymane=0, wejscia=0)

    kod = asyncio.run(pk.uruchom(fetch_all, fetch_one, execute))
    tekst = capsys.readouterr()
    caly = tekst.out + tekst.err

    assert kod == 0, "brak okazji do pomiaru nie jest awarią bezpiecznika"
    assert "ZALICZONA" not in caly, (
        "przebieg, który ogniwa 3 nie zmierzył, melduje się jako zaliczony")
    assert "NIEROZSTRZYGNIĘTA" in caly
    assert "ogniwo 3" in caly, (
        "wynik nie mówi, KTÓREGO ogniwa nie zmierzono — a to jedyna informacja, "
        "która odróżnia ten przebieg od sukcesu")


def test_ZALICZONA_wymaga_ZMIERZENIA_kazdego_ogniwa(bez_czekania, capsys):
    """Druga strona tego samego: przy komplecie pomiarów zdanie o sukcesie ma
    paść, i ma paść razem ze stanem każdego ogniwa z osobna."""
    pk = bez_czekania
    fetch_one, fetch_all, execute, wykonane = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=MOJE_POWIADOMIENIE,
        wstrzymane=3, wejscia=0)

    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 0
    wyjscie = capsys.readouterr().out
    assert "PRÓBA ZALICZONA" in wyjscie
    for ogniwo in ("ogniwo 1-2", "ogniwo 3", "ogniwo 4"):
        assert ogniwo in wyjscie, f"wynik nie pokazuje stanu: {ogniwo}"

    # Zaliczona proba zwalnia WLASNE zdarzenie - inaczej zostawia silnik
    # wstrzymany wlasnym artefaktem.
    zwolnienia = [p for s, p in wykonane
                  if "UPDATE risk.killswitch_event SET released_at" in s]
    assert zwolnienia and zwolnienia[-1][-1] == MOJE_ZDARZENIE


# --------------------------------------------- ogniwo 4: CZYJE powiadomienie

def test_ogniwo_4_NIE_zalicza_powiadomienia_innego_SILNIKA(bez_czekania):
    """`killswitch.triggered` nie należy do tej próby ani do tego silnika.

    Powiadomienie o zatrzymaniu silnika `equity` powstaje niezależnie i w tym
    samym oknie czasu. Zapytanie bez `engine::text = %s` liczyło je jako dowód,
    że TEN kanał alarmowy działa.
    """
    pk = bez_czekania
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=[("equity", MOJE_ZDARZENIE)],
        wstrzymane=1)

    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 1, (
        "ogniwo 4 zaliczyło powiadomienie o zatrzymaniu INNEGO silnika")


def test_ogniwo_4_NIE_zalicza_powiadomienia_o_INNYM_zdarzeniu(bez_czekania):
    """Ten sam silnik, ale inne zdarzenie — na przykład przypomnienie dozoru
    o zatrzymaniu sprzed godziny (`uzgodnienie.py`: „chodzi bez wyciszenia co
    dziesięć minut"). W oknie `CZEKAJ_DOZOR_S` takie przypomnienie trafia się
    regularnie, a przed poprawką było nieodróżnialne od zgłoszenia o naszym
    własnym zdarzeniu.
    """
    pk = bez_czekania
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE, powiadomienia=[("crypto", 12)], wstrzymane=1)

    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 1, (
        "ogniwo 4 zaliczyło powiadomienie o CUDZYM zdarzeniu tego samego "
        "silnika — a wtedy mierzy kanał alarmowy cudzą wiadomością")


def test_ogniwo_4_zalicza_WLASNE_powiadomienie(bez_czekania):
    """Kanarek do obu powyższych: filtr nie może być tak ciasny, żeby odrzucał
    zgłoszenie o naszym własnym zdarzeniu. Bez tego dwa testy wyżej dałyby się
    zaspokoić zapytaniem, które nie zalicza nigdy niczego."""
    pk = bez_czekania
    fetch_one, fetch_all, execute, _ = _atrapa(
        zdarzenie=ZDARZENIE,
        powiadomienia=[("equity", MOJE_ZDARZENIE), ("crypto", 12),
                       ("crypto", MOJE_ZDARZENIE)],
        wstrzymane=1)

    assert asyncio.run(pk.uruchom(fetch_all, fetch_one, execute)) == 0


# ------------------------------------------------ nazwa gałęzi i nadanie roli

def test_powod_wstrzymania_pochodzi_Z_MAPY_loop_a_nie_z_napisu():
    """Napis przepisany do próby rozjechałby się z `loop.OPIS_POWODU_HOLDU`
    po cichu: zapytanie przestałoby znajdować wiersze, a próba meldowałaby, że
    silnik NIE wstrzymał wejść — czyli fałszywą awarię działającego bezpiecznika.

    Indeksowanie mapy zamienia ten rozjazd w KeyError przy imporcie.
    """
    from services.engine.loop import OPIS_POWODU_HOLDU
    from services.engine.proba_killswitcha import (
        OPIS_WSTRZYMANIA,
        POWOD_WSTRZYMANIA,
    )

    assert POWOD_WSTRZYMANIA in OPIS_POWODU_HOLDU
    assert OPIS_WSTRZYMANIA == OPIS_POWODU_HOLDU[POWOD_WSTRZYMANIA]

    # Ten sam zbior zamkniety pilnuje CHECK z migracji 0040 - powod spoza listy
    # nie wszedlby do bazy, wiec proba szukalaby wartosci, ktorej tam nie ma.
    migracja = (KORZEN / "db/migrations/0040_powod_holdu.sql").read_text(
        encoding="utf-8")
    assert f"'{POWOD_WSTRZYMANIA}'" in migracja


def test_rola_proby_MA_NADANIE_na_dziennik_decyzji():
    """Zapytanie ogniwa 3 sięga do schematu `journal`, do którego rola
    `trader_proba` nie miała ani USAGE, ani SELECT (migracja 0034 dała jej
    tylko tyle, ile wymagało podniesienie progu i zwolnienie zdarzenia).

    Bez nadania zapytanie kończy się `permission denied for schema journal`
    w ŚRODKU próby: próg wraca w `finally`, ale zadanie cykliczne pokazuje ślad
    stosu zamiast wyniku, a łańcuch nie jest sprawdzony ani razu. Kod i nadanie
    wyglądają poprawnie z osobna — Postgres mówi o rozjeździe dopiero przy
    wykonaniu, czyli w jedynym miejscu, gdzie to kosztuje.
    """
    nadania = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((KORZEN / "db/migrations").glob("*.sql")))
    kod = "\n".join(w for w in nadania.splitlines()
                    if not w.lstrip().startswith("--"))
    zwarty = " ".join(kod.split())

    assert "GRANT USAGE ON SCHEMA journal TO trader_proba" in zwarty

    # NADANIE KOLUMNOWE, i to na DOKLADNIE te kolumny, ktorych dotyka zapytanie.
    # Nadanie na cala tabele przeszloby test slabszy ("czy w ogole czyta"),
    # a wpuscilo probe w `feature_snapshot`, `rationale_model` i kazda kolumne
    # dopisana pozniej - czyli rozszerzaloby jej zasieg przy okazji.
    import re

    from services.engine.proba_killswitcha import _ogniwo_3

    m = re.search(r"GRANT SELECT \(([^)]*)\) ON journal\.decision "
                  r"TO trader_proba", zwarty)
    assert m, ("nadanie na `journal.decision` nie jest kolumnowe — "
               "`GRANT SELECT ON <tabela>` daje próbie cały dziennik decyzji")
    nadane = {k.strip() for k in m.group(1).split(",")}

    import inspect
    sql = " ".join(inspect.getsource(_ogniwo_3).split())
    dotykane = set(re.findall(r"\bd\.(\w+)", sql))
    assert dotykane <= nadane, (
        f"zapytanie ogniwa 3 czyta {sorted(dotykane - nadane)} — kolumny bez "
        f"nadania. Postgres powie o tym dopiero przy wykonaniu, czyli w środku "
        f"próby")
    assert nadane <= dotykane, (
        f"nadane, a nieczytane: {sorted(nadane - dotykane)} — nadanie szersze "
        f"niż pomiar przestaje być decyzją i staje się zapasem na przyszłość")

    for zapis in ("INSERT", "UPDATE", "DELETE"):
        assert f"GRANT {zapis} ON journal.decision TO trader_proba" not in zwarty, (
            "próba, która może dopisać do dziennika, mierzy własny INSERT "
            "zamiast zachowania silnika")
    assert ("GRANT SELECT ON ALL TABLES IN SCHEMA journal TO trader_proba"
            not in zwarty), "nadanie na cały schemat dziennika"
