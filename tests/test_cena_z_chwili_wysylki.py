"""Cena limitu pochodzi z CHWILI WYSYŁKI, a nie z zamknięcia świecy sprzed 75 minut.

CO DOKŁADNIE BYŁO ZŁE — ZMIERZONE, NIE ZAŁOŻONE

`journal.planned_order.limit_price` powstaje na zamknięciu świecy decyzyjnej
(`loop.run_once`: `price = swieca_dec["close"]`, dalej `zaplanuj`). Zlecenie idzie
na giełdę 71–82 minuty później, bo pętla czeka na komplet cech — ślad w
`journal.pominiecie`:

    bar 2026-09-25 08:00 -> za_malo_cech 12:01, niepelne_sygnaly 12:11,
                            decyzja 13:11:48   (+71 min)
    bar 2026-09-25 20:00 -> decyzja 01:21:16   (+81 min)
    bar 2026-09-26 00:00 -> decyzja 05:21:52   (+82 min)

Egzekucja wysyła `timeInForce: PostOnly`, więc zlecenie przecinające spread MUSI
zostać odrzucone. Wszystkie pięć zleceń w historii produkcji rozkłada się na to
kryterium BEZ WYJĄTKU:

    zlec. instr.   strona limit     bid / ask          limit wobec rynku  status
    54    SOLUSDC  sell   114,55    114,39 / 114,43    +10,5 pb pasywne   filled
    55    SOLUSDC  buy    120,84    120,67 / 120,70    +11,6 pb PRZECINA  rejected
    56    ADAUSDC  buy    0,2585    0,2591 / 0,2594    -34,7 pb pasywne   filled
    57    SOLUSDC  buy    122,14    121,95 / 121,97    +13,9 pb PRZECINA  rejected
    58    SOLUSDC  buy    120,67    120,51 / 120,52    +12,4 pb PRZECINA  rejected

Separacja zupełna, 3/3 kontra 2/2. Nie jest to więc korelacja — to jest reguła
giełdy zastosowana do naszej ceny.

DLACZEGO TEN PLIK ODTWARZA TE PIĘĆ WIERSZY, A NIE WYMYŚLA WŁASNYCH LICZB

Test na cenach okrągłych (limit 100, księga 100) przechodzi także dla kodu,
który cenę bierze z planu — bo przy takich danych obie odpowiedzi są tą samą
liczbą. Asercje niżej mierzą odległość limitu od ASKU i od BIDU, czyli dokładnie
to, o co pyta PostOnly, i robią to na cenach, przy których stara i nowa wersja
odpowiadają RÓŻNIE.

CZEGO TEN PLIK NIE SPRAWDZA

Nie sprawdza, czy decyzja była dobra — od tego jest trener. Sprawdza, że cena,
po której ją wykonujemy, jest ceną z chwili wykonania, i że istnieje granica,
za którą wykonania NIE MA.
"""
import asyncio
import datetime as dt
from decimal import Decimal as D

import pytest

from services.engine.gielda_exec import WIEK_ARKUSZA_MAX_S, zloz_plan
from services.engine.zlecenia import (
    KROTNOSC_ZWLOKI, PROG_ZWLOKI_BEZ_ATR, SUFIT_ZWLOKI, Filtry,
    ocena_zwloki, prog_zwloki)
from services.exchanges.base import (
    BookLevel, ExchangeError, OrderBook, ZlozoneZlecenie)

# Reguly gieldy WZIETE Z PRODUKCJI, nie wymyslone. `basePrecision` dla ADAUSDC
# na podmiocie unijnym to 0,1 (zmierzone 2026-09-07, patrz komentarz przy
# `bybit.reguly_instrumentow`) - globalny host podaje 0,01, wiec pomylka hosta
# wraca komunikatem o NASZYM zaokraglaniu.
SOL = Filtry(krok_ilosci=D("0.001"), min_ilosc=D("0.001"),
             min_wartosc=D("5"), krok_ceny=D("0.01"))
ADA = Filtry(krok_ilosci=D("0.1"), min_ilosc=D("0.1"),
             min_wartosc=D("5"), krok_ceny=D("0.0001"))

