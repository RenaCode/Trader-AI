"""Progi uzgodnienia: pył po zamknięciu, stawka giełdy, ilość ujemna.

DLACZEGO TEN PLIK ISTNIEJE OSOBNO

Uzgodnienie jest jedynym miejscem, które porównuje nasz obraz stanu z obrazem
giełdy — i jedynym, które wysyła `killswitch.triggered` o severity `critical`.
Kanał powiadomień ma własny limit tempa, więc uzgodnienie alarmujące bez
powodu nie jest hałasem: jest ZAGŁUSZENIEM prawdziwego kill-switcha
i `engine.down`. Fałszywy alarm kosztuje tu tyle samo co brak alarmu.

Liczby są Z PRODUKCJI (`ref.instrument`, stan na 2026-09-07), a nie wymyślone.
Próg policzony z wymyślonych reguł mierzyłby własną atrapę.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from services.engine.koszty import koszty
from services.engine.uzgodnienie import (
    pozycje_z_wypelnien,
    progi_bezwzgledne,
    rozjazdy,
    tolerancja_wzgledna,
    uzgodnij,
)
from services.exchanges.base import Balance, ExchangeError, Wypelnienie

# aktywo -> (lot_size, min_notional, ostatnia cena) — z produkcji.
REGULY = {
    "LINK": (D("0.01"), D("5"), D("12.02")),
    "ADA": (D("0.1"), D("5"), D("0.74")),
    "SOL": (D("0.001"), D("5"), D("116.06")),
}

# aktywo -> (ilosc z bazy, pyl zostajacy po sprzedazy zaokraglonej w dol).
# Pyl = ilosc mod lot_size, czyli reszta z dzielenia przez krok instrumentu —
# `zlecenia.do_kroku_w_dol` zaokragla sprzedaz W DOL i inaczej byc nie moze.
PYL_Z_PRODUKCJI = {
    "LINK": (D("94.605844"), D("0.005844")),
    "ADA": (D("1357.067404"), D("0.067404")),
    "SOL": (D("5.551517"), D("0.000517")),
}


def _progi(aktywa=("LINK", "ADA", "SOL")) -> dict[str, D]:
    """Progi policzone tak, jak policzy je `progi_bezwzgledne` z bazy."""
    return asyncio.run(progi_bezwzgledne(_fetch(aktywa), "crypto"))


def _fetch(aktywa, pozycje=(), od=None, cena_swieza=True):
    async def fetch_all(sql, params=None):
        if "sum(p.qty)" in sql:
            return list(pozycje)
        if "min(i.lot_size)" in sql:
            return [{"aktywo": a, "lot": REGULY[a][0],
                     "min_wartosc": REGULY[a][1],
                     # Brak swiezej ceny to w SQL LEFT JOIN bez trafienia,
                     # czyli NULL — a nie zero i nie cena stara.
                     "cena": REGULY[a][2] if cena_swieza else None}
                    for a in aktywa]
        if "DISTINCT base_ccy" in sql:
            return [{"base_ccy": a} for a in aktywa]
        if "min(p.opened_at)" in sql:
            return [{"od": od}]
        if 'app."user"' in sql:
            return [{"id": 1}]
        return [{"symbol": f"{a}USDC", "base_ccy": a} for a in aktywa]
    return fetch_all


class _Salda:
    def __init__(self, salda):
        self._s = salda

    async def fetch_balances(self):
        return [Balance(currency=k, total=v, available=v)
                for k, v in self._s.items()]


class _BezSald:
    """Host, który na każdą ścieżkę do salda odpowiada 404 — jak demo bybit.eu."""

    def __init__(self, wyp):
        self._wyp = wyp

    async def fetch_balances(self):
        raise ExchangeError("endpoint niedostepny na tym hoscie", code="404")

    async def wypelnienia(self, **kw):
        return self._wyp


def _wypelnienie(qty, strona, symbol, czas=None, prowizja="0", prowizja_w=None):
    return Wypelnienie(
        id_gieldy="x", id_wlasne=None, symbol=symbol, strona=strona,
        ilosc=D(qty), cena=D("12.02"), prowizja=D(prowizja),
        prowizja_w=prowizja_w,
        czas=czas or datetime(2026, 9, 1, tzinfo=timezone.utc))


# ----------------------------------------------------------- prog bezwzgledny

@pytest.mark.parametrize("aktywo", ["LINK", "ADA", "SOL"])
def test_pyl_po_zamknieciu_pozycji_NIE_JEST_rozjazdem(aktywo):
    """Resztka poniżej kroku instrumentu jest TRWAŁA i nie jest pozycją.

    Sprzedaż zaokrągla ilość W DÓŁ do `lot_size` (`zlecenia.do_kroku_w_dol`,
    i w drugą stronę być nie może — w górę łamałoby limit risk kernela po
    cichu). Po wyjściu zostaje więc reszta z dzielenia przez krok. Sprzedać
    jej się NIE DA: jest poniżej kroku i poniżej `min_notional`.

    Sama różnica względna dawała tu 1,0 — bo przy `u_nas = 0` mianownikiem
    jest sam pył. Siedem centów wyglądało dokładnie tak samo jak zniknięcie
    całej pozycji, co dziesięć minut, bezterminowo.
    """
    _, pyl = PYL_Z_PRODUKCJI[aktywo]
    r = rozjazdy({}, {aktywo: pyl}, progi=_progi())
    assert r == [], (
        f"pył {pyl} {aktywo} zgłoszony jako {r[0]['rodzaj']!r} "
        f"z względną {r[0]['wzgledna']}")


@pytest.mark.parametrize("aktywo", ["LINK", "ADA", "SOL"])
def test_CALA_pozycja_ktorej_gielda_nie_ma_NADAL_jest_rozjazdem(aktywo):
    """Kanarek do poprzedniego testu.

    Próg dostatecznie duży wycisza wszystko — łącznie z przypadkiem, dla
    którego uzgodnienie istnieje. Bez tego testu „pył nie alarmuje" przechodzi
    także dla progu nieskończonego.
    """
    ilosc, _ = PYL_Z_PRODUKCJI[aktywo]
    r = rozjazdy({aktywo: ilosc}, {}, progi=_progi())
    assert len(r) == 1 and r[0]["rodzaj"] == "pozycja-widmo", (
        f"{ilosc} {aktywo} zniknięte z giełdy nie zostało zgłoszone: {r}")


def test_prog_bierze_WIEKSZY_z_kroku_i_minimalnej_wartosci():
    """Resztka jest nieusuwalna z dwóch niezależnych powodów, więc próg musi
    pokryć oba — inaczej jeden z nich zostaje jako źródło alarmów.

    ADA: krok 0,1 to osiem centów, a `min_notional` to 5 USDC, czyli ~6,76 ADA.
    Resztka 3 ADA jest WYSOKO ponad krokiem i mimo to nie da się jej sprzedać.
    """
    progi = _progi()
    assert progi["ADA"] == D("5") / D("0.74")
    assert progi["ADA"] > REGULY["ADA"][0]
    assert rozjazdy({}, {"ADA": D("3")}, progi=progi) == []
    # A tuz powyzej progu juz alarmuje — inaczej to nie jest prog, tylko cisza.
    assert len(rozjazdy({}, {"ADA": D("7")}, progi=progi)) == 1


def test_kierunek_KAZDEGO_agregatu_daje_prog_MNIEJSZY():
    """Jedno aktywo bazowe ma kilka par i to zapytanie je ŁĄCZY.

    Docstring `progi_bezwzgledne` mówi: „Bierzemy NAJMNIEJSZY krok i NAJWYŻSZĄ
    cenę, czyli w obu członach wariant dający próg MNIEJSZY". Kod robił
    `min(i.lot_size)` i `max(c.close)` zgodnie z tym zdaniem, ale
    `max(i.min_notional)` PRZECIWNIE: drugi człon progu brzmi
    `min_wartosc / cena`, więc najwyższe minimum wartości daje próg NAJWIĘKSZY.
    Sprzeczność siedziała wewnątrz jednej funkcji, dwie linie pod akapitem
    ostrzegającym, że „błąd w stronę progu za dużego jest tu najgorszy
    z możliwych: wycisza rozjazd, który był prawdziwy".

    Nie było to teoretyczne: migracja 0023 zostawiła pary USDT jako `active`,
    zdejmując im tylko `tradeable`, więc `GROUP BY i.base_ccy` naprawdę łączy
    dwie pary tego samego aktywu o różnych minimach wartości.

    SPRAWDZENIE NA ŹRÓDLE, bo agregat liczy Postgres, a nie Python. Atrapa
    w tym pliku oddaje wiersze JUŻ zagregowane — podmiana `min` na `max`
    niczego by w niej nie zmieniła, więc test na zachowaniu mierzyłby wyłącznie
    samą atrapę. To jest sprawdzenie słabsze niż reszta pliku i jest tak nazwane
    (ten sam kompromis co przy wieku świecy niżej).
    """
    import inspect
    import re

    sql = " ".join(inspect.getsource(progi_bezwzgledne).split())

    # Kolumna -> agregat, ktory daje prog MNIEJSZY. Mapa, a nie jeden `assert
    # "min(i.min_notional)"`, bo pytanie brzmi „w ktora strone myli sie kazdy
    # z trzech czlonow", a nie „czy ktos poprawil ten jeden napis".
    kierunek = {"i.lot_size": "min",      # krok: mniejszy krok, mniejszy prog
                "i.min_notional": "min",  # prog to min_wartosc/cena -> mniejsze
                "c.close": "max"}         # prog to min_wartosc/cena -> wieksza
    for kolumna, oczekiwany in kierunek.items():
        m = re.search(rf"(\w+)\(\s*{re.escape(kolumna)}\s*\)", sql)
        assert m, f"zapytanie o próg nie agreguje {kolumna}"
        assert m.group(1).lower() == oczekiwany, (
            f"{m.group(1)}({kolumna}) daje próg WIĘKSZY niż wariant "
            f"{oczekiwany}({kolumna}) — a próg za duży wycisza rozjazd, "
            f"który był prawdziwy")


def test_prog_dla_aktywa_o_DWOCH_parach_bierze_TANSZA_pare():
    """Konsekwencja poprzedniego, policzona na liczbach.

    SOL stoi na dwóch parach: SOLUSDT o minimum wartości 1 USD i SOLUSDC
    o minimum 5 USD. Resztka warta 3 USD jest do sprzedania na pierwszej
    z nich, czyli JEST prawdziwą pozycją — a przy progu z `max` (5 USD)
    uzgodnienie milczało o rozjeździe, który da się zamknąć.
    """
    progi_tansza = asyncio.run(progi_bezwzgledne(
        _fetch_wiersze([{"aktywo": "SOL", "lot": D("0.001"),
                         "min_wartosc": D("1"), "cena": D("100")}]), "crypto"))
    progi_drozsza = asyncio.run(progi_bezwzgledne(
        _fetch_wiersze([{"aktywo": "SOL", "lot": D("0.001"),
                         "min_wartosc": D("5"), "cena": D("100")}]), "crypto"))

    rozjazd = {"SOL": D("0.03")}      # 3 USD po cenie 100
    assert len(rozjazdy({}, rozjazd, progi=progi_tansza)) == 1, (
        "3 USD sprzedawalne na parze o minimum 1 USD to prawdziwy rozjazd")
    assert rozjazdy({}, rozjazd, progi=progi_drozsza) == [], (
        "kanarek: przy progu z DROŻSZEJ pary ten sam rozjazd milczy — i o to "
        "dokładnie chodziło w `max(i.min_notional)`")


def _fetch_wiersze(wiersze):
    """Atrapa oddająca podane wiersze na zapytanie o próg. Bez agregacji —
    agregat robi Postgres, więc kierunku `min`/`max` pilnuje test wyżej."""
    async def fetch_all(sql, params=None):
        assert "min(i.lot_size)" in sql, f"nieoczekiwane zapytanie: {sql}"
        return wiersze
    return fetch_all


def test_brak_SWIEZEJ_ceny_zostawia_sam_krok_a_nie_prog_z_powietrza():
    """Cena sprzed trzech dni przeliczona na ilość daje próg oderwany od rynku,
    a błąd w stronę progu ZA DUŻEGO wycisza rozjazd, który był prawdziwy.

    Zapytanie ogranicza świecę do ostatniej doby, więc brak trafienia daje NULL.
    Wtedy człon z `min_notional` po prostu odpada — zostaje sam `lot_size`,
    czyli mniej, ale nadal poprawnie."""
    progi = asyncio.run(progi_bezwzgledne(
        _fetch(("LINK",), cena_swieza=False), "crypto"))
    assert progi["LINK"] == REGULY["LINK"][0]

    # SPRAWDZENIE NA ZRODLE, bo bez bazy nie da sie inaczej. Atrapa oddaje
    # NULL, bo tak KAZE jej zapytanie — gdyby zapytanie przestalo ograniczac
    # wiek swiecy, atrapa dalej oddawalaby NULL i test wyzej przechodzilby,
    # mierzac wylacznie sam siebie. To jest slabsze sprawdzenie niz reszta
    # tego pliku i jest tak nazwane.
    import inspect
    sql = inspect.getsource(progi_bezwzgledne)
    assert "event_time > now() - interval" in sql, (
        "zapytanie o próg nie ogranicza wieku świecy — cena sprzed dowolnie "
        "długiego czasu dałaby próg oderwany od rynku, i to w stronę, która "
        "wycisza rozjazd prawdziwy")


def test_prog_mierzy_ROZNICE_a_nie_wielkosc_pozycji():
    """Reczne zamkniecie calej pozycji w panelu daje różnicę OGROMNĄ przy
    aktywie, którego próg jest niewielki. Gdyby próg porównywał się z saldem
    zamiast z różnicą, znikniecie 94 LINK przeszłoby jako 'poniżej progu'."""
    r = rozjazdy({"LINK": D("94.605844")}, {"LINK": D("0.005844")},
                 progi=_progi())
    assert len(r) == 1 and r[0]["rodzaj"] == "pozycja-widmo"


def test_prog_bezwzgledny_pochodzi_Z_BAZY_a_nie_ze_stalej():
    """Reguły instrumentu zmienia giełda, i to per instrument. Stała w kodzie
    byłaby trzecim miejscem, w którym trzyma się cudze reguły — i pierwszym,
    które by się rozjechało. Test pilnuje, że wartość naprawdę idzie
    z odczytanego wiersza, a nie z liczby wpisanej obok."""
    inne = asyncio.run(progi_bezwzgledne(_fetch(("SOL",)), "crypto"))
    assert inne["SOL"] == D("5") / D("116.06")
    assert "LINK" not in inne, "próg wzięty skądinąd niż z oddanych wierszy"


# --------------------------------------------------------- tolerancja z cennika

def test_tolerancja_pokrywa_TAKERA_zmierzonego_u_gieldy():
    """Podmiot unijny: maker 0,100%, taker 0,250% (odczytane z konta przez
    /v5/account/fee-rate). Dawna stała 0,001 leżała DOKŁADNIE na stawce makera
    i była 2,5x mniejsza od takera — czyli asymetria w księgowaniu prowizji
    (brak `feeCurrency` w /v5/order/*, obecny w /v5/execution/list) alarmowała
    przy każdym kupnie zleceniem rynkowym."""
    taker = D(str(koszty("spot_eu").taker))
    assert taker == D("0.0025")
    assert tolerancja_wzgledna("spot_eu") > taker, (
        "tolerancja bez marginesu nad stawką stoi na granicy — a giełda "
        "zaokrągla prowizję do precyzji aktywa, więc bywa odrobinę powyżej")

    brutto = D("94.842")
    netto_taker = brutto * (1 - taker)
    assert rozjazdy({"LINK": brutto}, {"LINK": netto_taker},
                    tolerancja_wzgledna("spot_eu"), _progi()) == []


def test_tolerancja_ZMIENIA_SIE_z_wierszem_cennika():
    """Kanarek do poprzedniego: gdyby wartość była zaszyta, oba rodzaje dałyby
    tę samą liczbę i test wyżej przechodziłby przez przypadek.

    To ten sam argument, który każe podać `--kind spot_eu` przy
    `mozliwosci.py --stawki`: próg policzony z wiersza cennika, którego nikt
    nie używa, opisuje inny rachunek niż uzgadniany."""
    assert tolerancja_wzgledna("spot_eu") > tolerancja_wzgledna("spot")
    assert tolerancja_wzgledna("perp") < tolerancja_wzgledna("spot")


def test_tolerancja_nadal_lapie_roznice_WIEKSZA_niz_prowizja():
    """Tolerancja za duża przepuszcza realne rozjazdy, więc test pilnuje obu
    stron. Ubytek 1% na pozycji to nie jest prowizja — to brakujące wyjście."""
    r = rozjazdy({"LINK": D("94.842")}, {"LINK": D("93.89")},
                 tolerancja_wzgledna("spot_eu"), _progi())
    assert len(r) == 1 and r[0]["rodzaj"] == "pozycja-widmo"


# ------------------------------------------------------------- ilosc ujemna

def test_ujemna_ilosc_to_ILOSC_UJEMNA_a_nie_pozycja_widmo():
    """Na spocie nie da się mieć -0,5 BTC, więc ujemna wartość nie jest stanem
    rachunku, tylko BŁĘDEM POMIARU: okno rejestru urwało kupno i zostawiło
    sprzedaż.

    Nazwanie tego 'pozycją-widmo' wysyła człowieka w złą stronę — kazałoby
    szukać aktywa, którego giełda nie ma, podczas gdy naprawdę brakuje POŁOWY
    REJESTRU, czyli problem jest w oknie retencji, a nie na rachunku."""
    r = rozjazdy({}, {"BTC": D("-0.5")}, progi=_progi())
    assert len(r) == 1
    assert r[0]["rodzaj"] == "ilosc-ujemna", (
        f"ujemna ilość nazwana {r[0]['rodzaj']!r} — to komunikat o innym "
        f"problemie niż ten, który zaszedł")


def test_prog_bezwzgledny_NIE_WYCISZA_ilosci_ujemnej():
    """Ujemna resztka mieści się poniżej każdego progu, a mimo to jest błędem.
    Progi mierzą WIELKOŚĆ różnicy; tutaj zła jest sama liczba, więc sprawdzenie
    znaku musi iść PRZED nimi."""
    r = rozjazdy({}, {"LINK": D("-0.001")}, progi=_progi())
    assert len(r) == 1 and r[0]["rodzaj"] == "ilosc-ujemna"


def test_urwane_okno_rejestru_dochodzi_do_rozjazdow_jako_blad():
    """Pełna ścieżka, nie sama funkcja czysta.

    `pozycje_z_wypelnien` musi PRZEPUŚCIĆ wartość ujemną — odfiltrowanie jej
    (`v > 0` zamiast `v != 0`) zamieniłoby błąd pomiaru w cichy brak wpisu,
    czyli w zdanie 'aktywa nie mamy': prawdziwie brzmiące i fałszywe.
    `pokrycie_wypelnien` tego przypadku nie łapie, bo przy pustym dzienniku
    (`najstarsze_otwarcie is None`) zwraca None i straży nie ma żadnej."""
    tylko_sprzedaz = [_wypelnienie("0.5", "Sell", "LINKUSDC")]
    stan = pozycje_z_wypelnien(tylko_sprzedaz, {"LINKUSDC": "LINK"})
    assert stan == {"LINK": D("-0.5")}, (
        "ujemna wartość odfiltrowana — błąd pomiaru zamieniony w ciszę")

    zgloszone = []

    async def notify(uid, kod, tytul, tresc, **kw):
        zgloszone.append((kod, tresc, kw.get("data")))

    n = asyncio.run(uzgodnij(_fetch(("LINK",)), notify,
                             _BezSald(tylko_sprzedaz), kind="spot_eu"))
    assert n == 1
    assert zgloszone, "błąd pomiaru nie trafił do powiadomienia"
    _, tresc, dane = zgloszone[0]
    assert dane["rozjazdy"][0]["rodzaj"] == "ilosc-ujemna"
    assert "BŁĄD POMIARU" in tresc and "kupna" in tresc, (
        f"komunikat nie mówi, czego naprawdę brakuje: {tresc!r}")
    assert "pozycja-widmo blokuje" not in tresc, (
        "komunikat o pozycji-widmie przy błędzie pomiaru wysyła człowieka "
        "szukać aktywa zamiast brakującej połowy rejestru")


# ---------------------------------------------------------- pelna sciezka

def test_pyl_na_calej_sciezce_nie_wysyla_powiadomienia():
    """To jest ten stan, który miał dawać 144 krytyczne powiadomienia dziennie:
    `killswitch.triggered` ma `default_cooldown_s = 0` i `always_on = true`,
    a uzgodnienie chodzi co 10 minut."""
    async def notify(*a, **k):
        raise AssertionError("pył wysłał powiadomienie krytyczne")

    n = asyncio.run(uzgodnij(
        _fetch(("LINK", "ADA", "SOL")), notify,
        _Salda({a: PYL_Z_PRODUKCJI[a][1] for a in ("LINK", "ADA", "SOL")}),
        kind="spot_eu"))
    assert n == 0


def test_prawdziwy_rozjazd_na_calej_sciezce_NADAL_wysyla():
    """Kanarek do poprzedniego. Bez niego 'pył nie wysyła' przechodziłoby
    także dla uzgodnienia, które nie wysyła nigdy niczego."""
    zgloszone = []

    async def notify(uid, kod, *a, **kw):
        zgloszone.append(kod)

    n = asyncio.run(uzgodnij(
        _fetch(("LINK",)), notify, _Salda({"LINK": D("94.605844")}),
        kind="spot_eu"))
    assert n == 1
    assert zgloszone == ["killswitch.triggered"]
