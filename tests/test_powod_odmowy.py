"""Zlecenie odrzucone ma ZAPISAĆ POWÓD — surowy od giełdy obok naszego.

ZMIERZONE NA PRODUKCJI 2026-09-26 (tor crypto, ostatnie 24 h: 3 odrzucone,
1 wypełnione). Wszystkie trzy odrzucenia w CAŁEJ historii produkcji —
`exec.order` 55, 57, 58, każde SOLUSDC buy — mają `reject_reason` NULL, a `raw`
mówi tylko tyle:

    {"rodzaj": "brak", "status_gieldy": "rejected",
     "venue_order_id": "2311949943159816448", "ulamek_wypelnienia": "0", ...}

Czyli: giełda PRZYJĘŁA zlecenie (nadała `venue_order_id`), potem je odrzuciła,
a my zapisaliśmy trzy identyczne wiersze bez jednego słowa o przyczynie. Trzy
czwarte wysłanych zleceń nie wchodzi, a w bazie nie ma nic, z czego dałoby się
odpowiedzieć „dlaczego".

INFORMACJA BYŁA W RĘKU. Bybit oddaje `rejectReason` w tym samym wierszu
odpowiedzi `/v5/order/realtime` i `/v5/order/history`, z którego
`stan_zlecenia` czyta `orderStatus`, `qty` i `cumExecQty`. Adapter czytał
sąsiednie pola i to jedno pomijał, więc poprawka jest ODCZYTEM, nie nowym
zapytaniem: `rejectReason` nie miał w tym pliku ani jednego trafienia.

DLACZEGO SUROWA WARTOŚĆ MUSI ZOSTAĆ OBOK NASZEJ. Nasze mapowanie może się
pomylić albo giełda może dodać nową wartość. Wtedy jedynym punktem zaczepienia
jest to, co giełda POWIEDZIAŁA — dlatego `raw.powod_odmowy_gieldy` trzyma kod
dosłownie, a `raw.powod_odmowy_rodzaj` naszą kategorię, i żadne nie zastępuje
drugiego. Kod nierozpoznany zapisujemy DOSŁOWNIE z adnotacją: da się go potem
zdiagnozować. Kod podstawiony pod najbliższą znaną kategorię wygląda jak
diagnoza i kończy śledztwo w złym miejscu.
"""
import asyncio
import datetime as dt
import json
from decimal import Decimal as D

import httpx

from services.engine.gielda_exec import rozstrzygnij_plan
from services.engine.zlecenia import Filtry
from services.exchanges.base import PowodOdmowy, StanZlecenia
from services.exchanges.bybit import POWODY_ODMOWY, BybitAdapter

# ------------------------------------------------------------------ atrapy
#
# Kursor i giełda są podstawione; REGUŁA, której test pilnuje, jest w kodzie
# produkcyjnym. Atrapa oddaje wyłącznie to, co giełda naprawdę odpowiada,
# i zapamiętuje to, co kod do bazy zapisał.

F = Filtry(krok_ilosci=D("0.01"), min_ilosc=D("0.01"),
           min_wartosc=D("5"), krok_ceny=D("0.01"))

PLAN = dict(id=43, decision_id=1743, instrument_id=14087, side="buy",
            qty=D("8.879"), limit_price=D("120.84"), symbol="SOLUSDC",
            quote_ccy="USDC", trigger=dict(rodzaj="wejscie", bar="x"))

TERAZ = dt.datetime(2026, 9, 25, 13, 16, tzinfo=dt.timezone.utc)