# ATR 4h, mediana z 30 dni `feat.feature_value` (cecha `atr_pct__4h`),
# w PROCENTACH ceny. Z tych samych danych, z ktorych policzony jest prog.
ATR_SOL = 1.8514
ATR_ADA = 2.5777

BAR = "2026-09-25T08:00:00+00:00"


class Kursor:
    """Atrapa kursora: zapamietuje SQL, pilnuje liczby parametrow.

    LICZBA PARAMETROW NIE JEST FORMALNOSCIA. `zapisz_pominiecie` wstawia szesc
    wartosci do szesciu miejsc; rozjazd wywraca cala transakcje cyklu na
    produkcji, a w tescie bez tego sprawdzenia wyglada jak sukces.
    """

    def __init__(self, wiersze=None):
        self.sql = []
        self._wiersze = list(wiersze or [])

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        assert jedno.count("%s") == (0 if params is None else len(params)), jedno
        self.sql.append((jedno, params))

    def fetchone(self):
        return self._wiersze.pop(0) if self._wiersze else None

    def fetchall(self):
        w, self._wiersze = self._wiersze, []
        return w

    def zapytania(self, fragment):
        return [(s, p) for s, p in self.sql if fragment in s]


def arkusz(bid, ask, *, wiek_s=0.0):
    """Arkusz z chwili wysylki. `wiek_s` liczony wobec `now()` - tak samo jak
    liczy go `zloz_plan`. Staly znacznik czasu starzalby sie z kazda godzina od
    napisania testu i przy progu 60 s test padalby sam z siebie."""
    teraz = dt.datetime.now(dt.timezone.utc)
    return OrderBook(event_time=teraz - dt.timedelta(seconds=wiek_s),
                     bids=[BookLevel(D(bid), D("100"))],
                     asks=[BookLevel(D(ask), D("100"))])


class Gielda:
    """Adapter-atrapa. `fetch_order_book` ODNOTOWUJE, o ktora ksiege pytano."""

    def __init__(self, *, ksiega=None, blad_ksiegi=None, odniesienie=None,
                 stan=None):
        self._ksiega = ksiega
        self._blad_ksiegi = blad_ksiegi
        self._odniesienie = odniesienie
        self._stan = stan
        self.zlozone = []
        self.pytania_o_arkusz = []

    async def cena_odniesienia(self, symbol, kategoria=None):
        return self._odniesienie

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        self.pytania_o_arkusz.append(dict(
            symbol=symbol, depth=depth, ksiega_egzekucji=ksiega_egzekucji))
        if self._blad_ksiegi:
            raise self._blad_ksiegi
        return self._ksiega

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return self._stan

    async def zloz_zlecenie(self, **kw):
        self.zlozone.append(kw)
        return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy="G1",
                               status="new")

    async def anuluj_zlecenie(self, **kw):
        return True


def plan(*, pid, strona, limit, qty, symbol="SOLUSDC", atr=ATR_SOL,
         rodzaj="wejscie"):
    trigger = dict(rodzaj=rodzaj, bar=BAR)
    if rodzaj == "wyjscie":
        trigger["position_id"] = 32
    return dict(id=pid, decision_id=1000 + pid, instrument_id=3,
                side=strona, qty=D(qty), limit_price=D(limit),
                symbol=symbol, quote_ccy="USDC", base_ccy=symbol[:3],
                atr_pct=atr, trigger=trigger)


def zloz(p, f=SOL, *, gielda):
    cur = Kursor([dict(id=32, qty=D(p["qty"]), avg_price=D("115"))])
    ok, msg = asyncio.run(zloz_plan(cur, gielda, p, f, engine="crypto",
                                    mode="demo", kategoria="spot"))
    return cur, ok, msg


# ============================================ 1. piec zlecen z produkcji

