"""Świeży rachunek — czy limity, które go chronią, w ogóle się LICZĄ.

DWA RÓŻNE PYTANIA, KTÓRE ŁATWO POMYLIĆ

1. „Czy brak historii ZYSKU wyłącza ochronę?"  — NIE. `wynik` to wartość
   jednostki uczestnictwa: startuje od 1.0 i schodzi w dół od straty. Warunki
   `state.wynik_doba > 0` i `state.wynik_szczyt > 0` w `evaluate_account` są
   zabezpieczeniem przed dzieleniem przez zero i na rachunku, który nigdy nie
   był w zysku, są spełnione od pierwszego punktu krzywej. Testy niżej pilnują
   tego wprost, bo gdyby ktoś zmienił `nav` na skumulowany P&L (który startuje
   od ZERA), oba te warunki stałyby się dziurą, a nic by się nie wywróciło.

2. „Czy brak historii DOBY wyłącza ochronę?" — TAK, wyłączała. `_wynik()`
   szukało punktu sprzed 24 h, a gdy go nie było (rachunek młodszy niż doba),
   brało punkt BIEŻĄCY. Strata dzienna liczona względem samej siebie wynosi
   0,00% zawsze — miara przestawała reagować na wejście. Pierwsza doba
   rachunku to dokładnie ten dzień, w którym limit straty dziennej jest
   martwy, bez błędu i z zielonym statusem.

Ten sam problem był już rozwiązany poprawnie w dwóch innych miejscach:
`routers/overview.py` spada na PIERWSZY punkt krzywej (`first`), a symulacja
portfelowa na `START_EQUITY`. Rozjechała się wyłącznie ścieżka bezpiecznika.
"""
from decimal import Decimal

import pytest

from services.engine.risk import RiskKernel
from services.engine.risk_rules import RiskState, evaluate_account


# ------------------------------------------------------- atrapa krzywej kapitału

class KursorKrzywej:
    """Atrapa kursora znająca DOKŁADNIE te warunki, o które chodzi w teście:
    filtr wieku (`24 hours`), filtr `nav IS NOT NULL` i kierunek sortowania.

    Atrapa oddająca zawsze ten sam wiersz nie odróżniłaby zapytania o punkt
    sprzed doby od zapytania o punkt bieżący — czyli byłaby ślepa na jedyną
    rzecz, którą ten plik ma sprawdzać.
    """

    def __init__(self, punkty: list[dict]):
        # punkty: [{"wiek_h": float, "equity": ..., "nav": ..., "wplaty": ...}]
        # posortowane rosnąco po czasie (najstarszy pierwszy)
        self.punkty = sorted(punkty, key=lambda p: -p["wiek_h"])
        self.zapisy: list[tuple] = []
        self._wynik: list[dict] = []

    def execute(self, sql: str, params=None):
        s = " ".join(sql.split())
        self._wynik = []
        if s.startswith("INSERT"):
            self.zapisy.append((s, params))
            return self
        # Wszystko, co nie dotyczy krzywej kapitalu (pozycje, zlecenia,
        # kill-switch, zuzycie limitow), oddaje pustke. Jawny warunek zamiast
        # listy wyjatkow: nowe zapytanie w kernelu ma tu wpasc w "brak danych",
        # a nie dostac przypadkiem wiersz krzywej z nieswoimi kolumnami.
        if "exec.equity_point" not in s:
            return self
        if "wiek_min" in s:
            # Wiek NAJSWIEZSZEGO punktu w minutach - kernel wstrzymuje handel,
            # gdy krzywa stoi.
            self._wynik = [{"wiek_min": self.punkty[-1]["wiek_h"] * 60
                            if self.punkty else None}]
            return self
        wiersze = list(self.punkty)
        if "nav IS NOT NULL" in s:
            wiersze = [p for p in wiersze if p.get("nav") is not None]
        if "max(equity)" in s:
            self._wynik = [{"peak": max((p["equity"] for p in wiersze), default=None)}]
            return self
        if "max(nav)" in s:
            nav = [p["nav"] for p in self.punkty if p.get("nav") is not None]
            self._wynik = [{"szczyt": max(nav) if nav else None,
                            "bez_nav": sum(1 for p in self.punkty
                                           if p.get("nav") is None)}]
            return self
        if "24 hours" in s:
            wiersze = [p for p in wiersze if p["wiek_h"] >= 24]
        if "ORDER BY event_time ASC" in s:
            wiersze = wiersze[:1]
        else:                                    # DESC - najnowszy
            wiersze = wiersze[-1:]
        self._wynik = [{"equity": p["equity"], "nav": p.get("nav"),
                        "wplaty_netto": p.get("wplaty")} for p in wiersze]
        return self

    def fetchone(self):
        return self._wynik[0] if self._wynik else None

    def fetchall(self):
        return self._wynik