class Kursor:
    """Atrapa kursora: zapamiętuje (SQL, parametry) i oddaje zadane wiersze."""

    def __init__(self, wiersze=None):
        self.sql = []
        self._wiersze = list(wiersze or [])

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        oczekiwane = jedno.count("%s")
        podane = 0 if params is None else len(params)
        assert oczekiwane == podane, (
            "zapytanie ma %d miejsc na parametry, a dostało %d: %s"
            % (oczekiwane, podane, jedno))
        self.sql.append((jedno, params))

    def fetchone(self):
        return self._wiersze.pop(0) if self._wiersze else None

    def fetchall(self):
        w, self._wiersze = self._wiersze, []
        return w

    def zapytania(self, fragment):
        return [(s, p) for s, p in self.sql if fragment in s]

    def powody(self):
        """Parametry zapisów, które dotykają `reject_reason`."""
        return [p for s, p in self.sql
                if "UPDATE exec.order" in s and "reject_reason" in s]

    def surowe(self):
        """Słowniki dopisane do kolumny `raw`."""
        return [json.loads(p[-2]) for s, p in self.sql
                if "UPDATE exec.order" in s and "raw = raw ||" in s]


class Gielda:
    """Adapter oddający jeden zadany `StanZlecenia`."""

    def __init__(self, stan):
        self._stan = stan
        self.anulowane = []

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return self._stan

    async def anuluj_zlecenie(self, **kw):
        self.anulowane.append(kw)
        return True

    async def wypelnienia(self, **kw):
        return []


def gielda_bybita(handler):
    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k


def odrzucone_przez_gielde(powod, status="Rejected"):
    """Wiersz zlecenia w kształcie, w jakim oddaje go Bybit V5.

    `rejectReason` przychodzi w TEJ SAMEJ odpowiedzi co `orderStatus` — i to
    jest cały dowód na to, że poprawka nie potrzebuje nowego zapytania.
    `powod=None` oznacza odpowiedź BEZ tego pola (tak zachowuje się Alpaca
    i starsze zapisy), a nie „brak odmowy".
    """
    wiersz = dict(orderLinkId="tai-p43", orderId="2311949943159816448",
                  orderStatus=status, qty="8.879", cumExecQty="0",
                  avgPrice="", cumExecFee="0")
    if powod is not None:
        wiersz["rejectReason"] = powod
    return wiersz


def stan_z_gieldy(rejectReason, status="Rejected", endpoint="realtime"):
    """Pełna droga: odpowiedź HTTP giełdy przez adapter do `StanZlecenia`."""
    def handler(req):
        pasuje = ("/v5/order/" + endpoint) in req.url.path
        lista = [odrzucone_przez_gielde(rejectReason, status)] if pasuje else []
        return httpx.Response(200, json=dict(retCode=0, result=dict(list=lista)))

    return asyncio.run(gielda_bybita(handler).stan_zlecenia("tai-p43"))


def stan_odrzucone(powod):
    """`StanZlecenia` odrzuconego zlecenia — kształt zleceń 55, 57 i 58."""
    return StanZlecenia(id_wlasne="tai-p43", id_gieldy="2311949943159816448",
                        status="rejected", ilosc=D("8.879"), wypelniono=D("0"),
                        srednia_cena=None, prowizja=D("0"),
                        powod_odmowy=powod)


def rozstrzygnij(stan):
    cur = Kursor([dict(id=55, qty=D("8.879"), submitted_at=TERAZ)])
    rodzaj, msg = asyncio.run(rozstrzygnij_plan(
        cur, Gielda(stan), PLAN, F, engine="crypto", mode="demo",
        kategoria="spot", teraz=TERAZ, kind="spot_eu"))
    return cur, rodzaj, msg


# ---------------------------------------------- 1. adapter CZYTA rejectReason

def test_adapter_czyta_rejectReason_z_odpowiedzi_o_stan_zlecenia():
    """Pole leży w odpowiedzi, którą już mamy w ręku — i musi z niej wyjść.

    KANAREK CAŁEJ ZMIANY. Przed poprawką `stan_zlecenia` budował `StanZlecenia`
    z `orderStatus`, `qty`, `cumExecQty`, `avgPrice`, `cumExecFee`
    i `feeCurrency`, a `rejectReason` z tego samego słownika pomijał. Wiersz
    zlecenia dostawał wtedy `reject_reason = NULL` i trzy odrzucenia na
    produkcji były nie do odróżnienia od siebie.
    """
    stan = stan_z_gieldy("EC_PostOnlyWillTakeLiquidity")

    assert stan.status == "rejected"
    assert stan.powod_odmowy is not None, (
        "giełda podała `rejectReason` w tej samej odpowiedzi, z której czytamy "
        "status — adapter go wyrzucił, więc `exec.order.reject_reason` nie ma "
        "skąd wziąć wartości")
    assert stan.powod_odmowy.kod == "EC_PostOnlyWillTakeLiquidity"
    assert stan.powod_odmowy.rodzaj == "postonly"
    assert not stan.powod_odmowy.nierozpoznany