# (numer, strona, limit z planu, bid, ask, reguly, atr, rodzaj, oczekiwany limit)
PRODUKCJA = [
    (54, "sell", "114.55", "114.39", "114.43", SOL, ATR_SOL, "wyjscie", "114.44"),
    (55, "buy",  "120.84", "120.67", "120.70", SOL, ATR_SOL, "wejscie", "120.66"),
    (56, "buy",  "0.2585", "0.2591", "0.2594", ADA, ATR_ADA, "wejscie", "0.2590"),
    (57, "buy",  "122.14", "121.95", "121.97", SOL, ATR_SOL, "wejscie", "121.94"),
    (58, "buy",  "120.67", "120.51", "120.52", SOL, ATR_SOL, "wejscie", "120.50"),
]


@pytest.mark.parametrize("nr,strona,limit,bid,ask,f,atr,rodzaj,oczekiwana",
                         PRODUKCJA)
def test_kazde_zlecenie_z_produkcji_lezy_po_PASYWNEJ_stronie_spreadu(
        nr, strona, limit, bid, ask, f, atr, rodzaj, oczekiwana):
    """SEDNO POPRAWKI. Kupno musi być poniżej ASKU, sprzedaż powyżej BIDU —
    inaczej `PostOnly` odrzuca zlecenie, co na produkcji stało się trzy razy
    na trzy kupna.

    Asercja nie brzmi "cena się zmieniła", bo to przechodziłoby także dla
    zmiany w złą stronę. Brzmi "cena jest po tej stronie księgi, po której
    zlecenie może leżeć" — i dodatkowo sprawdza dokładną liczbę, żeby przy
    okazji nie zgubić marginesu jednego tiku.
    """
    qty = "1996.7" if f is ADA else "8.87"
    g = Gielda(ksiega=arkusz(bid, ask), odniesienie=D(limit))
    _, ok, msg = zloz(plan(pid=nr, strona=strona, limit=limit, qty=qty,
                           symbol="ADAUSDC" if f is ADA else "SOLUSDC",
                           atr=atr, rodzaj=rodzaj), f, gielda=g)

    assert ok is True, msg
    wyslana = g.zlozone[0]["cena_limitu"]
    assert wyslana == D(oczekiwana), (
        f"zlecenie {nr}: wyslano {wyslana}, a z arkusza {bid}/{ask} przy kroku "
        f"{f.krok_ceny} wychodzi {oczekiwana}")
    if strona == "buy":
        assert wyslana < D(ask), (
            f"zlecenie {nr}: kupno {wyslana} nie jest ponizej asku {ask} - "
            f"PostOnly odrzuci je tak samo jak na produkcji")
        assert wyslana <= D(bid)
    else:
        assert wyslana > D(bid)
        assert wyslana >= D(ask)


@pytest.mark.parametrize("nr,strona,limit,bid,ask,f,atr,rodzaj,oczekiwana",
                         PRODUKCJA)
def test_STARA_cena_z_planu_tych_trzech_kupn_PRZECINALA_spread(
        nr, strona, limit, bid, ask, f, atr, rodzaj, oczekiwana):
    """Kontrola danych, nie kodu — i dlatego jest tu osobno.

    Gdyby ceny w tabeli wyżej były przepisane z błędem, cały ten plik mierzyłby
    fikcję, a przechodziłby. Ten test mówi wprost, co te liczby znaczą: trzy
    kupna z ceną PLANU leżały nad askiem (czyli PostOnly musiał je odrzucić),
    a dwa wypełnione zlecenia leżały po stronie pasywnej.
    """
    przecina = (strona == "buy" and D(limit) >= D(ask)) or \
               (strona == "sell" and D(limit) <= D(bid))
    assert przecina is (nr in (55, 57, 58)), (
        f"zlecenie {nr}: dane z produkcji nie zgadzaja sie z zapisanym "
        f"statusem - limit {limit}, arkusz {bid}/{ask}")


