"""Druga krzywa kapitału — w walucie, w której właściciel naprawdę wpłacił.

DLACZEGO TEN PLIK ISTNIEJE

Warunek właściciela brzmi „wpłacę 10 tysięcy i nie chcę, żeby rachunek poszedł
na minus". System mierzy w USDC, a właściciel wpłaca w EUR. Stan „kill-switch
zielony, wszystkie limity w normie, 10 000 USDC na rachunku" jest więc w pełni
zgodny ze stanem „właściciel ma o kilka procent mniej, niż wpłacił" — bo
bezpiecznik mierzy w walucie, w której tej straty nie widać.

Zmierzone na USDCEUR z api.bybit.eu, 452 zamknięte świece dzienne
2025-06-14 … 2026-09-08: min 0,8318, max 0,8806, zakres 5,87%. Wynik strategii
to około 2,9% rocznie. Ruch kursu jest większy niż DWULETNIA przewaga.

Liczby w testach niżej są wzięte z tego pomiaru, a nie dobrane pod tezę: 0,8806
to zmierzone maksimum, 0,8318 zmierzone minimum, +2,90% to zmierzony wynik
roczny. Scenariusz „zielono w USDC, pod wodą w EUR" nie jest więc konstrukcją
teoretyczną, tylko przepisaniem danych.

CZEGO TE TESTY PILNUJĄ SZCZEGÓLNIE

  1. kurs NIGDY z przyszłości — punkt z czerwca przeliczony kursem z lipca
     pokazuje rachunek, którego wtedy nie było (błąd klasy `feat.as_of()`),
  2. brak kursu to LUKA, nie zero i nie wartość w USDC udająca euro,
  3. wpłata przeliczana kursem Z DNIA WPŁATY, nie dzisiejszym,
  4. ten moduł NIE RUSZA bezpiecznika — dodaje miarę, nie przestawia progu.

Każde twierdzenie zostało zweryfikowane kanarkiem: do kodu wstawiano wersję
błędną (tę, którą naturalnie napisałby ktoś nieświadomy problemu) i sprawdzano,
że test pada. Lista kanarków jest w raporcie zadania.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from services.engine import waluta as w
from services.engine.waluta import (
    BrakZrodlaKursu, Kurs, krzywa_dwuwalutowa, kurs_na, podsumowanie,
    wynik_wlasciciela, zrodlo_dla,
)

KORZEN = Path(__file__).resolve().parents[1]
UTC = timezone.utc

# Skrajne kursy ZMIERZONE na USDCEUR (api.bybit.eu, 2025-06-14 … 2026-09-08).
SZCZYT = Decimal("0.8806")
DOLEK = Decimal("0.8318")


def czas(dzien: int, godzina: int = 0) -> datetime:
    """Dzień stycznia/roku 2026 — skrót, żeby liczby w testach były widoczne."""
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=dzien - 1,
                                                        hours=godzina)


def kurs(dzien: int, wartosc) -> Kurs:
    return Kurs(czas=czas(dzien), kurs=Decimal(str(wartosc)), zrodlo="test")


# ------------------------------------------------- kurs nie może być z przyszłości

def test_kurs_z_przyszlosci_nie_jest_uzywany():
    """Najbliższy w czasie ≠ obowiązujący.

    Kurs zapisany DZIEŃ PO punkcie krzywej jest bliżej niż ten sprzed dwóch dni,
    więc „weź najbliższy" wziąłby właśnie jego. Byłoby to przeliczenie
    przeszłości ceną, której wtedy nikt nie znał — ten sam błąd, przed którym
    broni bitemporalność cech.
    """
    kursy = [kurs(1, "0.80"), kurs(4, "0.90")]
    k, powod = kurs_na(czas(3), kursy, max_wiek=timedelta(days=5))
    assert k is not None and powod is None
    assert k.kurs == Decimal("0.80"), (
        "wzięto kurs z 4 stycznia do punktu z 3 stycznia — to cena z przyszłości")


def test_kurs_sprzed_calej_historii_to_luka_a_nie_najstarszy_znany():
    """Punkt starszy niż cała nasza historia kursów nie ma kursu.

    Podstawienie najstarszego znanego byłoby dokładnie tym samym błędem co wyżej,
    tylko o rok wcześniej — i uderzyłoby w walutę PLN, której historia u źródła
    zaczyna się dopiero 2026-04-30.
    """
    k, powod = kurs_na(czas(1), [kurs(10, "0.86")])
    assert k is None
    assert "przyszłości" in powod, powod


def test_kurs_zbyt_stary_to_luka():
    """Przeciąganie ostatniego znanego kursu w nieskończoność zamienia przerwę
    w źródle w płaski odcinek krzywej — czyli w stwierdzenie „kurs się nie
    zmieniał", którego nikt nie zmierzył."""
    k, powod = kurs_na(czas(10), [kurs(1, "0.86")], max_wiek=timedelta(hours=36))
    assert k is None
    assert "sprzed" in powod and "progu" in powod, powod


