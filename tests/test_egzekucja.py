"""Składanie zleceń: idempotencja i wypełnienia częściowe.

DLACZEGO TO JEST NAJOSTROŻNIEJ TESTOWANY KAWAŁEK

Ponawianie GET-a jest niegroźne — najwyżej pobierzemy te same świece dwa razy.
Ponawianie POST-a po przekroczeniu czasu jest DOKŁADNIE tym mechanizmem, który
tworzy drugą pozycję: odpowiedź mogła nie dotrzeć, mimo że zlecenie weszło.

Objaw podwójnej pozycji nie wygląda na awarię. Wygląda na strategię, która
weszła mocniej, niż zakładał risk kernel — i dopiero uzgodnienie z giełdą
albo obsunięcie dwa razy większe od zmierzonego to pokazuje.
"""
import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from services.exchanges.base import ExchangeError
from services.exchanges.bybit import BybitAdapter, _status_bybita


def gielda(handler):
    k = BybitAdapter(api_key="k", api_secret="s")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k


def pusto():
    return {"retCode": 0, "result": {"list": []}}


def zlecenie(status="New", qty="1", wypelniono="0", link="dec-42"):
    return {"retCode": 0, "result": {"list": [{
        "orderId": "gielda-1", "orderLinkId": link, "orderStatus": status,
        "qty": qty, "cumExecQty": wypelniono, "avgPrice": "64000",
        "cumExecFee": "0.5",
    }]}}


# --------------------------------------------------------------- idempotencja

def test_zlecenie_o_znanym_id_NIE_JEST_skladane_drugi_raz():
    """Sedno. Poprzednia próba skończyła się timeoutem, ale zlecenie weszło —
    ponowne wywołanie musi je ODNALEŹĆ, a nie złożyć kolejne."""
    zapytania = []

    def handler(req):
        zapytania.append((req.method, str(req.url).split("?")[0]))
        if "/order/realtime" in str(req.url):
            return httpx.Response(200, json=zlecenie())
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    wynik = asyncio.run(k.zloz_zlecenie(symbol="BTCUSDC", strona="buy",
                                        ilosc=Decimal("1"), cena_limitu=Decimal("64000"),
                                        id_wlasne="dec-42"))
    assert wynik.juz_istnialo is True
    assert wynik.id_gieldy == "gielda-1"
    assert not any(m == "POST" for m, _ in zapytania), (
        f"złożono zlecenie mimo że już istniało: {zapytania}")


def test_zlecenie_nieznane_gieldzie_JEST_skladane():
    """Kanarek do poprzedniego. Gdyby sprawdzenie zawsze mówiło 'jest',
    system nie złożyłby nigdy żadnego zlecenia — i wyglądałoby to jak
    strategia bez sygnałów."""
    metody = []

    def handler(req):
        metody.append(req.method)
        if req.method == "POST":
            return httpx.Response(200, json={"retCode": 0,
                                             "result": {"orderId": "nowe-1"}})
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    wynik = asyncio.run(k.zloz_zlecenie(symbol="BTCUSDC", strona="buy",
                                        ilosc=Decimal("1"), cena_limitu=Decimal("64000"),
                                        id_wlasne="dec-99"))
    assert wynik.juz_istnialo is False
    assert wynik.id_gieldy == "nowe-1"
    assert "POST" in metody


def test_stan_zlecenia_szuka_TAKZE_w_historii():
    """Zlecenie mogło się wypełnić i zniknąć z listy otwartych między naszym
    timeoutem a zapytaniem. Szukanie wyłącznie wśród otwartych dałoby wtedy
    'nie ma' — czyli zgodę na złożenie drugiego."""
    sciezki = []

    def handler(req):
        sciezki.append(str(req.url).split("?")[0])
        if "/order/history" in str(req.url):
            return httpx.Response(200, json=zlecenie(status="Filled", wypelniono="1"))
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    stan = asyncio.run(k.stan_zlecenia("dec-42"))
    assert stan is not None and stan.status == "filled"
    assert any("history" in s for s in sciezki), "nie sprawdzono historii"