def test_ARKUSZ_pytany_jest_o_ksiege_EGZEKUCJI_i_o_jeden_poziom():
    """Host publiczny (api.bybit.com) i host egzekucji (api.bybit.eu) to dwie
    różne księgi: zmierzony rozjazd ich zamknięć to mediana 5,9 pb i p90
    28,4 pb, czyli rząd całej rundy kosztowej. Odczyt z niewłaściwej odpowiada
    na inne pytanie niż zadane — i odpowiada liczbą wyglądającą poprawnie.

    Głębokość 1, bo pytamy o najlepszą ofertę po każdej stronie; głębszy arkusz
    opisywałby poślizg zlecenia rynkowego, a nasze zlecenia rynkowe nie są.
    """
    g = Gielda(ksiega=arkusz("120.67", "120.70"), odniesienie=D("120.84"))
    zloz(plan(pid=55, strona="buy", limit="120.84", qty="8.87"), gielda=g)

    # Dwa odczyty od 2026-10-03: przed wysylka (cena) i zaraz po niej
    # (diagnostyka PostOnly, `gielda_exec.arkusz_po_wysylce`). Oba z ksiegi
    # egzekucji i o jeden poziom.
    assert g.pytania_o_arkusz == [
        dict(symbol="SOLUSDC", depth=1, ksiega_egzekucji=True)] * 2


# ==================================== 2. miara REAGUJE na wejscie

def test_dwa_rozne_arkusze_daja_dwie_rozne_ceny_limitu():
    """Kanarek na sam pomiar: identyczny wynik przy wyraźnie różnym arkuszu
    znaczyłby, że arkusz do ceny nie dochodzi — a to jest dokładnie ten kształt
    wady, którą ta poprawka naprawia. Cena z planu jest w obu przebiegach TA
    SAMA, więc każda różnica w wyniku pochodzi wyłącznie z arkusza.
    """
    def wyslana(bid, ask):
        g = Gielda(ksiega=arkusz(bid, ask), odniesienie=D("120.84"))
        _, ok, msg = zloz(plan(pid=55, strona="buy", limit="120.84",
                               qty="8.87"), gielda=g)
        assert ok is True, msg
        return g.zlozone[0]["cena_limitu"]

    assert wyslana("120.67", "120.70") == D("120.66")
    assert wyslana("119.90", "119.92") == D("119.89")


def test_prog_zwloki_skaluje_sie_ZMIENNOSCIA_PARY():
    """Mediana dryfu 75-minutowego to 0,17% dla BTC i 1,02% dla NEAR —
    sześciokrotnie. Jedna liczba byłaby albo krzykliwa dla BTC, albo
    bezużyteczna dla NEAR; ten sam antywzorzec, co przy paśmie ceny.

    Sprawdzamy pełen wzór, nie samą monotoniczność: monotoniczność przechodzi
    także dla progu policzonego z byle czego.
    """
    assert prog_zwloki(1.1274) < prog_zwloki(ATR_SOL) < prog_zwloki(3.9116)
    assert prog_zwloki(ATR_SOL) == KROTNOSC_ZWLOKI * D(str(ATR_SOL)) / 100


@pytest.mark.parametrize("atr", [None, 0.0, -1.0, float("nan")])
def test_nieznana_zmiennosc_daje_prog_AWARYJNY_a_nie_brak_progu(atr):
    """Brak cechy nie może ani wyłączać progu (awaria danych otwierałaby
    furtkę), ani zatrzymywać handlu. Wartość zepsuta (NaN) ani ujemna nie może
    przy tym wejść do wzoru jako liczba."""
    assert prog_zwloki(atr) == PROG_ZWLOKI_BEZ_ATR
    z = ocena_zwloki(D("120"), D("121"), atr)
    assert z.ze_zmiennosci is False


def test_ogromny_ATR_nie_rozsuwa_progu_bez_konca():
    """Najwyższy próg policzony ze zmierzonych ATR-ów to ARBUSDC 7,8%, więc
    sufit 10% nie wiąże dla żadnej pary z uniwersum. Wiąże dopiero dla ATR
    powyżej 6,67% na świecy 4h — czyli dla wartości, która sama jest
    podejrzana."""
    assert prog_zwloki(5.2229) < SUFIT_ZWLOKI      # ARBUSDC, najwyzszy realny
    assert prog_zwloki(50.0) == SUFIT_ZWLOKI