def test_kurs_w_granicach_progu_jest_uzywany():
    """Kanarek odwrotny do poprzedniego: przy świecy dziennej najświeższy
    dostępny kurs dla „teraz" ma z definicji do doby, więc próg ciaśniejszy niż
    doba robiłby lukę z KAŻDEGO ostatniego punktu krzywej."""
    k, _ = kurs_na(czas(2, 20), [kurs(2, "0.86")])
    assert k is not None and k.kurs == Decimal("0.86")


def test_brak_kursow_w_ogole_mowi_wprost():
    k, powod = kurs_na(czas(1), [])
    assert k is None and "ani jednego kursu" in powod


# ----------------------------------------------- brak kursu to luka, nie zero

def test_brak_kursu_nie_daje_zera_ani_wartosci_w_usdc():
    """NAJWAŻNIEJSZE TWIERDZENIE TEGO PLIKU po stronie prezentacji.

    Punkt bez kursu ma być nieprzeliczony. Zero skłamałoby o całość rachunku,
    a przepisanie wartości w USDC skłamałoby o kilka procent i wyglądałoby
    przy tym na poprawny wynik — czyli byłoby gorsze.
    """
    punkty = [(czas(10), Decimal("10000"))]
    krzywa = krzywa_dwuwalutowa(punkty, [kurs(1, "0.86")])
    p = krzywa[0]
    assert p.equity == Decimal("10000"), "pierwsza krzywa zostaje nietknięta"
    assert p.equity_odniesienia is None
    assert p.equity_odniesienia != Decimal("0")
    assert p.equity_odniesienia != p.equity
    assert p.kurs is None
    assert p.luka, "luka musi nieść POWÓD, inaczej nie da się jej naprawić"


def test_luka_w_srodku_nie_psuje_punktow_wokol():
    """Przerwa w źródle nie ma prawa unieważnić punktów, dla których kurs jest.
    Inaczej pierwsza awaria zbierania kursu skasowałaby całą drugą krzywą."""
    punkty = [(czas(1), Decimal("10000")),
              (czas(9), Decimal("10100")),      # dziura w kursach
              (czas(20), Decimal("10200"))]
    kursy = [kurs(1, "0.88"), kurs(20, "0.83")]
    krzywa = krzywa_dwuwalutowa(punkty, kursy)
    assert [p.equity_odniesienia is None for p in krzywa] == [False, True, False]


# ------------------------------------- każdy punkt własnym kursem, nie dzisiejszym

def test_kazdy_punkt_przeliczony_kursem_ZE_SWOJEJ_chwili():
    """Gdyby całą historię przeliczyć dzisiejszym kursem, krzywa w EUR byłaby
    tylko przeskalowaną krzywą w USDC — czyli nie pokazywałaby niczego nowego
    i nie zauważyłaby ruchu kursu, dla którego istnieje.

    Liczby dobrane tak, że obie wersje dają RÓŻNY wynik dla pierwszego punktu.
    """
    punkty = [(czas(1), Decimal("10000")), (czas(20), Decimal("10000"))]
    kursy = [kurs(1, SZCZYT), kurs(20, DOLEK)]
    a, b = krzywa_dwuwalutowa(punkty, kursy)
    assert a.equity_odniesienia == Decimal("10000") * SZCZYT     # 8806.00
    assert b.equity_odniesienia == Decimal("10000") * DOLEK      # 8318.00
    assert a.equity_odniesienia != b.equity_odniesienia, (
        "obie chwile przeliczono tym samym kursem — historia jest przepisana")