class Polaczenie:
    def __init__(self, kursor):
        self._kursor = kursor

    def cursor(self, *a, **k):
        return self._kursor


# Komplet limitów wymaganych przez LIMITY_KONIECZNE, w postaci, w której
# działają na rachunku o dowolnej wielkości: próg kapitału jako ułamek wpłat,
# a nie kwota dobrana pod rachunek 100 000.
LIMITY = {
    "max_daily_loss": {"id": 1, "value": Decimal("2"), "unit": "pct",
                       "action": "killswitch"},
    "max_drawdown": {"id": 2, "value": Decimal("10"), "unit": "pct",
                     "action": "killswitch"},
    "min_equity": {"id": 3, "value": Decimal("90"), "unit": "pct_wplat",
                   "action": "killswitch"},
}


def kernel(punkty, limity=None) -> tuple[RiskKernel, KursorKrzywej]:
    kur = KursorKrzywej(punkty)
    k = RiskKernel.__new__(RiskKernel)
    k.conn, k.engine, k.mode = Polaczenie(kur), "crypto", "live"
    k._limits = limity or LIMITY
    return k, kur


def punkt(wiek_h, nav, wplaty=10_000):
    """Punkt krzywej rachunku, na który wpłacono `wplaty` przy nav = 1."""
    return {"wiek_h": wiek_h, "nav": Decimal(str(nav)), "wplaty": Decimal(wplaty),
            "equity": Decimal(wplaty) * Decimal(str(nav))}


# ------------------------------------- dziura: pierwsza doba bez straty dziennej

def test_strata_dzienna_dziala_w_PIERWSZEJ_DOBIE_rachunku():
    """KANAREK. Rachunek istnieje od trzech godzin i stracił 6% — trzykrotność
    limitu dziennego. Nie ma punktu sprzed 24 h, więc odniesieniem musi być
    PIERWSZY punkt krzywej: dla rachunku młodszego niż doba „od początku" i „od
    doby" to ta sama rzecz.

    Przed poprawką odniesieniem był punkt BIEŻĄCY, więc strata dzienna wynosiła
    0,00% i werdykt brzmiał `allow` — przy 6% straty i wszystkich trzech
    limitach obecnych w bazie.
    """
    k, kur = kernel([punkt(3, 1.0), punkt(1, 0.96), punkt(0, 0.94)])
    v = k.check_account()

    assert v.verdict == "block", (
        f"6% straty w pierwszej dobie przeszło jako {v.verdict}; "
        f"zużycie limitów: {v.usage}")
    assert v.trigger_limit == "max_daily_loss"
    uzycie = {u["name"]: u["used"] for u in v.usage}
    assert uzycie["max_daily_loss"] == pytest.approx(6.0, abs=0.01), (
        f"strata dzienna zmierzona jako {uzycie.get('max_daily_loss')}% zamiast 6%")
    assert any("killswitch_event" in s for s, _ in kur.zapisy), "brak zapisu kill-switcha"