def test_brak_ceny_z_chwili_wysylki_to_LUKA_a_nie_zgoda():
    """`ocena_zwloki` bez ceny wysyłki zwraca None, a nie "w progu". Zwrócenie
    "w progu" byłoby powiedzeniem nieprawdy o czymś, czego nie sprawdziliśmy —
    i wywołujący nie miałby jak tego odróżnić od pomiaru."""
    assert ocena_zwloki(D("120"), None, ATR_SOL) is None
    assert ocena_zwloki(D("120"), D("0"), ATR_SOL) is None
    assert ocena_zwloki(D("0"), D("120"), ATR_SOL) is None


# ===================================== 3. granica, za ktora NIE wchodzimy

def test_ZMIERZONY_dryf_produkcyjny_NIE_odmawia_wejscia():
    """Próg, który odmawia w przypadku zmierzonym jako normalny, zatrzymuje
    handel — i pierwszy człowiek w pośpiechu go podniesie.

    SOL zszedł w tych 75 minutach o 13–18 centów przy cenie ~120, czyli o
    0,11–0,15%. Próg przy ATR 1,85% to 2,78%, więc zapas jest osiemnastokrotny.
    Ta asercja jest tu po to, żeby zacieśnienie progu do wartości odmawiającej
    normalnego dnia zapaliło zestaw.

    Od 2026-10-03 próg to 0,5×ATR (0,93% przy SOL), więc zapas spadł do
    sześciokrotnego - i to nadal przy dryfie z ZWŁOKI 75 MINUT, której próg
    czasowy (`loop.MAKS_ZWLOKA_WEJSCIA`) już nie przepuści.
    """
    z = ocena_zwloki(D("120.84"), D("120.66"), ATR_SOL)
    assert z.poza is False
    assert z.odchylenie < D("0.002")
    assert z.prog > 5 * z.odchylenie


def test_cena_odjechana_DALEJ_NIZ_PROG_nie_idzie_na_gielde():
    """Wejście po cenie, której decyzja nie popierała, jest zakładem, którego
    nikt nie zatwierdził — a przy kupnie DROŻEJ jest jeszcze wartością pozycji
    większą, niż policzył risk kernel (`qty` zostaje z planu).

    5% przy progu 2,78% dla SOL-a to okolice p99,9 zmierzonego dryfu
    75-minutowego (p99 = 2,34%).
    """
    g = Gielda(ksiega=arkusz("126.88", "126.90"), odniesienie=D("120.84"))
    cur, ok, msg = zloz(plan(pid=55, strona="buy", limit="120.84",
                             qty="8.87"), gielda=g)

    assert ok is False
    assert g.zlozone == []
    assert "odjechala" in msg


def test_odmowa_zostawia_POMINIECIE_z_wlasnym_powodem_i_ze_SWIECA():
    """`exec.order` odpowiada na pytanie, co się stało z tym zleceniem.
    Pytanie, które naprawdę się zadaje, brzmi: czemu dla świecy 2026-09-25 08:00
    nie weszliśmy w SOLUSDC — i na nie odpowiada wyłącznie tabela kluczowana
    (instrument, świeca, powód), po której jedno złączenie zestawia odmowę
    z decyzją, która ją zrodziła (migracja 0041).

    Świeca MUSI pochodzić z `trigger.bar`, a nie z zegara: wiersz opisany chwilą
    odmowy nie złączy się z niczym, bo `decided_for_bar` pyta o świecę.
    """
    g = Gielda(ksiega=arkusz("126.88", "126.90"), odniesienie=D("120.84"))
    cur, ok, _ = zloz(plan(pid=55, strona="buy", limit="120.84", qty="8.87"),
                      gielda=g)

    (_, params), = cur.zapytania("INSERT INTO journal.pominiecie")
    assert params[0] == "crypto" and params[1] == "demo"
    assert params[2] == 3
    assert params[3] == dt.datetime.fromisoformat(BAR)
    assert params[4] == "cena_odjechala"
    assert "0,5×ATR" in params[5]