# --------------------------------------- różnica między krzywymi to cała treść

def test_zielono_w_usdc_i_pod_woda_w_eur():
    """SCENARIUSZ Z WARUNKU WŁAŚCICIELA, przepisany ze zmierzonych liczb.

    Rok pracy: +2,90% w USDC — dokładnie zmierzony wynik strategii. Kurs w tym
    czasie zszedł ze zmierzonego maksimum na zmierzone minimum. W USDC rachunek
    rośnie i każdy limit jest zielony; w EUR właściciel ma mniej, niż włożył.
    """
    punkty = [(czas(1), Decimal("10000")), (czas(20), Decimal("10290"))]
    s = podsumowanie(krzywa_dwuwalutowa(punkty, [kurs(1, SZCZYT), kurs(20, DOLEK)]))
    assert s["zmiana_pomiaru_pct"] == pytest.approx(2.90, abs=0.01)
    assert s["zmiana_odniesienia_pct"] < 0, (
        "krzywa w walucie właściciela musi pokazać stratę, której USDC nie widzi")
    assert s["roznica_pp"] == pytest.approx(-5.70, abs=0.05)
    assert s["luk"] == 0


def test_obie_zmiany_liczone_miedzy_TYMI_SAMYMI_punktami():
    """Zmiana w USDC po całej krzywej i zmiana w EUR po jej przeliczonym
    wycinku opisywałyby dwa różne okresy, a ich różnica nie znaczyłaby nic.

    Tu pierwszy punkt nie ma kursu, więc oba procenty muszą liczyć się od
    drugiego. Liczby dobrane tak, że wersja licząca USDC po całości dałaby
    +10%, a poprawna +2%.
    """
    punkty = [(czas(1), Decimal("10000")),     # bez kursu
              (czas(10), Decimal("10800")),
              (czas(11), Decimal("11000"))]
    kursy = [kurs(10, "0.86"), kurs(11, "0.86")]
    s = podsumowanie(krzywa_dwuwalutowa(punkty, kursy))
    assert s["luk"] == 1
    assert s["zmiana_pomiaru_pct"] == pytest.approx(1.85, abs=0.01), (
        "zmianę w walucie pomiaru policzono po innym odcinku niż zmianę w EUR")
    assert s["od"] == czas(10)


def test_jeden_przeliczony_punkt_to_brak_odpowiedzi_a_nie_zero_procent():
    """Procent policzony od jednego końca jest liczbą bez sensu, a nie liczbą
    niepewną. Zero procent w tym miejscu czytałoby się jako „kurs nie zaszkodził"."""
    punkty = [(czas(1), Decimal("10000")), (czas(30), Decimal("10290"))]
    s = podsumowanie(krzywa_dwuwalutowa(punkty, [kurs(30, "0.86")]))
    assert s["przeliczonych"] == 1
    assert s["zmiana_odniesienia_pct"] is None
    assert s["roznica_pp"] is None
    assert s["powod_luki"]


# --------------------------------------------- pytanie właściciela wprost

def test_wplata_przeliczana_kursem_Z_DNIA_WPLATY():
    """Właściciel nie wpłacił „10 000 USDC" — wpłacił tyle euro, ile ich wtedy
    kosztowało, i tę kwotę chce zobaczyć z powrotem.

    Przeliczenie wpłaty dzisiejszym kursem dałoby 8318 EUR i wynik +241 EUR,
    czyli ZYSK. Poprawnie: wpłacił 8806 EUR, ma 8559 EUR — czyli STRATĘ.
    Dwie wersje różnią się znakiem odpowiedzi na jedyne pytanie właściciela.
    """
    przeplywy = [(czas(1), Decimal("10000"))]
    kursy = [kurs(1, SZCZYT), kurs(20, DOLEK)]
    r = wynik_wlasciciela(przeplywy, Decimal("10290"), kursy, czas=czas(20))

    assert r["wynik_pomiar"] == Decimal("290"), "w USDC to zysk"
    assert r["wplacone_odniesienia"] == Decimal("10000") * SZCZYT
    assert r["wynik_odniesienia"] < 0, (
        "wpłatę przeliczono dzisiejszym kursem — strata zniknęła")
    assert float(r["wynik_odniesienia"]) == pytest.approx(-246.778, abs=0.001)