def test_POST_nie_jest_ponawiany_i_MOWI_co_zrobic():
    """Timeout przy składaniu nie może prowadzić do ponowienia w górę stosu.
    Komunikat musi kierować na SPRAWDZENIE, bo to jedyna bezpieczna reakcja."""
    def handler(req):
        if req.method == "POST":
            raise httpx.TimeoutException("brak odpowiedzi")
        return httpx.Response(200, json=pusto())

    k = gielda(handler)
    with pytest.raises(ExchangeError) as e:
        asyncio.run(k.zloz_zlecenie(symbol="BTCUSDC", strona="buy",
                                    ilosc=Decimal("1"), cena_limitu=Decimal("64000"),
                                    id_wlasne="dec-7"))
    assert e.value.retryable is False, "timeout przy POST oznaczony jako ponawialny"
    assert "sprawdz" in str(e.value).lower()


# --------------------------------------------------- wypełnienia częściowe

@pytest.mark.parametrize("ilosc,wypelniono,czesciowe", [
    ("1", "0", False),      # nic nie weszło
    ("1", "0.4", True),     # połowa
    ("1", "1", False),      # całość
])
def test_wypelnienie_czesciowe_jest_ROZPOZNAWANE(ilosc, wypelniono, czesciowe):
    """Symulacja zna dwa stany: wypełnione albo nie. Giełda zna trzeci."""
    def handler(req):
        return httpx.Response(200, json=zlecenie(status="PartiallyFilled",
                                                 qty=ilosc, wypelniono=wypelniono))

    stan = asyncio.run(gielda(handler).stan_zlecenia("dec-42"))
    assert stan.czesciowe is czesciowe, (
        f"{wypelniono} z {ilosc}: czesciowe={stan.czesciowe}, oczekiwano {czesciowe}")


def test_nieznany_status_zostaje_NIEZNANY():
    """Mapowanie słownikiem, nie łańcuchem if-ów: status, którego nie znamy,
    ma się rzucać w oczy, a nie wpaść do 'new' przez przypadek — bo 'new'
    znaczy 'czekamy', a to najgorszy domysł przy statusie nierozpoznanym."""
    assert _status_bybita("PartiallyFilled") == "partial"
    assert _status_bybita("Filled") == "filled"
    assert _status_bybita("CosCzegoNieZnamy").startswith("nieznany:")


def test_anulowanie_nieistniejacego_zlecenia_NIE_JEST_awaria():
    """Przy anulowaniu 'zlecenia nie ma' to sukces: cel osiągnięty."""
    def handler(req):
        return httpx.Response(200, json={"retCode": 110001, "retMsg": "order not exists"})

    assert asyncio.run(gielda(handler).anuluj_zlecenie(
        symbol="BTCUSDC", id_wlasne="dec-1")) is False


def test_identyfikator_wlasny_musi_byc_WYPROWADZONY_z_decyzji():
    """Idempotencja stoi na tym, że ponowienie użyje TEGO SAMEGO identyfikatora.
    Wyprowadzony z czasu albo losowy dawałby przy każdej próbie nowy — i cała
    ochrona znikałaby dokładnie wtedy, gdy jest potrzebna."""
    zrodlo = Path("services/exchanges/bybit.py").read_text(encoding="utf-8")
    fragment = zrodlo.split("async def zloz_zlecenie")[1].split("async def")[0]
    assert "uuid" not in fragment and "time.time()" not in fragment, (
        "identyfikator własny nie może powstawać w adapterze — musi przyjść z decyzji")


# ------------------------------------------------------ uzgodnienie z giełdą

from services.engine.uzgodnienie import rozjazdy  # noqa: E402


def test_zgodny_stan_nie_daje_rozjazdow():
    assert rozjazdy({"BTC": Decimal("1.5")}, {"BTC": Decimal("1.5")}) == []


def test_POZYCJA_WIDMO_jest_rozpoznawana():
    """Groźniejsza z dwóch. Risk kernel liczy z niej ekspozycję, więc blokuje
    wejścia, których nic nie blokuje — system stoi i nie ma o tym pojęcia."""
    r = rozjazdy({"BTC": Decimal("1.0")}, {"BTC": Decimal("0.2")})
    assert len(r) == 1 and r[0]["rodzaj"] == "pozycja-widmo"
    assert r[0]["roznica"] < 0


def test_SALDO_NIEZNANE_jest_rozpoznawane():
    """Druga strona: ekspozycja, o której limity nie wiedzą."""
    r = rozjazdy({"BTC": Decimal("0.2")}, {"BTC": Decimal("1.0")})
    assert len(r) == 1 and r[0]["rodzaj"] == "saldo-nieznane"
    assert r[0]["roznica"] > 0