def test_WYJSCIE_nie_dostaje_zadnego_progu_nawet_przy_wielkim_dryfie():
    """Symetria byłaby tu błędem, i to kosztownym. Odmowa wyjścia zostawia
    pozycję otwartą wbrew decyzji, a risk kernel liczy z niej ekspozycję —
    kosztuje więc pieniądze, a nie okazję. Ten sam argument stoi w nagłówku
    `zlecenia.py` i w `sprawdz_zlecenie`."""
    g = Gielda(ksiega=arkusz("60.00", "60.02"), odniesienie=D("114.55"))
    cur, ok, _ = zloz(plan(pid=54, strona="sell", limit="114.55", qty="12.138",
                           rodzaj="wyjscie"), gielda=g)

    assert ok is True
    assert g.zlozone[0]["cena_limitu"] == D("60.03")
    assert not cur.zapytania("INSERT INTO journal.pominiecie")


# ======================================= 4. wiek odczytu i brak odczytu

def test_ARKUSZ_STARSZY_NIZ_GRANICA_nie_jest_cena_z_chwili_wysylki():
    """Świeża cena z odczytu sprzed godziny to ten sam błąd w nowym miejscu.
    Przy nieczytelnej ostatniej cenie zostaje tylko cena z planu — więc
    wejścia NIE składamy.

    Granica jest luźna świadomie: zmierzony rozjazd znacznika oferty z naszym
    zegarem sięga 13 s (`market.book_snapshot`, 3 dni), a próg ciasny
    zatrzymywałby handel na szumie zegara.
    """
    g = Gielda(ksiega=arkusz("120.67", "120.70", wiek_s=WIEK_ARKUSZA_MAX_S + 5),
               odniesienie=None)
    cur, ok, msg = zloz(plan(pid=55, strona="buy", limit="120.84",
                             qty="8.87"), gielda=g)

    assert ok is False
    assert g.zlozone == []
    assert "brak ceny z chwili wysyłki" in msg
    (_, params), = cur.zapytania("INSERT INTO journal.pominiecie")
    assert params[4] == "brak_ceny_wysylki"


def test_ARKUSZ_MLODSZY_NIZ_GRANICA_przechodzi():
    """Druga granica tego samego progu. Bez tej asercji test wyżej przechodziłby
    także dla kodu odrzucającego KAŻDY arkusz — czyli dla silnika, który nie
    handluje wcale."""
    g = Gielda(ksiega=arkusz("120.67", "120.70", wiek_s=WIEK_ARKUSZA_MAX_S - 5),
               odniesienie=None)
    _, ok, msg = zloz(plan(pid=55, strona="buy", limit="120.84", qty="8.87"),
                      gielda=g)

    assert ok is True, msg
    assert g.zlozone[0]["cena_limitu"] == D("120.66")


def test_PADNIECIE_ARKUSZA_spada_na_ostatnia_cene_TEJ_SAMEJ_ksiegi():
    """Awaria jednego endpointu, która wstrzymuje handel, jest gorszym
    lekarstwem niż choroba — dlatego cena z chwili wysyłki ma dwa źródła. Oba
    są z chwili wysyłki; różnią się dokładnością, nie wiekiem.

    `lastPrice` 120,69 leży między bidem i askiem z produkcji, więc kupno
    wychodzi 120,68 — o dwa tiki wyżej niż z arkusza (120,66), ale nadal
    PONIŻEJ asku 120,70, czyli nadal pasywne.
    """
    g = Gielda(blad_ksiegi=ExchangeError("timeout arkusza"),
               odniesienie=D("120.69"))
    _, ok, msg = zloz(plan(pid=55, strona="buy", limit="120.84", qty="8.87"),
                      gielda=g)

    assert ok is True, msg
    assert g.zlozone[0]["cena_limitu"] == D("120.68")
    assert g.zlozone[0]["cena_limitu"] < D("120.70")