def test_odniesienie_dzienne_REAGUJE_na_wielkosc_straty():
    """Kanarek do poprzedniego: miara, która zwraca stałą, przechodzi każdy
    pojedynczy test na blokadę. Dwie różne straty muszą dać dwie różne liczby.
    """
    zuzycie = []
    for nav in (0.99, 0.97, 0.95):
        k, _ = kernel([punkt(3, 1.0), punkt(0, nav)])
        v = k.check_account()
        zuzycie.append(round({u["name"]: u["used"] for u in v.usage}["max_daily_loss"], 2))
    assert zuzycie == [1.0, 3.0, 5.0], f"strata dzienna nie reaguje na wejście: {zuzycie}"


def test_swiezy_rachunek_BEZ_straty_nadal_handluje():
    """Kanarek odwrotny. Gdyby poprawka po prostu blokowała świeży rachunek,
    poprzednie testy przechodziłyby razem z silnikiem, który nie handluje."""
    k, kur = kernel([punkt(3, 1.0), punkt(0, 1.01)])
    v = k.check_account()
    assert v.verdict == "allow", f"zyskowny świeży rachunek zatrzymany: {v.reason}"
    assert not [z for z in kur.zapisy if "killswitch_event" in z[0]], (
        "kill-switch zapisany bez powodu")


def test_rachunek_STARSZY_niz_doba_liczy_sie_jak_dotad():
    """Poprawka dotyczy WYŁĄCZNIE braku punktu sprzed doby. Gdy taki punkt
    istnieje, odniesieniem jest on — a nie początek historii, bo inaczej limit
    „straty dziennej" mierzyłby stratę od zawsze."""
    # Szczyt 1.05 trzydzieści godzin temu, doba temu 1.00, teraz 0.99.
    # Od początku historii: -6%. Od doby: -1%. Limit dzienny wynosi 2%.
    k, _ = kernel([punkt(40, 1.05), punkt(30, 1.00), punkt(24, 1.00), punkt(0, 0.99)])
    v = k.check_account()
    uzycie = {u["name"]: u["used"] for u in v.usage}
    assert uzycie["max_daily_loss"] == pytest.approx(1.0, abs=0.01), (
        "strata dzienna liczona od początku historii zamiast od doby")
    assert v.verdict == "allow"


# --------------------------- obalenie hipotezy: brak zysku ≠ brak ochrony

def test_obsuniecie_dziala_na_rachunku_ktory_NIGDY_nie_byl_w_zysku():
    """`wynik_szczyt` to wartość jednostki, a ta startuje od 1.0 — nie od zera.
    Rachunek, który od pierwszego dnia tylko traci, ma szczyt równy 1.0 i pełną
    ochronę obsunięcia. Warunek `state.wynik_szczyt > 0` jest zabezpieczeniem
    przed dzieleniem przez zero, a nie wymogiem historii zysku.

    Strata rozłożona w czasie: dzienna wynosi 1,1% (poniżej limitu 2%), a próg
    kapitału obniżony do 80% wpłat — zatrzymać ma OBSUNIĘCIE, a nie któryś
    z limitów stojących wyżej w kolejności. Bez tego test przechodziłby także
    wtedy, gdyby obsunięcie w ogóle się nie liczyło."""
    luzny_prog = dict(LIMITY, min_equity=dict(LIMITY["min_equity"], value=Decimal("80")))
    k, _ = kernel([punkt(200, 1.0), punkt(30, 0.895), punkt(24, 0.89), punkt(0, 0.88)],
                  luzny_prog)
    v = k.check_account()
    assert v.verdict == "block" and v.trigger_limit == "max_drawdown", (
        f"obsunięcie 12% bez ani jednego zyskownego dnia dało {v.verdict}")