def test_aktywo_znane_TYLKO_JEDNEJ_stronie_tez_jest_rozjazdem():
    """Brak wpisu to nie to samo co zero. Pozycja, o której giełda nic nie wie,
    i saldo, o którym my nic nie wiemy, są oba rozjazdami — a naiwne
    porównanie po kluczach wspólnych przemilczałoby oba."""
    assert len(rozjazdy({"BTC": Decimal("1")}, {})) == 1
    assert len(rozjazdy({}, {"ETH": Decimal("3")})) == 1


def test_tolerancja_przepuszcza_zaokraglenia_ale_nie_wiecej():
    """Giełda zaokrągla do kroku instrumentu, a prowizja bywa pobierana
    w kupionym aktywie. Porównanie na dokładną równość zgłaszałoby rozjazd
    przy każdej transakcji — ale tolerancja za duża przepuszczałaby realne
    różnice, więc test pilnuje obu stron."""
    assert rozjazdy({"BTC": Decimal("1")}, {"BTC": Decimal("1.0009")}) == []
    assert len(rozjazdy({"BTC": Decimal("1")}, {"BTC": Decimal("1.01")})) == 1


def test_porownanie_jest_CZYSTA_funkcja():
    """Bez bazy i bez sieci. Inaczej test tej logiki wymagałby giełdy,
    a giełda w teście to już nie jest test tej logiki."""
    import inspect

    zrodlo = inspect.getsource(rozjazdy)
    for zakazane in ("await", "fetch_", "adapter", "conn"):
        assert zakazane not in zrodlo, f"porównanie sięga po {zakazane}"


def test_brak_klucza_NIE_jest_zgodnoscia():
    """Milczące zero powiedziałoby 'sprawdzone i w porządku' o czymś, czego
    nie sprawdzono."""
    zrodlo = Path("services/engine/uzgodnienie.py").read_text(encoding="utf-8")
    assert "nie ma z czym porównać" in zrodlo


def test_kazdy_budowniczy_adaptera_bierze_HOST_Z_BAZY():
    """Pułapka, w którą projekt już raz wszedł — i to po tym, jak ją opisał.

    Podmiot unijny ma własne hosty. Klucz z bybit.eu odbity od hosta globalnego
    wraca z kodem 10003, czyli komunikatem NIE DO ODRÓŻNIENIA od złego klucza:
    szukanie idzie wtedy na klucz, a wina jest w adresie.

    Każde miejsce budujące adapter musi więc podać host jawnie. Test sprawdza
    wszystkie naraz, bo następne takie miejsce powstanie za miesiąc i nikt nie
    będzie pamiętał.
    """
    budowniczowie = [
        "services/api/routers/credentials.py",
        "services/engine/uzgodnienie.py",
    ]
    for plik in budowniczowie:
        zrodlo = Path(plik).read_text(encoding="utf-8")
        if "BybitAdapter(" not in zrodlo:
            continue
        fragment = zrodlo.split("BybitAdapter(")[1][:400]
        assert "host=" in fragment, (
            f"{plik}: adapter budowany bez jawnego hosta — klucz z bybit.eu "
            f"odbije się od globalnego z kodem nie do odróżnienia od złego klucza")


# Reguly instrumentu tak, jak oddaje je gielda — z produkcji (`ref.instrument`).
# Trzymane w jednym miejscu, bo prog bezwzgledny uzgodnienia liczy sie WLASNIE
# z nich i test na wymyslonych liczbach mierzylby wtedy wlasna atrape.
D = Decimal

REGULY_Z_PRODUKCJI = {
    # aktywo: (lot_size, min_notional, ostatnia cena)
    "BTC": (D("0.000001"), D("5"), D("50000")),
    "LINK": (D("0.01"), D("5"), D("12.02")),
    "ADA": (D("0.1"), D("5"), D("0.74")),
    "SOL": (D("0.001"), D("5"), D("116.06")),
}


def _wiersze_regul(aktywa):
    """Odpowiedz na zapytanie `progi_bezwzgledne` — kolumny jak w SQL."""
    out = []
    for a in aktywa:
        lot, minw, cena = REGULY_Z_PRODUKCJI.get(a, (D("0.001"), D("5"), D("1")))
        out.append({"aktywo": a, "lot": lot, "min_wartosc": minw, "cena": cena})
    return out