def test_SCIEZKA_ZAPASOWA_TEZ_MA_PROG_a_nie_tylko_arkusz():
    """Luka, którą łatwo zrobić: próg policzony z samego arkusza znikałby
    dokładnie wtedy, gdy arkusz padł — czyli zabezpieczenie wyłączałoby się
    razem z lepszym źródłem, i nic by tego nie pokazało. Dlatego `ocena_zwloki`
    porównuje CENĘ WYSYŁANĄ, bez pytania, skąd pochodzi."""
    g = Gielda(blad_ksiegi=ExchangeError("timeout arkusza"),
               odniesienie=D("126.90"))
    cur, ok, msg = zloz(plan(pid=55, strona="buy", limit="120.84", qty="8.87"),
                        gielda=g)

    assert ok is False
    assert g.zlozone == []
    (_, params), = cur.zapytania("INSERT INTO journal.pominiecie")
    assert params[4] == "cena_odjechala"


# ============================== 5. DECYZJA sie nie zmienia, zmienia sie CENA

def test_podmiana_ceny_NIE_RUSZA_ilosci_ani_dziennika_decyzji():
    """Zmieniamy cenę, po której wchodzimy, a nie to, czy i ile kupujemy.

    `qty` z planu jest liczbą, którą zatwierdził risk kernel; przeliczenie jej
    pod nową cenę byłoby zmianą wielkości pozycji, czyli zmianą decyzji.
    `journal.decision` nie może przy tym dostać ANI JEDNEGO zapisu — egzekucja
    wykonuje decyzję i nie ma prawa jej poprawiać.
    """
    g = Gielda(ksiega=arkusz("120.67", "120.70"), odniesienie=D("120.84"))
    cur, ok, _ = zloz(plan(pid=55, strona="buy", limit="120.84", qty="8.87"),
                      gielda=g)

    assert ok is True
    assert g.zlozone[0]["ilosc"] == D("8.87")
    assert not cur.zapytania("journal.decision")
    # CENA PLANU ZOSTAJE W BAZIE NIETKNIETA: roznica miedzy
    # `journal.planned_order.limit_price` i `exec.order.limit_price` JEST
    # zapisem zdarzenia i na niej stoi `poslizg.py`.
    assert not cur.zapytania("UPDATE journal.planned_order SET limit_price")
    (_, params), = cur.zapytania("INSERT INTO exec.order")
    assert D("120.66") in params, "wiersz zlecenia nie zapisal ceny WYSLANEJ"


# =========================== 5. swiezosc decyzji: prog CZASOWY i 0,5 x ATR (T7)
#
# Prog cenowy 1,5 x ATR byl p98-p99,5 dryfu przy zwloce 75 MINUT - przepuszczal
# 99% wejsc po godzinie (audyt F-4.4). Teraz zwloke ogranicza wprost czas od
# zamkniecia swiecy, a prog cenowy zaciesnia sie do 0,5 x ATR.

def test_KROTNOSC_ZWLOKI_to_POL_ATR():
    assert KROTNOSC_ZWLOKI == D("0.5")
    assert prog_zwloki(ATR_SOL) == D("0.5") * D(str(ATR_SOL)) / 100


def test_dryf_PRZEPUSZCZANY_przy_1_5_ATR_jest_teraz_ODMOWA():
    """Kanarek na wartość: SOL o 1,2% w górę mieścił się w 1,5×ATR (2,78%),
    a nie mieści się w 0,5×ATR (0,93%) - i wejście nie idzie na giełdę."""
    g = Gielda(ksiega=arkusz("122.30", "122.32"), odniesienie=D("122.30"))
    cur, ok, msg = zloz(plan(pid=55, strona="buy", limit="120.84", qty="8.87"),
                        gielda=g)
    assert ok is False, msg
    assert g.zlozone == []
    (_, params), = cur.zapytania("INSERT INTO journal.pominiecie")
    assert params[4] == "cena_odjechala"