def test_luka_w_jednym_przeplywie_uniewaznia_cala_odpowiedz():
    """Suma wpłat policzona z CZĘŚCI wpłat jest gorsza niż jej brak, bo wygląda
    dokładnie tak samo jak pełna — a jest punktem odniesienia dla progu
    „nie na minus"."""
    przeplywy = [(czas(1), Decimal("10000")),      # bez kursu
                 (czas(20), Decimal("5000"))]
    r = wynik_wlasciciela(przeplywy, Decimal("15000"), [kurs(20, DOLEK)],
                          czas=czas(20))
    assert r["wplacone_odniesienia"] is None
    assert r["wynik_odniesienia"] is None
    assert "NIEZNANA" in r["luka"]
    assert r["wplacone_pomiar"] == Decimal("15000"), (
        "odpowiedź w walucie pomiaru zostaje — luka dotyczy tylko przeliczenia")


def test_brak_biezacego_kursu_zostawia_sume_wplat_ale_nie_wynik():
    """Znamy, ile włożył; nie wiemy, ile ma. Podanie wyniku wymagałoby zgadnięcia
    dzisiejszego kursu, a zgadnięty kurs jest tu wart tyle, co zgadnięty wynik."""
    r = wynik_wlasciciela([(czas(1), Decimal("10000"))], Decimal("10290"),
                          [kurs(1, SZCZYT)], czas=czas(30))
    assert r["wplacone_odniesienia"] == Decimal("10000") * SZCZYT
    assert r["equity_odniesienia"] is None
    assert r["wynik_odniesienia"] is None
    assert "bieżący kurs" in r["luka"]


def test_brak_przeplywow_to_nie_zerowa_wplata():
    """Rachunek bez zapisanego przepływu nie jest rachunkiem z wpłatą zero —
    wynik policzony od zera pokazałby cały kapitał jako zysk."""
    r = wynik_wlasciciela([], Decimal("10000"), [kurs(1, SZCZYT)], czas=czas(1))
    assert r["wynik_odniesienia"] is None
    assert "ani jednego zapisanego przepływu" in r["luka"]


# ------------------------------------------------------- źródło kursu i zapis

def test_nieznana_waluta_konczy_sie_bledem_a_nie_cichym_eur():
    """Raport w walucie, o którą nikt nie prosił, jest gorszy niż brak raportu,
    bo wygląda na odpowiedź."""
    with pytest.raises(BrakZrodlaKursu):
        zrodlo_dla("CHF")


def test_pln_jest_obslugiwany_ale_z_wypisanym_zastrzezeniem():
    """Waluta odniesienia ma być USTAWIENIEM, nie stałą — więc PLN musi działać.
    Źródło jest jednak wyraźnie gorsze niż dla EUR (krótsza historia, cieńszy
    obrót) i właściciel ma to zobaczyć, zanim przestawi ustawienie."""
    z = zrodlo_dla("PLN")
    assert z.para == "USDCPLN"
    assert "bybit.eu" in z.host, "USDCPLN nie istnieje na hoście globalnym"
    assert z.zastrzezenie and "2026-04-30" in z.zastrzezenie
    assert zrodlo_dla("EUR").zastrzezenie is None


def test_swieca_niedomknieta_nie_wchodzi_do_historii(monkeypatch):
    """Zamknięcie świecy TRWAJĄCEJ to bieżąca cena, która jeszcze się zmieni.
    Zapisana pod czasem końca świecy byłaby kursem z przyszłości — a przy
    `ON CONFLICT DO NOTHING` zostałaby tam na zawsze."""
    teraz = datetime(2026, 9, 8, 12, tzinfo=UTC)
    zamknieta = int(datetime(2026, 9, 7, tzinfo=UTC).timestamp() * 1000)
    trwajaca = int(datetime(2026, 9, 8, tzinfo=UTC).timestamp() * 1000)
    _podstaw_httpx(monkeypatch, {
        "retCode": 0,
        "result": {"list": [
            [str(trwajaca), "0.8602", "0.8613", "0.8596", "0.8613", "1", "1"],
            [str(zamknieta), "0.8613", "0.8614", "0.8596", "0.8602", "1", "1"],
        ]}})
    kursy = asyncio.run(w.kursy_dzienne("https://api.bybit.eu", "USDCEUR",
                                        teraz=teraz))
    assert len(kursy) == 1, "świeca trwająca trafiła do historii kursów"
    assert kursy[0].kurs == Decimal("0.8602")