def test_wszystkie_trzy_limity_sa_LICZONE_na_rachunku_bez_zysku():
    """Sedno obalenia. Limit pominięty przez warunek wstępny nie zostawia po
    sobie NICZEGO — ani werdyktu, ani wpisu w `usage`. Ten test sprawdza, że na
    rachunku o zerowym wyniku wszystkie trzy krytyczne limity zostały policzone.

    Gdyby `nav` zmieniło znaczenie na skumulowany P&L (start od 0), dwa z nich
    zniknęłyby z `usage` i nic innego by tego nie zauważyło.
    """
    stan = RiskState(
        equity=Decimal("10000"), peak=Decimal("10000"), day_ago=Decimal("10000"),
        exposure=Decimal("0"), per_instrument={}, orders_last_hour=0,
        nav=Decimal("1"), nav_peak=Decimal("1"), nav_day_ago=Decimal("1"),
        wplaty_netto=Decimal("10000"))
    v = evaluate_account(stan, LIMITY)
    assert v.verdict == "allow"
    assert {u["name"] for u in v.usage} == {"max_daily_loss", "min_equity", "max_drawdown"}, (
        f"nie wszystkie limity krytyczne policzone: {[u['name'] for u in v.usage]}")


# ----------------------------------- to samo odniesienie po stronie WZNOWIENIA

def fetch_one_z_krzywej(punkty):
    """`fetch_one` z services/api/db, oparty na tej samej atrapie co kernel.

    Reguła wznowienia MUSI czytać tę samą liczbę co kernel. Gdyby kernel
    mierzył stratę dzienną od początku historii, a wznowienie od punktu
    bieżącego, powstałaby pętla: kernel zatrzymuje przy 4%, wznowienie widzi
    0,00%, zwalnia — i tak w kółko, każde ogniwo „poprawne" z osobna.
    """
    kur = KursorKrzywej(punkty)

    async def fetch_one(sql, params=None):
        return kur.execute(sql, params).fetchone()

    return fetch_one


def test_wznowienie_NIE_zwalnia_swiezego_rachunku_na_niezmierzonej_zerowce():
    """KANAREK. Dokładnie scenariusz z audytu: 100 000 → 96 000 w trzy godziny.

    `stan_konta` brało punkt bieżący jako odniesienie sprzed doby, więc
    `warunek_ustal` liczył stratę dzienną 0,00% i MELDOWAŁ USTANIE PRZYCZYNY —
    zwalniał kill-switcha na podstawie pomiaru, którego nie było. Kierunek błędu
    jest tu gorszy niż w kernelu: tam limit nie zadziałał, tu zdejmowana jest
    ochrona, która już zadziałała.
    """
    import asyncio

    from services.engine.wznowienie import stan_konta, warunek_ustal

    punkty = [{"wiek_h": 3, "equity": Decimal("100000"), "nav": None, "wplaty": None},
              {"wiek_h": 0, "equity": Decimal("96000"), "nav": None, "wplaty": None}]
    stan = asyncio.run(stan_konta(fetch_one_z_krzywej(punkty), "crypto", "live"))

    assert stan["doba"] == 100_000, (
        f"odniesienie sprzed doby to {stan['doba']}, a nie początek krzywej")
    ustal, dowod = warunek_ustal("max_daily_loss", 2.0, stan)
    assert ustal is False, f"kill-switch zwolniony przy stracie 4%: {dowod}"
    assert "4.00%" in dowod, f"strata dzienna zmierzona jako: {dowod}"


def test_wznowienie_NADAL_zwalnia_gdy_strata_naprawde_ustala():
    """Kanarek odwrotny: poprawka nie może zamienić wznowienia w drzwi
    jednokierunkowe. Rachunek odbudowany do 99 800 (−0,2%) ma wrócić do handlu
    przy histerezie 1% z limitu 2%."""
    import asyncio

    from services.engine.wznowienie import stan_konta, warunek_ustal

    punkty = [{"wiek_h": 3, "equity": Decimal("100000"), "nav": None, "wplaty": None},
              {"wiek_h": 0, "equity": Decimal("99800"), "nav": None, "wplaty": None}]
    stan = asyncio.run(stan_konta(fetch_one_z_krzywej(punkty), "crypto", "live"))
    ustal, dowod = warunek_ustal("max_daily_loss", 2.0, stan)
    assert ustal is True, f"odbudowany rachunek nie wraca do handlu: {dowod}"