def test_powod_odmowy_takze_z_HISTORII_zlecen():
    """Odrzucone zlecenie znika z arkusza — powód jest w historii.

    `stan_zlecenia` pyta oba endpointy właśnie dlatego. Gdyby odczyt powodu
    wisiał tylko na pierwszym, na produkcji nie zadziałałby ani razu:
    rozstrzygnięcie przychodzi cykl po złożeniu, a zlecenie `rejected` nie leży
    już wśród otwartych.
    """
    stan = stan_z_gieldy("EC_PostOnlyWillTakeLiquidity", endpoint="history")
    assert stan.powod_odmowy.kod == "EC_PostOnlyWillTakeLiquidity"


# ------------------------------------- 2. nieznanego kodu NIE PODSTAWIAMY

def test_nieznany_kod_zapisany_DOSLOWNIE_a_nie_pod_najblizsza_kategorie():
    """Wartość, której nie znamy, zostaje wartością — nie zgadywaną kategorią.

    Podstawienie najbliższej znanej kategorii jest GORSZE niż brak kategorii:
    kod nierozpoznany widać jako nierozpoznany i da się go sprawdzić
    w dokumentacji giełdy, a kod wpisany pod złą kategorię wygląda jak diagnoza
    i kieruje śledztwo w złe miejsce — np. każe szukać środków na rachunku tam,
    gdzie giełda mówiła o cenie.
    """
    kod = "EC_JakisNowyKodKtoregoNieZnamy"
    stan = stan_z_gieldy(kod)

    assert stan.powod_odmowy.kod == kod, "surowa wartość musi przejść bez zmian"
    assert stan.powod_odmowy.rodzaj == "nierozpoznane"
    nasze_kategorie = set(r for r, _ in POWODY_ODMOWY.values())
    assert stan.powod_odmowy.rodzaj not in nasze_kategorie, (
        "nieznany kod dostał jedną z NASZYCH kategorii — to jest podstawienie "
        "domysłu pod diagnozę")
    # Kod w TREŚCI opisu, nie tylko w polu obok: opis trafia do
    # `exec.order.reject_reason`, po którym szuka się gremem.
    assert kod in stan.powod_odmowy.opis
    assert "NIE ROZPOZNAJEMY" in stan.powod_odmowy.opis


def test_EC_NoError_i_puste_pole_NIE_SA_powodem_odmowy():
    """Bybit wypełnia `rejectReason` także przy zleceniu przyjętym.

    Bez tego rozróżnienia KAŻDE wypełnienie dostałoby powód odrzucenia —
    dziennik kłamałby w drugą stronę, a dozór meldowałby odmowy, których nie
    było.
    """
    for wartosc in ("EC_NoError", "", None):
        stan = stan_z_gieldy(wartosc, status="Filled")
        assert stan.powod_odmowy is None, (
            "rejectReason=%r przy zleceniu wypełnionym nie jest odmową"
            % (wartosc,))


# --------------------------- 3. powód trafia do BAZY: kolumna ORAZ `raw`