def _fetch_all_stub(pozycje, symbole=(("BTCUSDC", "BTC"),), od=None,
                    okno=None):
    """Rozdziela zapytania po treści SQL — jeden stub na cztery różne pytania."""
    async def fetch_all(sql, params=None):
        # Nowe pytanie uzgodnienia: czy ktoras otwarta pozycja zostala
        # zaksiegowana BRUTTO (`uzgodnienie.zaksiegowane_brutto`).
        # Atrapa musi je ZNAC, inaczej oddalaby na nie wiersz z innego
        # zapytania - czyli mierzylaby sama siebie.
        if "AS prowizja_w" in sql:
            return []
        # Zlecenia w locie (`uzgodnienie.zlecenia_w_locie`) - brak.
        if "AS strona" in sql:
            return []
        # Rozroznienie po NAJWEZSZYM markerze. Zapytanie o pozycje takze
        # dotyka ref.instrument, wiec dopasowanie po nazwie tabeli laczyloby
        # dwa rozne pytania w jedno - i test padalby na wlasnym stubie.
        if "sum(p.qty)" in sql:
            return pozycje
        if "min(i.lot_size)" in sql:
            return _wiersze_regul([b for _, b in symbole])
        if "LEAST(p.opened_at" in sql:
            # KOLUMNY DOKLADNIE TAKIE, JAKIE POBIERA `kontekst_wypelnien`.
            # Atrapa oddajaca kolumne, o ktora prawdziwy SELECT nie prosi,
            # sprawdzalaby sama siebie — a `od` i `opened_at` to tutaj DWIE
            # ROZNE chwile (zlozenie zlecenia kontra zapis skutku).
            if okno is not None:
                return list(okno)
            # Bez wiersza w `exec.order` prawdziwy `LEAST` oddaje samo
            # `opened_at`, bo `LEAST` pomija NULL-e.
            return ([] if od is None
                    else [{"opened_at": od, "closed_at": None, "od": od}])
        return [{"symbol": s, "base_ccy": b} for s, b in symbole]
    return fetch_all


async def _notify_zabroniony(*a, **k):
    raise AssertionError("nie powiadamiamy o rozjazdach, których nie zmierzono")


class _BezSald:
    """Host, który na każdą ścieżkę do salda odpowiada 404 — jak demo bybit.eu."""

    def __init__(self, wypelnienia=None, blad_wypelnien=None):
        self._wyp = wypelnienia or []
        self._blad = blad_wypelnien

    async def fetch_balances(self):
        from services.exchanges.base import ExchangeError
        raise ExchangeError("endpoint niedostepny na tym hoscie", code="404")

    async def wypelnienia(self, **kw):
        if self._blad:
            raise self._blad
        return self._wyp


def _wypelnienie(qty, side="Buy", czas=None, symbol="BTCUSDC"):
    from datetime import datetime, timezone

    from services.exchanges.base import Wypelnienie
    return Wypelnienie(
        id_gieldy="x", id_wlasne=None, symbol=symbol, strona=side,
        ilosc=Decimal(qty), cena=Decimal("50000"), prowizja=Decimal("0"),
        prowizja_w=None,
        czas=czas or datetime(2026, 9, 1, tzinfo=timezone.utc))


def test_brak_sald_I_brak_wypelnien_to_NIEWYKONALNE_a_nie_zgodnosc():
    """Najgroźniejsza możliwa odpowiedź uzgodnienia to ciche zero.

    Gdyby brak sald potraktować jak pusty ich zestaw, porównanie poszłoby
    z niczym. Sprawdzone kanarkiem: taka wersja nie melduje nawet zgodności —
    melduje FAŁSZYWY rozjazd „pozycja-widmo" o pozycji, która istnieje.
    """
    import asyncio

    from services.engine.uzgodnienie import Nieuzgadnialne, uzgodnij
    from services.exchanges.base import ExchangeError

    gielda = _BezSald(blad_wypelnien=ExchangeError("brak", code="404"))
    pozycje = [{"aktywo": "BTC", "ilosc": Decimal("0.5")}]
    with pytest.raises(Nieuzgadnialne) as exc:
        asyncio.run(uzgodnij(_fetch_all_stub(pozycje), _notify_zabroniony, gielda))
    assert "NIEWYKONALNE" in str(exc.value)