def test_prog_CZASOWY_wejscia_to_20_minut_OD_ZAMKNIECIA_swiecy():
    """Granica liczona od KOŃCA świecy (`okno_decyzji`), nie od jej początku:
    świeca 4h 08:00 zamyka się o 12:00, więc 12:20 jest jeszcze w porządku,
    a 12:21 już nie. Przy liczeniu od początku każda decyzja byłaby
    przeterminowana o cztery godziny."""
    from services.engine.horizons import get as get_horizon
    from services.engine.loop import MAKS_ZWLOKA_WEJSCIA, decyzja_przeterminowana

    assert MAKS_ZWLOKA_WEJSCIA == dt.timedelta(minutes=20)
    h4 = get_horizon("4h")
    poczatek = dt.datetime(2026, 9, 25, 8, 0, tzinfo=dt.timezone.utc)
    zamkniecie = poczatek + dt.timedelta(hours=4)
    assert not decyzja_przeterminowana(zamkniecie + dt.timedelta(minutes=5), poczatek, h4)
    assert not decyzja_przeterminowana(zamkniecie + dt.timedelta(minutes=20), poczatek, h4)
    assert decyzja_przeterminowana(zamkniecie + dt.timedelta(minutes=21), poczatek, h4)
    # Zmierzone przed poprawka potoku: decyzje 63-88 min po zamknieciu.
    assert decyzja_przeterminowana(zamkniecie + dt.timedelta(minutes=63), poczatek, h4)

    h1 = get_horizon("1h")
    assert not decyzja_przeterminowana(poczatek + dt.timedelta(minutes=75), poczatek, h1)
    assert decyzja_przeterminowana(poczatek + dt.timedelta(minutes=81), poczatek, h1)


def test_prog_czasowy_wstrzymuje_WEJSCIE_z_wlasnym_powodem_a_po_KILL_SWITCHU():
    """Drzewo składniowe, nie tekst: gałąź z `decyzja_przeterminowana(...)`
    w warunku musi (1) ustawiać hold z powodem `decyzja_przeterminowana`,
    (2) stać PO gałęzi `wstrzymanie_ryzykiem` - próba pożarowa rozpoznaje
    działające ogniwo 3 po tym powodzie - i (3) PRZED `risk.check`, żeby
    przeterminowane wejście nie zużywało limitów ryzyka. Warunek musi też
    dotyczyć wyłącznie wejść (`action.startswith("enter")`)."""
    import ast
    from pathlib import Path

    drzewo = ast.parse(Path("services/engine/loop.py").read_text(encoding="utf-8"))

    def wola(wezel, nazwa):
        return any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == nazwa
                   for n in ast.walk(wezel))

    def powod(blok):
        return [a.value.value for a in blok if isinstance(a, ast.Assign)
                and any(getattr(t, "id", None) == "powod_holdu" for t in a.targets)
                and isinstance(a.value, ast.Constant)]

    # ŁAŃCUCH `if/elif` od gałęzi `wstrzymanie_ryzykiem`. Od 2026-10-18 stoją
    # w nim także `swieca_przed_sesja` (przed progiem czasowym) i
    # `ponizej_jednej_akcji` (za nim) - test pilnuje KOLEJNOŚCI, nie sąsiedztwa.
    def lancuch(n):
        out = [n]
        while len(n.orelse) == 1 and isinstance(n.orelse[0], ast.If):
            n = n.orelse[0]
            out.append(n)
        return out

    poczatki = [n for n in ast.walk(drzewo) if isinstance(n, ast.If)
                and powod(n.body) == ["wstrzymanie_ryzykiem"]
                and any(wola(g.test, "decyzja_przeterminowana") for g in lancuch(n))]
    assert len(poczatki) == 1, "brak gałęzi progu czasowego w łańcuchu wyboru"
    galezie = lancuch(poczatki[0])
    (i_czas,) = [i for i, g in enumerate(galezie)
                 if wola(g.test, "decyzja_przeterminowana")]
    galaz = galezie[i_czas]
    assert i_czas > 0, "próg czasowy przed `wstrzymanie_ryzykiem`"
    assert powod(galaz.body) == ["decyzja_przeterminowana"]
    assert "startswith" in ast.unparse(galaz.test) and "enter" in ast.unparse(galaz.test)
    (i_risk,) = [i for i, g in enumerate(galezie)
                 if any("risk.check" in ast.unparse(w) for w in g.body)]
    assert i_risk > i_czas, "przeterminowane wejście zużywa limity ryzyka"

