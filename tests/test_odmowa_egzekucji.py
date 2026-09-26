"""Odmowa giełdy: cztery wady, przez które pozycja stała 19 godzin nie do zamknięcia.

AWARIA, KTÓRA TEN PLIK OPISUJE — liczby z produkcji, 2026-09-23/24

Pozycja 32 (SOLUSDC) została zaksięgowana BRUTTO: `exec.position.qty` = 12.138,
a na rachunku leżało 12.125862, bo Bybit pobrał prowizję kupna W KUPIONYM
AKTYWIE. Każde wyjście prosiło więc o więcej, niż jest, i wracało z giełdy jako
`Insufficient balance. (kod 170131)`:

    order 49  2026-09-23 17:18:52  qty 12.138  ->  cancelled
    order 51  2026-09-23 21:28:58  qty 12.138  ->  cancelled
    order 52  2026-09-24 01:14:54  qty 12.138  ->  cancelled
    order 53  2026-09-24 05:15:52  qty 12.138  ->  cancelled

Wszystkie cztery bez `venue_order_id`, wszystkie z `raw.etap` =
`odpowiedz_nie_dotarla`. Trzy niezależne warstwy nadzoru meldowały w tym czasie
„bez zastrzeżeń". Każda z czterech wad poniżej wystarczała sama, żeby awaria
została niewidoczna — dlatego każda ma tu własny test i własnego kanarka.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal as D

from services.engine.dozor import ocen_egzekucje
from services.engine.gielda_exec import posprzataj_zlecenia, zloz_plan
from services.engine.uzgodnienie import uzgodnij
from services.engine.zlecenia import Filtry
from services.exchanges.base import Balance, ExchangeError, ZlozoneZlecenie
from services.exchanges.bybit import _blad_z_kodu

# NOWE NAZWY IMPORTUJEMY W SRODKU TESTOW, ktore ich uzywaja.
#
# Na kodzie SPRZED poprawki tych nazw nie ma wcale, a import na gorze modulu
# zamienilby kazda z 21 pozycji w jeden blad zbierania - czyli test "zawodzacy
# przed poprawka" nie powiedzialby, KTORA wade lapie. Import lokalny zostawia
# pozostalym testom ich wlasne, czytelne niepowodzenia.

# Reguly SOLUSDC z `ref.instrument`, stan produkcyjny.
F_SOL = Filtry(krok_ilosci=D("0.001"), min_ilosc=None,
               min_wartosc=D("5"), krok_ceny=D("0.01"))

KSIEGI = D("12.138")          # exec.position.qty — BRUTTO
RACHUNEK = D("12.125862")     # ile naprawde lezy na koncie (brutto - prowizja)
PROWIZJA = D("0.012138")      # 0,1% makera, pobrane w SOL

PLAN_WYJSCIA = {
    "id": 53, "decision_id": 1668, "instrument_id": 3, "side": "sell",
    "qty": KSIEGI, "limit_price": D("117.24"), "symbol": "SOLUSDC",
    "quote_ccy": "USDC", "base_ccy": "SOL", "atr_pct": 2.0,
    "trigger": {"rodzaj": "wyjscie", "position_id": 32, "bar": "4h"},
}


class Kursor:
    """Atrapa kursora. Pilnuje liczby parametrów i UNIQUE (mode, client_order_id).

    Ograniczenie unikalnosci jest tu istotne, a nie ozdobne: nowa sciezka
    odmowy sklada zlecenie ZASTEPCZE pod tym samym `client_order_id`, wiec
    zapisanie najpierw odmowy, a potem zlecenia byloby drugim INSERT-em z tym
    samym kluczem — i wywrocilo CALA transakcje cyklu na produkcji, gdzie
    atrapy nie ma.
    """

    def __init__(self, wiersze=None):
        self.sql = []
        self._wiersze = list(wiersze or [])
        self._klucze = set()

    def execute(self, sql, params=None):
        jedno = " ".join(sql.split())
        assert jedno.count("%s") == (0 if params is None else len(params)), (
            f"zapytanie ma {jedno.count('%s')} miejsc na parametry, a dostało "
            f"{0 if params is None else len(params)}: {jedno}")
        if "INSERT INTO exec.order" in jedno:
            for p in params or ():
                if isinstance(p, str) and p.startswith("tai-"):
                    assert p not in self._klucze, (
                        f"drugi INSERT INTO exec.order z client_order_id={p!r} "
                        f"łamie UNIQUE (mode, client_order_id)")
                    self._klucze.add(p)
        self.sql.append((jedno, params))

    def fetchone(self):
        return self._wiersze.pop(0) if self._wiersze else None

    def fetchall(self):
        w, self._wiersze = self._wiersze, []
        return w

    def zapytania(self, fragment):
        return [(s, p) for s, p in self.sql if fragment in s]


class Gielda:
    """Giełda, która odmawia za saldo i (opcjonalnie) podaje stan rachunku."""

    def __init__(self, *, blad, salda=None, blad_sald=None, stan=None):
        self._blad = blad
        self._salda = salda
        self._blad_sald = blad_sald
        self._stan = stan
        self.zlozone = []

    async def cena_odniesienia(self, symbol, kategoria):
        # None = brak odniesienia, wiec cena zostaje planowa. Pasmo ceny nie
        # jest przedmiotem tego pliku i podstawianie go tu mierzyloby co innego.
        return None

    async def fetch_order_book(self, symbol, depth=50, *, ksiega_egzekucji=True):
        """Arkusz NIEODCZYTANY — i to jest w tym pliku decyzja, nie brak.

        `zloz_plan` czyta arkusz księgi egzekucji, żeby cena limitu pochodziła
        z chwili wysyłki. Ten plik mierzy co innego (POWÓD odmowy giełdy),
        a podstawienie arkusza podmieniałoby cenę wysyłaną i mieszało dwa
        pomiary w jednej asercji. Atrapa MUSI jednak tę metodę mieć: bez niej
        produkcyjne wywołanie kończy się `AttributeError`, który wygląda jak
        błąd testu, a nie jak luka w atrapie.
        """
        raise ExchangeError("arkusz nieczytelny (atrapa)")

    async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
        return self._stan

    async def zloz_zlecenie(self, **kw):
        # Odmowa dotyczy WYLACZNIE ilosci z ksiag. Ilosc mniejsza przechodzi —
        # bo na rachunku naprawde tyle jest. Dokladnie tak zachowala sie gielda.
        if kw["ilosc"] > RACHUNEK:
            raise self._blad
        self.zlozone.append(kw)
        return ZlozoneZlecenie(id_wlasne=kw["id_wlasne"], id_gieldy="G-53",
                               status="new")

    async def fetch_balances(self):
        if self._blad_sald:
            raise self._blad_sald
        return list(self._salda or [])

    async def anuluj_zlecenie(self, **kw):
        return None


def _zloz(gielda, plan=None, kursor=None):
    cur = kursor or Kursor()
    ok, msg = asyncio.run(zloz_plan(cur, gielda, dict(plan or PLAN_WYJSCIA),
                                    F_SOL, engine="crypto", mode="demo",
                                    kategoria="spot"))
    return cur, ok, msg


def _raw(cur):
    """`raw` z jedynego INSERT-a do `exec.order` — ostatni parametr."""
    (_, params), = cur.zapytania("INSERT INTO exec.order")
    return json.loads(params[-1])


def _status(cur):
    """Status wpisany do `exec.order` — literałem albo parametrem.

    Obie postacie istnieją w kodzie naprawdę (`'rejected'` i `'unknown'` są
    wpisane wprost, status z giełdy idzie parametrem), więc atrapa musi
    rozpoznać obie. Zwracanie parametru dla zapytania z literałem podałoby
    pierwszą lepszą liczbę i test przechodziłby na nieprawdzie.
    """
    (sql, params), = cur.zapytania("INSERT INTO exec.order")
    for literal in ("'rejected'", "'unknown'"):
        if literal in sql:
            return literal.strip("'")
    return params[-4]


# ============================================================== ZNALEZISKO 2
#
# Twarda odmowa giełdy zapisywana jako „stan nieznany".

def test_kod_170131_jest_ODMOWA_a_nie_zwyklym_bledem():
    """`Insufficient balance` to odpowiedź JEDNOZNACZNA — giełda powiedziała
    „nie". Wpadanie tego kodu do ogólnego `ExchangeError` sprawiało, że ścieżka
    błędu nie miała jak odróżnić odmowy od braku odpowiedzi."""
    from services.exchanges.base import OdmowaGieldy

    blad = _blad_z_kodu("170131", "Insufficient balance.")

    assert isinstance(blad, OdmowaGieldy), (
        f"kod 170131 sklasyfikowany jako {type(blad).__name__} — giełda "
        f"odpowiedziała, a kod zapisze to jako brak odpowiedzi")
    assert blad.rodzaj == "saldo" and blad.brak_salda
    assert blad.retryable is False


def test_kod_zlego_parametru_tez_jest_odmowa_ale_INNEGO_rodzaju():
    """Krok, precyzja i minimum to odmowa tak samo pewna jak saldo, ale reakcja
    jest inna: pytanie o stan rachunku nic tu nie zmieni."""
    from services.exchanges.base import OdmowaGieldy

    blad = _blad_z_kodu("170137", "Order quantity has too many decimals")

    assert isinstance(blad, OdmowaGieldy)
    assert blad.rodzaj == "parametr" and not blad.brak_salda


def test_kod_NIEZNANY_nadal_jest_zwyklym_bledem():
    """Kanarek do obu powyższych. Zaliczenie kodu do odmów znaczy zapisanie
    „zlecenia NA PEWNO nie ma" — pomyłka w tę stronę porzuca zlecenie ŻYWE na
    giełdzie, czyli ekspozycję, której risk kernel nie policzy. Kody spoza
    zbioru muszą więc zostać przy `unknown`."""
    from services.exchanges.base import OdmowaGieldy

    blad = _blad_z_kodu("999999", "cos zupelnie nowego")

    assert not isinstance(blad, OdmowaGieldy), (
        "nieznany kod zaliczony do odmów — zlecenie, które MOGŁO wejść, "
        "zostanie zapisane jako nieistniejące")


def test_odmowa_zapisuje_REJECTED_z_prawdziwym_powodem():
    """Sedno znaleziska 2: `unknown` jest stanem, którego nic nie przerywa.

    Każda świeca widzi pozycję nadal otwartą, planuje wyjście od nowa i dostaje
    tę samą odmowę — a licznik odrzuceń czujki nie drgnie, bo `rejected` nie
    zostaje zapisane ANI RAZU."""
    gielda = Gielda(blad=_blad_z_kodu("170131", "Insufficient balance."),
                    # Sald nie da sie odczytac, wiec zostaje sama odmowa —
                    # sciezka zastepcza jest przedmiotem znaleziska 1 nizej.
                    blad_sald=ExchangeError("brak endpointu", code="404"))
    cur, ok, msg = _zloz(gielda)

    assert ok is False
    assert _status(cur) == "rejected", (
        "twarda odmowa giełdy zapisana jako stan nieznany — pętla wyjścia nie "
        "ma się o co zatrzymać i powtórzy się przy każdej świecy")
    assert _raw(cur)["etap"] != "odpowiedz_nie_dotarla"
    assert _raw(cur)["kod_gieldy"] == "170131"
    assert cur.zapytania("SET status='rejected'"), (
        "plan zostaje `pending` po twardej odmowie — rozstrzyganie wróci do "
        "niego w każdym cyklu i odczyta w kółko to samo")


def test_BRAK_ODPOWIEDZI_nadal_zapisuje_UNKNOWN():
    """Kanarek do poprzedniego i granica całej poprawki. Przy przekroczeniu
    czasu zlecenie MOGŁO wejść, więc `unknown` jest tam jedyną uczciwą
    odpowiedzią — zamiana go na `rejected` porzuciłaby żywe zlecenie."""
    gielda = Gielda(blad=ExchangeError("brak odpowiedzi na zlecenie",
                                       code="timeout"))
    cur, ok, _ = _zloz(gielda)

    assert ok is False
    assert _status(cur) == "unknown", (
        "brak odpowiedzi zapisany jako odrzucenie — zlecenie mogło wejść, "
        "a nikt po nie nie wróci")
    assert _raw(cur)["etap"] == "odpowiedz_nie_dotarla"


# ============================================================== ZNALEZISKO 1
#
# Wyjście prosi o więcej, niż potwierdza giełda.

def test_wyjscie_sklada_TYLE_ILE_GIELDA_POTWIERDZA():
    """Zamiast powtarzać 12.138 co świecę, pytamy o stan rachunku i wysyłamy
    to, co tam leży. Brakuje dokładnie prowizji, nie płynności: 12.125 SOL
    sprzedaje się normalnie za ~1421 USDC."""
    gielda = Gielda(blad=_blad_z_kodu("170131", "Insufficient balance."),
                    salda=[Balance(currency="SOL", total=RACHUNEK,
                                   available=RACHUNEK)])
    cur, ok, msg = _zloz(gielda)

    assert ok is True, f"wyjście nadal nie poszło: {msg}"
    assert len(gielda.zlozone) == 1
    assert gielda.zlozone[0]["ilosc"] == D("12.125"), (
        f"wysłano {gielda.zlozone[0]['ilosc']} zamiast ilości potwierdzonej "
        f"zaokrąglonej w dół do kroku 0.001")


def test_rozjazd_jest_WIDOCZNY_a_nie_zamieciony():
    """Pułapka z pierwszego audytu: „sprzedaj, co jest" łatwo zamienić w ciche
    domknięcie pozycji. Jeśli giełda potwierdza mniej niż księgi, to jest
    ROZJAZD — musi zostać zapisany z obiema liczbami, a `exec.position` NIE
    WOLNO ruszyć, bo to zamieniłoby awarię widoczną w awarię cichą."""
    gielda = Gielda(blad=_blad_z_kodu("170131", "Insufficient balance."),
                    salda=[Balance(currency="SOL", total=RACHUNEK,
                                   available=RACHUNEK)])
    cur, _, msg = _zloz(gielda)

    (_, params), = cur.zapytania("INSERT INTO exec.order")
    powod = next(p for p in params if isinstance(p, str) and "ROZJAZD" in p)
    assert str(KSIEGI) in powod and str(RACHUNEK) in powod, (
        f"powód nie niesie obu liczb, więc nie da się z niego odtworzyć "
        f"rozjazdu: {powod}")

    rozjazd = _raw(cur)["rozjazd_ksiag"]
    assert rozjazd["ksiegi"] == str(KSIEGI)
    assert rozjazd["gielda"] == str(RACHUNEK)
    assert rozjazd["position_id"] == 32

    assert not cur.zapytania("UPDATE exec.position"), (
        "ilość pozycji poprawiona po cichu przy okazji sprzedaży — rozjazd "
        "znika z ksiąg razem z dowodem, że w ogóle zaszedł")
    assert not cur.zapytania("closed_at=now()"), (
        "pozycja domknięta przy odmowie — to jest maskowanie rozjazdu, "
        "a nie jego naprawa")


def test_bez_odczytu_sald_NIE_ZGADUJEMY_ilosci():
    """`api-demo.bybit.eu` nie wystawia sald (404 na każdej ścieżce), więc to
    jest stan CODZIENNY, nie brzegowy. Podstawienie zgadniętej ilości
    sprzedałoby pozycję na podstawie liczby, której nikt nie potwierdził."""
    gielda = Gielda(blad=_blad_z_kodu("170131", "Insufficient balance."),
                    blad_sald=ExchangeError("endpoint niedostepny", code="404"))
    cur, ok, msg = _zloz(gielda)

    assert ok is False and gielda.zlozone == []
    assert _status(cur) == "rejected"
    assert _raw(cur)["saldo_gieldy"] == "nie_do_odczytania"
    assert "NIE DA SIE ODCZYTAC" in msg.upper() or "NIE DA SIĘ ODCZYTAĆ" in msg.upper()


def test_gdy_gielda_potwierdza_DOSC_nie_zmniejszamy_zlecenia():
    """Kanarek: odmowa za saldo nie zawsze dotyczy naszej ilości — środki mogą
    być zablokowane innym zleceniem. Zgadywanie mniejszej liczby byłoby wtedy
    sprzedaniem części pozycji bez żadnego powodu."""
    gielda = Gielda(blad=_blad_z_kodu("170131", "Insufficient balance."),
                    salda=[Balance(currency="SOL", total=D("100"),
                                   available=D("100"))])
    cur, ok, _ = _zloz(gielda)

    assert ok is False and gielda.zlozone == []
    assert _status(cur) == "rejected"
    assert "NIE MNIEJ" in str(cur.zapytania("INSERT INTO exec.order")[0][1])


def test_brak_aktywa_na_liscie_sald_to_ZERO_a_nie_niewiedza():
    """`fetch_balances` pomija monety o zerowym saldzie, więc nieobecność
    aktywa JEST odpowiedzią giełdy. Zwrócenie `None` kazałoby traktować ją jak
    brak odczytu — czyli jak host bez sald."""
    from services.engine.gielda_exec import potwierdzone_saldo

    class BezSol:
        async def fetch_balances(self):
            return [Balance(currency="USDC", total=D("99830"),
                            available=D("99830"))]

    assert asyncio.run(potwierdzone_saldo(BezSol(), "SOL")) == D(0)


# ============================================================== ZNALEZISKO 3
#
# Sprzątanie kasuje dowód, zanim czujka spojrzy.

def _stan_egz(**kw):
    baza = dict(przyjete=0, odrzucone=0, nieznane=0, id_nieznanych=[],
                powody=[], zawieszone_plany=0, bez_kontaktu=0,
                wyjscia_bez_skutku=[])
    return {**baza, **kw}


def test_zlecenie_BEZ_KONTAKTU_z_gielda_jest_zglaszane():
    """Sprzątanie przestawia `unknown` na `cancelled` po ~2 h 45 min, czyli
    PRZED progiem 4 h licznika „o nieznanym stanie" — zmierzone na wszystkich
    czterech zleceniach tej awarii. Brak `venue_order_id` jest jedynym śladem,
    którego sprzątanie nie może nadpisać."""
    powody = ocen_egzekucje(_stan_egz(przyjete=0, bez_kontaktu=4))

    assert powody, (
        "cztery zlecenia, których giełda nigdy nie widziała, nie zgłoszone — "
        "dokładnie ta cisza trwała 19 godzin")
    assert any("BEZ KONTAKTU" in t for _, t, _ in powody)


def test_pozycja_NIE_DO_ZAMKNIECIA_jest_osobna_regula():
    """Reguła niezależna od statusów zleceń: pozycja otwarta, przy której żadna
    sprzedaż się nie wypełniła. Ta jedna złapałaby całą awarię od pierwszego
    dnia — i złapie ją niezależnie od tego, jakim słowem ścieżka błędu zapisze
    swoją porażkę."""
    otwarta = datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc)
    powody = ocen_egzekucje(_stan_egz(
        przyjete=5,
        wyjscia_bez_skutku=[{"symbol": "SOLUSDC", "pozycja": 32,
                             "opened_at": otwarta, "prob": 4,
                             "ostatnia": datetime(2026, 9, 24, 5, 15,
                                                  tzinfo=timezone.utc)}]))

    assert len(powody) == 1
    kod, tytul, tresc = powody[0]
    assert kod == "order.rejected"
    assert "SOLUSDC" in tytul and "32" in tresc


def test_sprzatanie_ZOSTAWIA_SLAD_po_zleceniu_bez_kontaktu():
    """Sam licznik nie wystarczy, jeśli sprzątanie zatrze fakt w wierszu.
    `raw.etap` ma dać się znaleźć gremem, a `reject_reason` — powiedzieć wprost,
    że zlecenie nigdy nie dotarło do giełdy."""
    cur = Kursor([{"id": 53, "client_order_id": "tai-p53", "status": "unknown",
                   "venue_order_id": None, "symbol": "SOLUSDC",
                   "lot_size": D("0.001"), "zaksiegowane": 0}])

    class NieZna:
        async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
            return None

    st = asyncio.run(posprzataj_zlecenia(cur, NieZna(), engine="crypto",
                                         mode="demo", kategoria="spot",
                                         verbose=False))

    assert st["bez_kontaktu"] == 1
    (_, params), = cur.zapytania("UPDATE exec.order SET status='cancelled'")
    assert "NIGDY NIE DOTARŁO" in params[0]
    assert json.loads(params[1])["etap"] == "zamkniete_bez_kontaktu_z_gielda"


def test_sprzatanie_zlecenia_ZNANEGO_gieldzie_nie_jest_bez_kontaktu():
    """Kanarek: licznik ma liczyć brak kontaktu, a nie każde sprzątanie.
    Zlecenie z `venue_order_id` giełda widziała — jego zniknięcie z jej listy
    jest czymś zupełnie innym niż zlecenie, które nigdy do niej nie doszło."""
    cur = Kursor([{"id": 48, "client_order_id": "tai-p36", "status": "unknown",
                   "venue_order_id": "G-48", "symbol": "ETHUSDC",
                   "lot_size": D("0.0001"), "zaksiegowane": 0}])

    class NieZna:
        async def stan_zlecenia(self, id_wlasne, kategoria="spot"):
            return None

    st = asyncio.run(posprzataj_zlecenia(cur, NieZna(), engine="crypto",
                                         mode="demo", kategoria="spot",
                                         verbose=False))

    assert st["domkniete"] == 1 and st["bez_kontaktu"] == 0


def test_licznik_PRZYJETYCH_wymaga_dowodu_kontaktu_z_gielda():
    """`przyjete` liczyło wszystko poza `rejected`/`unknown`, więc `cancelled`
    wpadało do PRZYJĘTYCH. Cztery martwe wyjścia podbijały licznik sukcesu do
    piątki przy zerze odrzuceń — stąd wydruk „bez zastrzeżeń"."""
    zapytania = []

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        zapytania.append(p)
        if "journal.planned_order" in p:
            return [{"n": 0, "ostatni": None}]
        if "max(submitted_at)" in p:
            return [{"ostatnie_zlecenie": None}]
        if "exec.position" in p:
            return []
        if "exec.order" in p and "reject_reason" in p:
            return []
        if "exec.order" in p and "client_order_id" in p:
            return []
        if "exec.order" in p:
            return [{"przyjete": 0, "odrzucone": 0, "nieznane": 0,
                     "bez_kontaktu": 0}]
        return [{"id": 1}]

    async def notify(*a, **k):
        return 1

    from services.engine.dozor import sprawdz_egzekucje
    asyncio.run(sprawdz_egzekucje(fetch_all, notify, sucho=True))

    liczniki = next(p for p in zapytania if "AS przyjete" in p)
    assert "venue_order_id IS NOT NULL" in liczniki, (
        "licznik przyjęć nie wymaga identyfikatora od giełdy — zlecenie, "
        "którego giełda nigdy nie widziała, nadal liczy się jako sukces")
    assert "NOT IN ('rejected','unknown')" not in liczniki
    assert any("exec.position" in p for p in zapytania), (
        "czujka nie pyta o pozycje bez skutecznego wyjścia — reguła "
        "niezależna od statusów zleceń nie istnieje")