def test_kernel_i_wznowienie_MIERZA_TO_SAMO_na_swiezym_rachunku():
    """Sedno: jedna liczba, dwóch czytelników. Test porównuje stratę dzienną
    zmierzoną przez kernel z tą, którą widzi reguła wznowienia — na tym samym
    rachunku. Rozjazd tych dwóch liczb jest pętlą zatrzymań i zwolnień, której
    z zewnątrz nie widać inaczej niż jako „silnik dziwnie się zachowuje"."""
    import asyncio

    from services.engine.wznowienie import stan_konta

    punkty = [{"wiek_h": 3, "equity": Decimal("100000"), "nav": None, "wplaty": None},
              {"wiek_h": 0, "equity": Decimal("96000"), "nav": None, "wplaty": None}]

    k, _ = kernel(punkty, {"max_daily_loss": LIMITY["max_daily_loss"]})
    z_kernela = {u["name"]: u["used"] for u in k.check_account().usage}["max_daily_loss"]

    stan = asyncio.run(stan_konta(fetch_one_z_krzywej(punkty), "crypto", "live"))
    z_wznowienia = (stan["doba"] - stan["wynik"]) / stan["doba"] * 100

    assert z_kernela == pytest.approx(z_wznowienia, abs=0.01), (
        f"kernel mierzy {z_kernela:.2f}%, wznowienie {z_wznowienia:.2f}% "
        f"— ta sama strata, dwie odpowiedzi")
    assert z_kernela == pytest.approx(4.0, abs=0.01)


# ------------------ druga kopia pomiaru: wznowienie na `equity`, kernel na `nav`

def krzywa_z_doplata() -> list[dict]:
    """Rachunek traci 2,5% na jednostce, a właściciel dopłaca 5% kapitału.

    Dwie liczby rozjeżdżają się w PRZECIWNE strony i to jest cała treść tego
    scenariusza: `nav` spada (bo dopłata nie jest zyskiem), `equity` rośnie
    ponad punkt sprzed doby (bo dopłata to pieniądze).

        nav      1,000 → 0,975            strata dzienna 2,50%  ≥ limit 2%
        equity 100 000 → 102 375          strata dzienna −2,38% < histereza
    """
    return [{"wiek_h": 30, "nav": Decimal("1.000"), "wplaty": Decimal("100000"),
             "equity": Decimal("100000")},
            {"wiek_h": 0, "nav": Decimal("0.975"), "wplaty": Decimal("105000"),
             "equity": Decimal("102375")}]


def test_wznowienie_NIE_zdejmuje_hamulca_po_doplacie():
    """KANAREK dla rozjazdu equity↔nav z migracji 0031.

    Kill-switch zadziałał, bo `evaluate_account` zmierzyło stratę dzienną
    z `nav`: 2,5% przy limicie 2%. `stan_konta` czytało wyłącznie
    `exec.equity_point.equity`, a tę dopłata podniosła — strata dzienna
    wychodziła ujemna, poniżej histerezy 1%, więc dozorca zwalniał bezpiecznik
    W DNIU, w którym rachunek naprawdę tracił 2,5%.

    Kierunek błędu jest jednostronny: dopłata może stratę na `equity` tylko
    zamaskować, nigdy powiększyć. Nic się przy tym nie wywracało — w dzienniku
    zostawało zdanie prawdziwe o innej krzywej niż ta, którą mierzy limit.
    """
    import asyncio

    from services.engine.wznowienie import stan_konta, warunek_ustal

    stan = asyncio.run(stan_konta(fetch_one_z_krzywej(krzywa_z_doplata()), "crypto", "live"))
    assert stan["jednostka"] is True, (
        f"wznowienie zeszło na kapitał, choć krzywa `nav` jest pełna — stan: {stan}")

    ustal, dowod = warunek_ustal("max_daily_loss", 2.0, stan)
    assert ustal is False, f"hamulec zdjęty przy stracie 2,5% na jednostce: {dowod}"
    assert "2.50%" in dowod, f"strata dzienna zmierzona jako: {dowod}"