def test_zamkniecie_stemplowane_KONCEM_swiecy_a_nie_poczatkiem(monkeypatch):
    """Zamknięcie świecy zaczynającej się 7 września jest ZNANE dopiero
    8 września o północy. Ostemplowanie go początkiem świecy wstawiłoby do
    historii cenę z przyszłości i przeliczyło wczorajszą krzywą kursem, którego
    wczoraj nikt nie znał."""
    poczatek = datetime(2026, 9, 7, tzinfo=UTC)
    _podstaw_httpx(monkeypatch, {
        "retCode": 0,
        "result": {"list": [[str(int(poczatek.timestamp() * 1000)),
                             "0.8613", "0.8614", "0.8596", "0.8602", "1", "1"]]}})
    kursy = asyncio.run(w.kursy_dzienne("https://api.bybit.eu", "USDCEUR",
                                        teraz=datetime(2026, 9, 9, tzinfo=UTC)))
    assert kursy[0].czas == poczatek + timedelta(days=1)


def test_odmowa_zrodla_nie_wraca_pusta_lista(monkeypatch):
    """Pusta lista jest nieodróżnialna od „nic nowego nie ma", a to dwa różne
    stany. Cichy brak kursu to dokładnie ta luka, o którą tu chodzi."""
    _podstaw_httpx(monkeypatch, {"retCode": 10001, "retMsg": "Not supported symbols",
                                 "result": {}})
    with pytest.raises(RuntimeError, match="odmówił"):
        asyncio.run(w.kursy_dzienne("https://api.bybit.com", "USDCPLN"))


def test_zapis_nie_nadpisuje_juz_zapisanego_kursu():
    """Kurs raz zapisany pod daną chwilą jest tym, co system wtedy wiedział.
    `DO UPDATE` skasowałoby jedyny ślad tego, na czym oparto raport z tamtego
    dnia — bitemporalność bez niezmienności przeszłości nie jest bitemporalnością.
    """
    zapisane = []

    async def execute(sql, params=None):
        zapisane.append((" ".join(sql.split()), params))

    asyncio.run(w.zapisz_kursy(execute, "USDC", "EUR", [kurs(1, "0.86")]))
    sql, params = zapisane[0]
    assert "ON CONFLICT DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert params[:2] == ("USDC", "EUR")


def test_kurs_nieodczytany_nie_staje_sie_wierszem():
    """`cena_odniesienia` zwraca None, gdy odczyt się nie uda — i to None ma iść
    dalej. Podstawienie tu jedynki albo ostatniego znanego kursu zamieniłoby
    lukę w liczbę."""
    class Adapter:
        _host = "https://api.bybit.eu"

        async def cena_odniesienia(self, symbol, kategoria=None):
            return None

    assert asyncio.run(w.kurs_biezacy(Adapter(), "USDCEUR")) is None


# --------------------------------------- bezpiecznik zostaje nietknięty