def test_odrzucone_zlecenie_zapisuje_powod_do_reject_reason():
    """`exec.order.reject_reason` przestaje być NULL — to jest cała rzecz.

    Dokładnie ten zapis (`UPDATE exec.order SET status=..., raw = raw || ...`)
    zostawiał kolumnę pustą na produkcji: ustawiał status i `raw`, a powodu nie
    dotykał ani jednym parametrem. Dozór czyta `reject_reason`
    (`dozor.py`: SELECT DISTINCT left(reject_reason,160) ... status='rejected'),
    więc przy NULL-u nie ma o czym meldować.
    """
    powod = PowodOdmowy(kod="EC_PostOnlyWillTakeLiquidity", rodzaj="postonly",
                        opis="spread przecięty")
    cur, rodzaj, msg = rozstrzygnij(stan_odrzucone(powod))

    assert cur.powody(), (
        "zamknięcie odrzuconego zlecenia nie zapisuje `reject_reason` w ogóle "
        "— kolumna zostaje NULL-em, dokładnie jak na produkcji")
    wartosci = [str(p) for p in cur.powody()]
    assert any("spread przecięty" in w for w in wartosci), (
        "powód nie doszedł do zapisu: %s" % (wartosci,))
    # ...i na ekran cyklu, bo inaczej operator widzi samo „nic nie weszło".
    assert "spread przecięty" in msg


def test_raw_trzyma_SUROWY_kod_obok_naszej_interpretacji():
    """Dwie wartości, nie jedna — nasze mapowanie może się pomylić.

    `raw.powod_odmowy_gieldy` to kod giełdy dosłownie, `raw.powod_odmowy_rodzaj`
    nasza kategoria. Gdyby został tylko rodzaj, pomyłka w mapowaniu byłaby nie
    do wykrycia po fakcie: z 'parametr' nie da się dojść do tego, CO giełda
    powiedziała.
    """
    powod = PowodOdmowy(kod="EC_PostOnlyWillTakeLiquidity", rodzaj="postonly",
                        opis="spread przecięty")
    cur, _, _ = rozstrzygnij(stan_odrzucone(powod))

    surowe = cur.surowe()
    assert surowe, "brak zapisu do `raw`"
    assert any(j.get("powod_odmowy_gieldy") == "EC_PostOnlyWillTakeLiquidity"
               for j in surowe), (
        "`raw` nie trzyma surowej wartości giełdy: %s" % (surowe,))
    assert any(j.get("powod_odmowy_rodzaj") == "postonly" for j in surowe)


def test_nierozpoznany_kod_dochodzi_do_bazy_doslownie():
    """Nierozpoznany kod jest zapisywany, a nie pomijany.

    Pominięcie go („nie wiemy, więc nie zapisujemy") daje ten sam NULL, od
    którego cała ta zmiana się zaczęła — tylko z lepszym uzasadnieniem.
    """
    powod = PowodOdmowy(kod="EC_CosNowego", rodzaj="nierozpoznane",
                        opis="powodu EC_CosNowego NIE ROZPOZNAJEMY")
    cur, _, _ = rozstrzygnij(stan_odrzucone(powod))

    wartosci = [str(p) for p in cur.powody()]
    assert any("EC_CosNowego" in w and "NIE ROZPOZNAJEMY" in w
               for w in wartosci), (
        "nierozpoznany kod nie doszedł do kolumny: %s" % (wartosci,))
    surowe = cur.surowe()
    assert any(j.get("powod_odmowy_gieldy") == "EC_CosNowego" for j in surowe)
    assert any(j.get("powod_odmowy_rodzaj") == "nierozpoznane" for j in surowe)


def test_odrzucenie_BEZ_powodu_nie_zostawia_NULL_a_mowi_ze_powodu_NIE_BYLO():
    """„Giełda nie podała powodu" to inna wiedza niż „nie zapytaliśmy".

    Kolumna pusta nie odróżnia tych dwóch stanów, a prowadzą one w dwa różne
    miejsca: jedno do dokumentacji giełdy, drugie do naszego adaptera. Ścieżka
    jest realna — Alpaca nie oddaje odpowiednika `rejectReason`.
    """
    cur, _, _ = rozstrzygnij(stan_odrzucone(None))

    wartosci = [str(p) for p in cur.powody()]
    assert any("NIE PODAŁA" in w for w in wartosci), (
        "brak powodu zapisany jako NULL, czyli nie do odróżnienia od braku "
        "odczytu: %s" % (wartosci,))
    assert any(j.get("powod_odmowy_rodzaj") == "nie_podany"
               for j in cur.surowe())