def test_kernel_i_wznowienie_ZGADZAJA_SIE_takze_po_doplacie():
    """Sedno poprawki: jedna liczba, dwóch czytelników — również wtedy, gdy
    wpłaty rozjeżdżają kapitał z wynikiem.

    Test poprzedni pilnuje samego kierunku (nie zwalniaj); ten pilnuje, że obie
    strony podają DOKŁADNIE tę samą wartość. Gdyby kernel zmienił regułę wyboru
    miary, a wznowienie zostało przy swojej, poprzedni test nadal by przechodził
    — bo 2,5% przekracza histerezę przy każdej z nich.
    """
    import asyncio

    from services.engine.wznowienie import stan_konta

    punkty = krzywa_z_doplata()
    k, _ = kernel(punkty, {"max_daily_loss": LIMITY["max_daily_loss"]})
    z_kernela = {u["name"]: u["used"] for u in k.check_account().usage}["max_daily_loss"]

    stan = asyncio.run(stan_konta(fetch_one_z_krzywej(punkty), "crypto", "live"))
    z_wznowienia = (stan["doba"] - stan["wynik"]) / stan["doba"] * 100

    assert z_kernela == pytest.approx(z_wznowienia, abs=0.001), (
        f"kernel mierzy {z_kernela:.3f}%, wznowienie {z_wznowienia:.3f}% "
        f"— ta sama strata, dwie odpowiedzi")
    assert z_kernela == pytest.approx(2.5, abs=0.001)


def test_obsuniecie_we_wznowieniu_tez_idzie_z_jednostki():
    """Ta sama para, drugi limit. Obsunięcie liczone z `equity` widzi szczyt
    podniesiony wpłatą — czyli mierzy przelew, a nie stratę."""
    import asyncio

    from services.engine.wznowienie import stan_konta, warunek_ustal

    # Szczyt jednostki 1,00; teraz 0,94 → obsunięcie 6% przy limicie 10%,
    # histereza wznowienia 5%. Na kapitale: szczyt 100 000, teraz 98 700
    # (po dopłacie 5%) → 1,3%, czyli „ustało" — a nie ustało.
    punkty = [{"wiek_h": 30, "nav": Decimal("1.00"), "wplaty": Decimal("100000"),
               "equity": Decimal("100000")},
              {"wiek_h": 0, "nav": Decimal("0.94"), "wplaty": Decimal("105000"),
               "equity": Decimal("98700")}]
    stan = asyncio.run(stan_konta(fetch_one_z_krzywej(punkty), "crypto", "live"))
    ustal, dowod = warunek_ustal("max_drawdown", 10.0, stan)
    assert ustal is False, f"obsunięcie 6% zameldowane jako ustałe: {dowod}"
    assert "6.00%" in dowod, f"obsunięcie zmierzone jako: {dowod}"


def test_bez_kolumny_nav_wznowienie_wraca_na_kapital():
    """Kanarek odwrotny: baza bez przeliczonej krzywej `nav` nie może zatrzymać
    wznowienia na zawsze. Wtedy — i tylko wtedy — obie strony liczą z kapitału,
    dokładnie jak przed migracją 0031."""
    import asyncio

    from services.engine.wznowienie import stan_konta, warunek_ustal

    punkty = [{"wiek_h": 30, "equity": Decimal("100000"), "nav": None, "wplaty": None},
              {"wiek_h": 0, "equity": Decimal("99800"), "nav": None, "wplaty": None}]
    stan = asyncio.run(stan_konta(fetch_one_z_krzywej(punkty), "crypto", "live"))
    assert stan["jednostka"] is False
    ustal, dowod = warunek_ustal("max_daily_loss", 2.0, stan)
    assert ustal is True, f"rachunek bez krzywej nav nie wraca do handlu: {dowod}"