def test_rejestr_krotszy_niz_nasza_historia_to_NIEWYKONALNE():
    """Okno retencji giełdy krótsze od naszej historii zaniża odtworzoną
    pozycję — i wygląda dokładnie jak pozycja-widmo."""
    import asyncio
    from datetime import datetime, timezone

    from services.engine.uzgodnienie import Nieuzgadnialne, uzgodnij

    stare_otwarcie = datetime(2026, 8, 1, tzinfo=timezone.utc)
    gielda = _BezSald([_wypelnienie("0.5", czas=datetime(2026, 9, 5, tzinfo=timezone.utc))])
    pozycje = [{"aktywo": "BTC", "ilosc": Decimal("0.5")}]
    with pytest.raises(Nieuzgadnialne) as exc:
        asyncio.run(uzgodnij(_fetch_all_stub(pozycje, od=stare_otwarcie),
                             _notify_zabroniony, gielda))
    assert "retencji" in str(exc.value)


def test_zgodnosc_z_rejestrem_NIE_JEST_nazywana_zgodnoscia_z_saldami(capsys):
    """Dwa różne pytania nie mogą dać tego samego komunikatu.

    Rejestr transakcji nie widzi ręcznego zamknięcia w panelu ani przelewu.
    Komunikat, który by to zacierał, trafiłby do decyzji o dopuszczeniu trybu
    do handlu jako mocniejszy, niż jest naprawdę.
    """
    import asyncio
    from datetime import datetime, timezone

    from services.engine.uzgodnienie import uzgodnij

    otwarcie = datetime(2026, 9, 1, tzinfo=timezone.utc)
    gielda = _BezSald([_wypelnienie("0.5", czas=otwarcie)])
    pozycje = [{"aktywo": "BTC", "ilosc": Decimal("0.5")}]
    n = asyncio.run(uzgodnij(_fetch_all_stub(pozycje, od=otwarcie),
                             _notify_zabroniony, gielda))
    assert n == 0
    out = capsys.readouterr().out
    assert "REJESTREM TRANSAKCJI" in out
    assert "zgodne z saldami" not in out
    assert "niesprawdzone" in out


def test_ocena_hosta_odrzuca_brak_kazdej_sciezki_do_salda():
    """Sciezek do salda jest kilka; brak JEDNEJ nie przesądza, brak wszystkich tak."""
    from services.exchanges.mozliwosci import SPRAWDZANE, ocena

    pelne = {n: {"jest": True} for n, _, _, _ in SPRAWDZANE}
    assert ocena(pelne) == (True, [])

    # Sam UNIFIED niedostepny — SPOT wystarcza, wiec handel nadal dopuszczalny.
    jedna_mniej = dict(pelne, saldo_unified={"jest": False})
    assert ocena(jedna_mniej)[0] is True

    bez_salda = dict(pelne)
    for n in ("saldo_unified", "saldo_spot", "saldo_aktywa"):
        bez_salda[n] = {"jest": False}
    mozna, powody = ocena(bez_salda)
    assert mozna is False
    assert any("NIEWYKONALNE" in p for p in powody)

    bez_historii = dict(pelne, zlecenia_historia={"jest": False})
    assert ocena(bez_historii)[0] is False


class _GieldaZWypelnieniami:
    """Adapter z podmienioną warstwą HTTP — reszta metody działa jak w produkcji."""

    def __init__(self, strony):
        self.strony = strony
        self.zapytania = []

    async def _get(self, path, params=None, **kw):
        self.zapytania.append((path, dict(params or {})))
        return self.strony[len(self.zapytania) - 1]


def _adapter_z_odpowiedziami(strony):
    from services.exchanges.bybit import BybitAdapter
    a = BybitAdapter.__new__(BybitAdapter)
    a._category = "spot"
    a._get = _GieldaZWypelnieniami(strony)._get
    return a


def _wyp(exec_id, qty, side="Buy", link=None):
    return {"execId": exec_id, "orderLinkId": link, "symbol": "BTCUSDC",
            "side": side, "execQty": qty, "execPrice": "50000",
            "execFee": "0.1", "feeCurrency": "USDC", "execTime": "1700000000000"}