# ============================================================== ZNALEZISKO 4
#
# Uzgodnienie melduje zgodność mimo rozjazdu brutto/netto.

def _wiersz_pozycji(*, wypelniono, ilosc=KSIEGI, prowizja=PROWIZJA,
                    prowizja_w="SOL"):
    """Wiersz w kształcie, w jakim oddaje go `zaksiegowane_brutto`.

    `ilosc` (`exec.position.qty`) i `wypelniono` (`exec.fill.qty`) są ROZDZIELONE,
    bo na produkcji rozjechały się naprawdę: ręczna korekta z 2026-09-24 poprawiła
    pozycję i zostawiła wiersz wypełnienia brutto. Atrapa trzymająca je razem nie
    umiałaby odtworzyć ani stanu awarii, ani stanu po naprawie.
    """
    return {"pozycja": 32, "symbol": "SOLUSDC", "aktywo": "SOL",
            "ilosc": ilosc, "zlecono": KSIEGI, "wypelniono": wypelniono,
            "prowizja": prowizja, "prowizja_w": prowizja_w}


def test_pozycja_zaksiegowana_BRUTTO_jest_wykrywana_bez_progu():
    """Różnica brutto/netto ma dokładnie wysokość jednej prowizji, czyli tyle,
    ile tolerancja `rozjazdy` ma z założenia pochłaniać — dla SOLUSDC 0.012138
    przy progu bezwzględnym 0.0427 i tolerancji 0.0607. Dlatego to sprawdzenie
    nie porównuje sum, tylko dwa zapisy tego samego zlecenia w naszych własnych
    księgach: ile wysłaliśmy kontra ile zapisaliśmy jako otrzymane."""
    from services.engine.uzgodnienie import pozycje_brutto

    (b,) = pozycje_brutto([_wiersz_pozycji(wypelniono=KSIEGI)])

    assert b["pozycja"] == 32 and b["aktywo"] == "SOL"
    assert b["nadmiar"] == PROWIZJA