def test_modul_nie_dotyka_regul_ryzyka():
    """Zadaniem tego modułu jest POKAZAĆ drugą liczbę, nie przestawić próg.

    Import reguł ryzyka byłby pierwszym krokiem do przeliczania `min_equity`
    kursem — czyli do zmiany ZACHOWANIA bezpiecznika pod pozorem dodania miary.
    Czy próg ma stać w USDC, czy w walucie właściciela, jest decyzją właściciela.
    """
    import ast

    zrodlo = (KORZEN / "services/engine/waluta.py").read_text(encoding="utf-8")
    drzewo = ast.parse(zrodlo)

    # IMPORTY czytane z drzewa składni, a nie szukane jako napis: docstring tego
    # modułu MÓWI o risk_rules i o min_equity, i ma o nich mówić. Chodzi o to,
    # czego kod DOTYKA, nie o to, o czym komentarz wspomina.
    moduly = set()
    for n in ast.walk(drzewo):
        if isinstance(n, ast.ImportFrom):
            # NAZWY też, nie sam moduł: `from services.engine import risk_rules`
            # ma `module = "services.engine"`, więc sprawdzenie samego modułu
            # przepuściłoby dokładnie ten import, o który chodzi.
            moduly.add(n.module or "")
            moduly |= {a.name for a in n.names}
        elif isinstance(n, ast.Import):
            moduly |= {a.name for a in n.names}
    assert not [m for m in moduly if "risk" in m], (
        f"waluta.py importuje {sorted(m for m in moduly if 'risk' in m)} — "
        f"to już nie jest dodanie miary, tylko sięgnięcie do bezpiecznika")

    # Żadnego SQL-a dotykającego stanu, na którym stoi kill-switch. Zapis do
    # tych tabel byłby zmianą zachowania bezpiecznika pod pozorem raportu.
    for zakazane in ("risk.limit_def", "risk.killswitch_event",
                     "risk.violation", "INSERT INTO risk", "UPDATE risk",
                     "INSERT INTO exec", "UPDATE exec"):
        assert zakazane not in zrodlo, (
            f"waluta.py zapisuje albo czyta {zakazane!r} — czy próg ma stać "
            f"w USDC, czy w walucie właściciela, jest decyzją właściciela")


def test_migracja_0036_nie_rusza_schematu_risk():
    """Ta sama własność po stronie bazy. Migracja dodaje kurs i ustawienie;
    gdyby dotykała `risk`, zmieniałaby to, na czym stoi kill-switch."""
    plik = KORZEN / "db/migrations/0036_waluta_odniesienia.sql"
    assert plik.exists(), "brak migracji 0036"
    kod = "\n".join(l.split("--")[0] for l in
                    plik.read_text(encoding="utf-8").splitlines())
    assert "risk." not in kod, "migracja 0036 dotyka schematu risk"
    assert "exec.equity_point" not in kod, (
        "migracja 0036 zmienia tabelę, z której czyta risk kernel")
    assert "market.fx_rate" in kod and "app.waluta_odniesienia" in kod


def test_waluta_odniesienia_jest_ustawieniem_a_nie_stala():
    """Właściciel może liczyć w PLN, nie w EUR. Zmiana sposobu, w jaki człowiek
    patrzy na własne pieniądze, nie ma prawa wymagać wdrożenia."""
    kod = "\n".join(l.split("--")[0] for l in
                    (KORZEN / "db/migrations/0036_waluta_odniesienia.sql")
                    .read_text(encoding="utf-8").splitlines())
    assert "CREATE TABLE IF NOT EXISTS app.waluta_odniesienia" in kod
    # Wartość początkowa musi być w samym INSERT-cie, a nie gdziekolwiek
    # w pliku: „brak ustawienia" i „ustawione na EUR" to dwa różne stany,
    # a raport milczący z powodu pustej tabeli wygląda dokładnie tak samo jak
    # raport milczący z powodu braku kursu.
    assert "VALUES (true, 'EUR'" in kod, (
        "migracja nie wstawia waluty początkowej — pusta tabela to nie ustawienie")
    zrodlo = (KORZEN / "services/engine/waluta.py").read_text(encoding="utf-8")
    assert "app.waluta_odniesienia" in zrodlo, (
        "moduł nie czyta ustawienia, więc waluta jest jednak stałą")


# ------------------------------------------------------------------ pomocnicze

def _podstaw_httpx(monkeypatch, dane: dict) -> None:
    """Podmiana klienta HTTP. Test odczytu świec nie ma prawa wymagać giełdy —
    inaczej pada przy każdej awarii sieci i uczy się go pomijać."""
    import httpx

    class Odpowiedz:
        def raise_for_status(self):
            return None

        def json(self):
            return dane

    class Klient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return Odpowiedz()

    monkeypatch.setattr(httpx, "AsyncClient", Klient)