def test_wypelnienia_pobieraja_WSZYSTKIE_strony():
    """Zatrzymanie się na pierwszej stronie podaje ostatnie kilkadziesiąt
    transakcji jako całą historię — a odtworzona z tego pozycja jest zaniżona."""
    import asyncio

    a = _adapter_z_odpowiedziami([
        {"list": [_wyp("a", "1")], "nextPageCursor": "k1"},
        {"list": [_wyp("b", "2")], "nextPageCursor": "k2"},
        {"list": [_wyp("c", "3")], "nextPageCursor": ""},
    ])
    out = asyncio.run(a.wypelnienia())
    assert [w.id_gieldy for w in out] == ["a", "b", "c"]
    assert sum(w.ilosc for w in out) == Decimal("6")


def test_urwana_paginacja_KONCZY_SIE_BLEDEM_a_nie_krotsza_lista():
    """Cicha ucinka wygląda potem jak rozjazd „pozycja-widmo" — alarm
    o pozycji, która naprawdę istnieje. Lepszy błąd niż fałszywy alarm."""
    import asyncio

    from services.exchanges.base import ExchangeError
    from services.exchanges.bybit import BybitAdapter

    strony = [{"list": [_wyp(str(i), "1")], "nextPageCursor": "dalej"}
              for i in range(BybitAdapter.STRON_MAX + 1)]
    a = _adapter_z_odpowiedziami(strony)
    with pytest.raises(ExchangeError) as exc:
        asyncio.run(a.wypelnienia())
    assert "niepelna" in str(exc.value)


def test_pusto_po_obu_stronach_NIE_jest_nazywane_zgodnoscia(capsys):
    """Zgadzają się dwa puste zbiory — czyli nie porównano niczego.

    Na tym właśnie komunikacie opiera się decyzja o dopuszczeniu trybu do
    handlu. „Zgodne" przy zerze pozycji brzmi tak samo mocno jak „zgodne" po
    realnym porównaniu, a znaczy nieporównanie mniej.
    """
    import asyncio

    from services.engine.uzgodnienie import uzgodnij

    class PusteSalda:
        async def fetch_balances(self):
            return []

    n = asyncio.run(uzgodnij(_fetch_all_stub([]), _notify_zabroniony, PusteSalda()))
    assert n == 0
    out = capsys.readouterr().out
    assert "NIC NIE ZOSTAŁO PORÓWNANE" in out
    assert "zgodne" not in out.lower()


# --------------------------------------------- cennik: kod kontra giełda

def test_rozjazd_stawek_lapie_zmiane_cennika_gieldy():
    """Cała przewaga strategii mieści się w kilku punktach bazowych, a stawki
    nie są stałe: zmieniają je progi VIP, promocje i regulamin podmiotu.

    ROZJAZD NIE OBJAWIA SIĘ NICZYM. Rachunek papierowy dalej liczy po starej
    stawce, backtest dalej pokazuje przewagę, a prawdziwy wynik po prostu jest
    inny — i wygląda jak pech albo zmiana rynku.
    """
    from services.engine.koszty import koszty
    from services.exchanges.mozliwosci import rozjazd_stawek

    k = koszty("spot_eu")
    # Zmierzone na produkcji 2026-09-07: maker 0.0010, taker 0.0025.
    assert rozjazd_stawek({"maker": Decimal("0.001"),
                           "taker": Decimal("0.0025")}, k) == []

    (opis,) = rozjazd_stawek({"maker": Decimal("0.002"),
                              "taker": Decimal("0.0025")}, k)
    assert "maker" in opis
    assert "0.2000%" in opis and "0.1000%" in opis   # obie liczby, nie sama roznica


def test_brak_odczytu_stawek_to_NIE_jest_zgodnosc():
    """Milczące zero powiedziałoby „sprawdzone i w porządku" o czymś, czego
    sprawdzić się nie da — a cennik wyznacza cały modelowany wynik."""
    from services.engine.koszty import koszty
    from services.exchanges.mozliwosci import rozjazd_stawek

    (opis,) = rozjazd_stawek({}, koszty("spot_eu"))
    assert "NIESPRAWDZONY" in opis


def test_prog_rozjazdu_ignoruje_zaokraglenia_ale_nie_zmiane_stawki():
    """Poniżej punktu bazowego różnica nie zmienia wniosków; powyżej zmienia
    je natychmiast, bo mediana zwrotu na transakcję jest tego samego rzędu."""
    from services.engine.koszty import koszty
    from services.exchanges.mozliwosci import rozjazd_stawek

    k = koszty("spot_eu")
    ledwie = {"maker": Decimal("0.00105"), "taker": Decimal("0.0025")}
    wyraznie = {"maker": Decimal("0.0013"), "taker": Decimal("0.0025")}
    assert rozjazd_stawek(ledwie, k) == []
    assert len(rozjazd_stawek(wyraznie, k)) == 1