def test_pozycja_zaksiegowana_NETTO_nie_jest_zglaszana():
    """Kanarek: po poprawce `rozlicz_prowizje` wypełnienie jest zapisywane
    netto, więc poprawnie zaksięgowana pozycja musi milczeć — inaczej alarm
    brzmiałby przy każdym wejściu i nauczyłby odbiorcę, że można go pomijać."""
    from services.engine.uzgodnienie import pozycje_brutto

    assert pozycje_brutto([_wiersz_pozycji(wypelniono=RACHUNEK,
                                           ilosc=RACHUNEK)]) == []


def test_prowizja_w_walucie_kwotowanej_nie_rusza_ilosci():
    """Przy sprzedaży (i przy każdej prowizji w USDC) ilość jest nietknięta
    z definicji — nie ma czego odejmować i nie ma tu żadnej wady."""
    from services.engine.uzgodnienie import pozycje_brutto

    assert pozycje_brutto([_wiersz_pozycji(wypelniono=KSIEGI,
                                           prowizja_w="USDC")]) == []


def test_pozycja_JUZ_SKORYGOWANA_przestaje_byc_zglaszana():
    """Stan produkcyjny po ręcznej korekcie z 2026-09-24: `exec.fill.qty` nadal
    jest BRUTTO (12.138), ale `exec.position.qty` to już 12.125862.

    Sprzedaż przechodzi, więc alarmu być nie może — a to jest kanarek na
    najłatwiejszą pomyłkę w tym sprawdzeniu: mierzenie wiersza wypełnienia
    zamiast ilości, którą wyjście naprawdę planuje. Czujka krzycząca o pozycji
    już naprawionej uczy odbiorcę, że można ją pomijać."""
    from services.engine.uzgodnienie import pozycje_brutto

    assert pozycje_brutto([_wiersz_pozycji(wypelniono=KSIEGI,
                                           ilosc=RACHUNEK)]) == []