def test_wypelnione_zlecenie_nie_dostaje_zadnego_powodu_odmowy():
    """Kanarek w drugą stronę: zapis powodu nie może brudzić wypełnień.

    `reject_reason` przy zleceniu wypełnionym opisywałby odmowę, której nie
    było, a dozór melduje właśnie po tej kolumnie.
    """
    stan = StanZlecenia(id_wlasne="tai-p43", id_gieldy="G1", status="filled",
                        ilosc=D("8.87"), wypelniono=D("8.87"),
                        srednia_cena=D("120.84"), prowizja=D("0.1"),
                        prowizja_w="SOL")
    cur = Kursor([dict(id=55, qty=D("8.87"), submitted_at=TERAZ), dict(id=1)])
    asyncio.run(rozstrzygnij_plan(cur, Gielda(stan), PLAN, F, engine="crypto",
                                  mode="demo", kategoria="spot", teraz=TERAZ,
                                  kind="spot_eu"))
    for params in cur.powody():
        teksty = [x for x in (params or ()) if isinstance(x, str)]
        assert not any("odmow" in t.lower() for t in teksty), (
            "wypełnienie dostało powód odmowy: %s" % (params,))
    for j in cur.surowe():
        assert "powod_odmowy_rodzaj" not in j


# ----------------------------- 4. co ten powód znaczy dla ZACHOWANIA

def test_mapowanie_ma_tylko_kody_o_ktorych_cokolwiek_twierdzimy():
    """Tablica mapowań jest krótka celowo — wpis to zdanie „wiemy, co to znaczy".

    Test pilnuje DYSCYPLINY, nie liczby: każdy dołożony wpis wymaga dowodu
    (wartości zobaczonej w `raw` na produkcji albo cytatu z dokumentacji API),
    a nie pamięci. Dopisanie kodu „z pamięci" psuje ten test i ma go zepsuć —
    trzeba wtedy podać, skąd wiadomo.

    Jedyny kod, który ten projekt potrafi wskazać u siebie, to PostOnly:
    egzekucja wysyła `timeInForce: PostOnly` (`bybit.zloz_zlecenie`), a cena
    limitu to zamknięcie świecy sprzed kilkudziesięciu minut.
    """
    assert set(POWODY_ODMOWY) == set(["EC_PostOnlyWillTakeLiquidity"]), (
        "doszedł wpis do POWODY_ODMOWY — jeśli stoi na zmierzonej odpowiedzi "
        "giełdy, dopisz go tutaj razem z dowodem")


def test_odrzucenie_postonly_NIE_sklada_zastepczego_zlecenia():
    """Odmowa PO PRZYJĘCIU nie uruchamia mechanizmu `odmowa_gieldy` — i nie ma.

    `odmowa_gieldy` (ta sama ścieżka, którą zgłasza `OdmowaGieldy` z rodzajem
    'saldo') pyta giełdę o stan rachunku i składa ilość potwierdzoną.
    Uruchomienie jej przy odmowie PostOnly byłoby błędem: giełda nie
    kwestionuje ILOŚCI, tylko CENĘ, a powtórzenie tej samej ceny dostałoby tę
    samą odpowiedź.

    Test przypina obecne zachowanie, żeby ewentualna zmiana na „złóż zastępcze
    zlecenie" była decyzją widoczną w diffie, a nie skutkiem ubocznym zapisu
    powodu.
    """
    powod = PowodOdmowy(kod="EC_PostOnlyWillTakeLiquidity", rodzaj="postonly",
                        opis="spread przecięty")
    cur, rodzaj, _ = rozstrzygnij(stan_odrzucone(powod))

    assert rodzaj == "brak"
    assert not cur.zapytania("INSERT INTO exec.order"), (
        "rozstrzygnięcie złożyło NOWE zlecenie po odmowie PostOnly — byłaby to "
        "cena, której żadna decyzja już nie popiera")
    assert cur.zapytania("UPDATE journal.planned_order SET status='expired'")