def test_uzgodnienie_WIDZI_aktywo_ktorego_nie_ma_w_dzienniku():
    """Kierunek „saldo-nieznane": giełda trzyma coś, o czym system nie wie.

    To jest pozycja, której nikt nie pilnuje — risk kernel nie liczy z niej
    ekspozycji, stop na obsunięciu jej nie widzi. Filtr zbudowany z naszych
    własnych pozycji wycinał ją, zanim porównanie ją zobaczyło, więc
    uzgodnienie meldowało zgodność, mając przed sobą dokładnie ten przypadek,
    dla którego istnieje.
    """
    import asyncio

    from services.engine.uzgodnienie import uzgodnij

    class Gielda:
        async def fetch_balances(self):
            from services.exchanges.base import Balance
            return [Balance(currency="SOL", total=Decimal("4"),
                            available=Decimal("4"))]

    async def fetch_all(sql, params=None):
        # Nowe pytanie uzgodnienia: czy ktoras otwarta pozycja zostala
        # zaksiegowana BRUTTO (`uzgodnienie.zaksiegowane_brutto`).
        # Atrapa musi je ZNAC, inaczej oddalaby na nie wiersz z innego
        # zapytania - czyli mierzylaby sama siebie.
        if "AS prowizja_w" in sql:
            return []
        # Zlecenia w locie (`uzgodnienie.zlecenia_w_locie`) - brak.
        if "AS strona" in sql:
            return []
        if "sum(p.qty)" in sql:
            return []                       # nasz dziennik jest PUSTY
        if "DISTINCT base_ccy" in sql:
            return [{"base_ccy": "SOL"}, {"base_ccy": "XRP"}]
        if "min(i.lot_size)" in sql:
            return _wiersze_regul(["SOL", "XRP"])
        if "min(p.opened_at)" in sql:
            return [{"od": None}]
        return [{"id": 1}]

    zgloszone = []

    async def notify(uid, kod, tytul, tresc, **kw):
        zgloszone.append((kod, tytul))

    n = asyncio.run(uzgodnij(fetch_all, notify, Gielda()))
    assert n == 1
    assert zgloszone, "rozjazd musi trafić do powiadomienia, nie tylko na ekran"


def test_uzgodnienie_nadal_pomija_walute_kwotowana():
    """Giełda trzyma USDC i resztki po operacjach spoza uniwersum. To nie są
    pozycje i ich obecność nie jest rozjazdem — inaczej alarm brzmiałby
    przy każdym uruchomieniu."""
    import asyncio

    from services.engine.uzgodnienie import uzgodnij

    class Gielda:
        async def fetch_balances(self):
            from services.exchanges.base import Balance
            return [Balance(currency="USDC", total=Decimal("100000"),
                            available=Decimal("100000")),
                    Balance(currency="DOGE", total=Decimal("7"),
                            available=Decimal("7"))]

    async def fetch_all(sql, params=None):
        # Nowe pytanie uzgodnienia: czy ktoras otwarta pozycja zostala
        # zaksiegowana BRUTTO (`uzgodnienie.zaksiegowane_brutto`).
        # Atrapa musi je ZNAC, inaczej oddalaby na nie wiersz z innego
        # zapytania - czyli mierzylaby sama siebie.
        if "AS prowizja_w" in sql:
            return []
        # Zlecenia w locie (`uzgodnienie.zlecenia_w_locie`) - brak.
        if "AS strona" in sql:
            return []
        if "sum(p.qty)" in sql:
            return []
        if "DISTINCT base_ccy" in sql:
            return [{"base_ccy": "SOL"}]
        if "min(i.lot_size)" in sql:
            return _wiersze_regul(["SOL"])
        if "min(p.opened_at)" in sql:
            return [{"od": None}]
        return [{"id": 1}]

    async def notify(*a, **k):
        raise AssertionError("waluta kwotowana nie jest rozjazdem")

    assert asyncio.run(uzgodnij(fetch_all, notify, Gielda())) == 0