def test_wypelnienie_CZESCIOWE_nie_jest_zgadywane():
    """Granica tej metody, powiedziana wprost: przy wypełnieniu częściowym
    `wypelniono < zlecono` z natury rzeczy, więc te dwie liczby nie rozstrzygają
    niczego o prowizji. Milczymy zamiast zgadywać."""
    from services.engine.uzgodnienie import pozycje_brutto

    assert pozycje_brutto([_wiersz_pozycji(wypelniono=D("6.0"),
                                           ilosc=D("6.0"))]) == []


def _fetch_uzgodnienia(brutto_rows):
    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        if "AS prowizja_w" in p:
            return list(brutto_rows)
        if "sum(p.qty)" in p:
            return [{"aktywo": "SOL", "ilosc": KSIEGI}]
        if "DISTINCT base_ccy" in p:
            return [{"base_ccy": "SOL"}]
        if "min(i.lot_size)" in p:
            return [{"aktywo": "SOL", "lot": D("0.001"),
                     "min_wartosc": D("5"), "cena": D("117.24")}]
        if 'app."user"' in p:
            return [{"id": 1}]
        return []
    return fetch_all


class _Salda:
    async def fetch_balances(self):
        # Gielda POTWIERDZA netto — czyli dokladnie stan produkcyjny.
        return [Balance(currency="SOL", total=RACHUNEK, available=RACHUNEK)]


def test_uzgodnienie_ZGLASZA_pozycje_brutto_choc_rozjazd_sum_milczy(capsys):
    """Sedno znaleziska 4. Same sumy (12.138 kontra 12.125862) przechodzą pod
    oboma progami, więc `rozjazdy` nie ma prawa nic powiedzieć — i nie mówi.
    Uzgodnienie musi mimo to zgłosić awarię, bo pozycja jest nie do sprzedania."""
    zgloszone = []

    async def notify(uid, kod, tytul, tresc, **kw):
        zgloszone.append((kod, tresc))

    n = asyncio.run(uzgodnij(_fetch_uzgodnienia([_wiersz_pozycji(wypelniono=KSIEGI)]),
                             notify, _Salda()))

    assert n == 1, (
        "uzgodnienie zameldowało zgodność, mając przed sobą pozycję, której "
        "giełda odmawia sprzedaży od 19 godzin")
    assert zgloszone and "pozycja-brutto" in zgloszone[0][1]
    assert "POZYCJA BRUTTO" in capsys.readouterr().out


def test_uzgodnienie_MILCZY_gdy_pozycja_jest_netto():
    """Kanarek do poprzedniego: sprawdzenie ma wyłapać wadę, a nie zgłaszać
    każdej otwartej pozycji."""
    async def notify(*a, **k):
        raise AssertionError("poprawnie zaksięgowana pozycja zgłoszona jako awaria")

    assert asyncio.run(uzgodnij(
        _fetch_uzgodnienia([_wiersz_pozycji(wypelniono=RACHUNEK,
                                            ilosc=RACHUNEK)]),
        notify, _Salda())) == 0
