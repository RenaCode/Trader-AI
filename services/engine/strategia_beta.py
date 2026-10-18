"""Regula "beta z filtrem trendu" na SPY, konto equity DEMO. ALOKACJA, NIE PRZEWAGA.

Uzasadnienie: renacode-infra docs/projekty/trader/hipotezy.md (D-3, H-16).

CO ROBI. Regula Fabera (2007): w miesiacu M 95% kapitalu w SPY, gdy
zamkniecie miesiaca M-1 (X) jest powyzej sredniej z 10 zamkniec miesiecznych
M-10..M-1 (SMA, z X wlacznie), inaczej gotowka. Zamkniecie miesieczne to
zamkniecie ostatniej sesji miesiaca (czas NY), z ostatniej swiecy 1h sesji
regularnej (`market.candle`, godziny z `sesja.py`). Dane z miesiaca M nie sa
czytane wcale, wiec cel na caly miesiac nie moze zajrzec w przyszlosc.

DLACZEGO ALOKACJA, A NIE PRZEWAGA. Regula swiadomie trzyma bete rynku;
filtr trendu ma tylko ograniczac glebokie obsuniecia kosztem czesci wzrostu
i "pily" na rynku bocznym. Okresy pod kreska wzgledem samego SPY sa stanem
oczekiwanym, nie awaria - tak jak E-1 (`strategia_momentum.py`) byl pomiarem
egzekucji, a nie hipoteza przewagi.

PARAMETRY SA ZAMROZONE (`PARAMETRY`; `tests/test_strategia_beta.py` przypina
liczby). Zmiana to nowa decyzja w rejestrze, nie poprawka tego pliku.

W PETLI (`loop.run_once --strategia beta`, tylko demo, model nieczytany,
`journal.decision.strategia` = `beta_filtr_trendu`, migracja 0100):

  * wejscie przy celu "w rynku" i braku pozycji - WYLACZNIE na pierwszej
    swiecy sesji dnia (swieca z otwarciem 9:30 NY). Niewypelnione wejscie
    ponawia sie nastepnego dnia sesji, zamiast zostawiac gotowke na miesiac;
  * wyjscie przy celu "gotowka" na KAZDEJ swiecy, az wyjdzie; pozycja krotka
    i pozycja w innym instrumencie (pozostalosc po ensemble) wychodza zawsze;
  * 95% kapitalu (bufor na roznice ceny limitu i zaokraglenie), CALE akcje
    w dol, bez dorownywania w trakcie miesiaca.

SPY PO SYMBOLU, NIE Z UNIWERSUM. SPY ma `tradeable = false` (0047/0060)
i tak zostaje: `tradeable` wyznacza odcisk uniwersum ensemble
(`uniwersum.odcisk`), czyli nazwy cech, na ktorych liczy trener. Regula
bierze SPY z `ref.instrument` po symbolu oraz kazdy instrument z otwarta
pozycja na tym koncie.

ZNANE ODSTEPSTWA OD POSTACI BADANEJ: wejscie/wyjscie ok. 10:00 NY pierwszej
sesji po decyzji, nie na zamknieciu miesiaca; zamkniecie z feedu IEX, nie
z aukcji; w dni skrocone (13:00 NY) kalendarz `sesja.py` nie zna wczesniejszego
zamkniecia i "zamkniecie" moze byc notowaniem posesyjnym sprzed 16:00.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from types import MappingProxyType

from services.engine.horizons import Horizon
from services.engine.risk_rules import GROSZ
from services.engine.sesja import ZAMKNIECIE_SESJI_US, STREFA_US, otwarcie_sesji_us
from services.exchanges.fabryka import OBSLUGIWANE

NAZWA = "beta_filtr_trendu"
OPIS = "beta z filtrem trendu (SPY, SMA 10 mies.)"

# ZAMROZONE (D-3, H-16). Widok tylko do odczytu - test przypina liczby.
PARAMETRY = MappingProxyType({
    "symbol": "SPY",
    "okno_sma_miesiecy": 10,
    "udzial_kapitalu": "0.95",
})
SYMBOL = PARAMETRY["symbol"]
OKNO_SMA = PARAMETRY["okno_sma_miesiecy"]
UDZIAL = Decimal(PARAMETRY["udzial_kapitalu"])

# Swieca, z ktorej licza sie zamkniecia dzienne. Zrodlo stale (1h) niezaleznie
# od horyzontu petli - sygnal miesieczny nie moze zmieniac sie od tego, jaka
# swieca decyduje petla.
TIMEFRAME_ZRODLA = "1h"
MINUT_ZRODLA = 60

# Historia do SMA: 10 pelnych miesiecy przed miesiacem decyzji to najwyzej
# ~310 dni; 400 daje zapas na swieta i brak danych na poczatku okna, a nie
# zmienia wyniku (liczy sie tylko 10 ostatnich zamkniec miesiecznych).
HISTORIA_DNI = 400

# Ostatnia sesja miesiaca wypada najdalej 3 dni przed jego koncem (31 maja
# w poniedzialek = Memorial Day -> piatek 28; Wielki Piatek 29 marca przy
# weekendzie 30-31 -> czwartek 28). Zamkniecie "miesieczne" starsze niz
# 4 dni przed koncem miesiaca to dziura w danych, nie ostatnia sesja -
# taki miesiac traktujemy jak brakujacy, zamiast liczyc SMA ze zlej ceny.
MAKS_LUKA_KONCA_MIESIACA_DNI = 4

# Powody holdu wlasciwe tej regule (CHECK w migracji 0100).
POWOD_FILTR = "filtr_trendu_spy"
POWOD_HISTORIA = "historia_filtra_niepelna"
POWOD_CZEKA = "czeka_na_sesje"
POWOD_POZA = "poza_instrumentem_reguly"
POWODY_HOLDU = (POWOD_FILTR, POWOD_HISTORIA, POWOD_CZEKA, POWOD_POZA)


# ------------------------------------------------------------ kalendarz

def swieca_sesji(t: datetime, minut: int = MINUT_ZRODLA) -> bool:
    """Czy swieca [t, t+minut) naklada sie na sesje regularna NYSE. CZYSTA.

    Swieca 9:00-10:00 NY zawiera otwarcie (9:30), 15:00-16:00 - zamkniecie.
    Swieta: swiec sesji nie ma wcale, wiec nie ma czego odfiltrowac.
    """
    ny = t.astimezone(STREFA_US)
    if ny.weekday() >= 5:
        return False
    zamkniecie = datetime.combine(ny.date(), ZAMKNIECIE_SESJI_US, tzinfo=STREFA_US)
    return t < zamkniecie and t + timedelta(minutes=minut) > otwarcie_sesji_us(ny.date())


def swieca_decyzyjna(bar_start: datetime, h: Horizon) -> bool:
    """Czy to PIERWSZA swieca sesji dnia - jedyna, na ktorej wolno wejsc. CZYSTA.

    Pierwsza = ta, ktora zawiera otwarcie 9:30 NY (przy 1h: 13:00-14:00 UTC
    latem, 14:00-15:00 zima - strefa z bazy IANA, jak w `sesja.py`). Decyzja
    zapada po jej zamknieciu, czyli w sesji, wiec `swieca_przed_sesja` jej nie
    dotyczy. Swieto: zegar gieldy zamyka bramke sesji i decyzji nie ma wcale.
    """
    ny = bar_start.astimezone(STREFA_US)
    if ny.weekday() >= 5:
        return False
    otwarcie = otwarcie_sesji_us(ny.date())
    return bar_start <= otwarcie < bar_start + timedelta(minutes=h.bar_minutes)


def miesiac_decyzji(bar_start: datetime) -> date:
    """Pierwszy dzien miesiaca (NY), w ktorym zapada decyzja dla tej swiecy."""
    return bar_start.astimezone(STREFA_US).date().replace(day=1)


def poczatek_miesiaca_utc(miesiac: date) -> datetime:
    """Polnoc NY pierwszego dnia miesiaca, w UTC - granica "dane przed M"."""
    return datetime.combine(miesiac, time(0), tzinfo=STREFA_US).astimezone(timezone.utc)


def poprzednie_miesiace(miesiac: date, n: int) -> list[date]:
    """n miesiecy przed `miesiac` (pierwsze dni), od najstarszego. CZYSTA."""
    wynik, r, m = [], miesiac.year, miesiac.month
    for _ in range(n):
        r, m = (r, m - 1) if m > 1 else (r - 1, 12)
        wynik.append(date(r, m, 1))
    return wynik[::-1]


def ostatni_dzien(miesiac: date) -> date:
    nastepny = (miesiac.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nastepny - timedelta(days=1)


# ------------------------------------------------------------ sygnal

def zamkniecia_dzienne(swiece: list[dict]) -> list[tuple[date, float]]:
    """[(dzien NY, zamkniecie ostatniej swiecy sesji)] rosnaco. CZYSTA FUNKCJA.

    `swiece`: [{"t": poczatek swiecy 1h (UTC), "close": float}]. Swiece przed-
    i posesyjne (Alpaca je oddaje, patrz `sesja.py`) sa pomijane - zamkniecie
    dnia to ostatnia swieca nakladajaca sie na 9:30-16:00 NY.
    """
    dni: dict[date, tuple[datetime, float]] = {}
    for s in swiece:
        if s.get("close") is None or not swieca_sesji(s["t"]):
            continue
        d = s["t"].astimezone(STREFA_US).date()
        if d not in dni or s["t"] > dni[d][0]:
            dni[d] = (s["t"], float(s["close"]))
    return [(d, dni[d][1]) for d in sorted(dni)]


def zamkniecia_miesieczne(dzienne: list[tuple[date, float]]) -> dict[date, tuple[date, float]]:
    """{pierwszy dzien miesiaca: (dzien ostatniej sesji, zamkniecie)}. CZYSTA."""
    wynik: dict[date, tuple[date, float]] = {}
    for d, c in dzienne:
        m = d.replace(day=1)
        if m not in wynik or d > wynik[m][0]:
            wynik[m] = (d, c)
    return wynik


@dataclass(frozen=True)
class Cel:
    """Cel na miesiac decyzji i wszystko, co trafia do dziennika."""
    miesiac: date                                  # pierwszy dzien miesiaca M
    w_rynku: bool
    zamkniecia: tuple[tuple[date, float], ...] = ()   # M-10..M-1 (gdy komplet)
    zamkniecie: float | None = None                # X = zamkniecie M-1
    sma: float | None = None
    powod: str = ""
    symbol: str = SYMBOL
    # Interfejs reguly w `loop.run_once` (jak `strategia_momentum.Cel`):
    # cel tej reguly nie czeka na niedomknieta swiece - zawsze kompletny.
    kompletny: bool = True

    @property
    def chwila(self) -> datetime:
        return poczatek_miesiaca_utc(self.miesiac)

    @property
    def sygnaly(self) -> dict[str, float]:
        """Odchylenie X od SMA - do kolejnosci rozpatrywania i dziennika."""
        if self.sma is None or self.zamkniecie is None or self.sma <= 0:
            return {}
        return {self.symbol: self.zamkniecie / self.sma - 1.0}

    def snapshot(self, symbol: str) -> dict[str, float]:
        """`feature_snapshot` decyzji: dokladnie te liczby, na ktorych zapadla."""
        s = {"beta_w_rynku": 1.0 if self.w_rynku else 0.0,
             "beta_instrument_reguly": 1.0 if symbol == self.symbol else 0.0,
             "beta_zamkniec_miesiecznych": float(len(self.zamkniecia))}
        if self.zamkniecie is not None:
            s["beta_spy_zamkniecie_mies"] = self.zamkniecie
        if self.sma is not None:
            s["beta_spy_sma_10m"] = self.sma
            s["beta_odchylenie"] = self.sygnaly.get(self.symbol, 0.0)
        return s


def wyznacz_cel(swiece: list[dict], miesiac: date, symbol: str = SYMBOL) -> Cel:
    """Cel na miesiac `miesiac` ze swiec 1h `symbol`. CZYSTA FUNKCJA.

    Swiece z miesiaca `miesiac` i pozniejsze sa ODRZUCANE tutaj, a nie tylko
    w zapytaniu - wynik nie moze zalezec od tego, czy wolajacy je przycial.
    Brak ktoregokolwiek z 10 miesiecy (albo dziura na koncu miesiaca) = cel
    gotowka z powodem: SMA z dziewieciu zamkniec albo z ceny sprzed dziury to
    inna regula niz zmierzona.
    """
    granica = poczatek_miesiaca_utc(miesiac)
    przeszle = [s for s in swiece if s["t"] + timedelta(minutes=MINUT_ZRODLA) <= granica]
    miesieczne = zamkniecia_miesieczne(zamkniecia_dzienne(przeszle))
    potrzebne = poprzednie_miesiace(miesiac, OKNO_SMA)
    brak = []
    for m in potrzebne:
        z = miesieczne.get(m)
        if z is None or (ostatni_dzien(m) - z[0]).days > MAKS_LUKA_KONCA_MIESIACA_DNI:
            brak.append(m)
    if brak:
        return Cel(miesiac, False, symbol=symbol, powod=(
            f"za mało historii {symbol}: brak zamknięcia ostatniej sesji "
            f"{len(brak)} z {OKNO_SMA} miesięcy "
            f"({', '.join(f'{m:%Y-%m}' for m in brak)}) — cel = gotówka"))
    zamkniecia = tuple(miesieczne[m] for m in potrzebne)
    x = zamkniecia[-1][1]
    sma = sum(c for _, c in zamkniecia) / OKNO_SMA
    return Cel(miesiac, x > sma, zamkniecia, x, sma, symbol=symbol)


def akcja(cel: Cel, pozycja: dict | None, symbol: str,
          decyzyjna: bool) -> tuple[str, str | None]:
    """(akcja, powod_holdu) dla jednego instrumentu. CZYSTA FUNKCJA.

    Ten sam podpis co `strategia_momentum.akcja` - petla wola jedno albo drugie.
    Pozycja trzymana tylko wtedy, gdy jest dluga, w SPY i cel jest "w rynku";
    kazda inna wychodzi na kazdej swiecy, az wyjdzie.
    """
    if pozycja is not None:
        if pozycja.get("side") == "long" and symbol == cel.symbol and cel.w_rynku:
            return "hold", "trzyma_pozycje"
        return "exit", None
    if symbol != cel.symbol:
        return "hold", POWOD_POZA
    if not cel.w_rynku:
        return "hold", POWOD_HISTORIA if cel.sma is None else POWOD_FILTR
    if not decyzyjna:
        return "hold", POWOD_CZEKA
    return "enter_long", None


# ------------------------------------------------------------ budzet

def sufit(kapital: Decimal) -> Decimal:
    return kapital * UDZIAL


def budzet(kapital: Decimal) -> Decimal:
    """95% kapitalu w DOL do grosza."""
    return sufit(kapital).quantize(GROSZ, rounding=ROUND_DOWN)


def miesci_sie(kapital: Decimal, ekspozycja: Decimal, kwota: Decimal) -> bool:
    """Wlasny sufit reguly: 95% kapitalu NIEZALEZNIE od `risk.limit_def`.

    Limity konta equity/demo (0100: 97%) sa luzniejsze o bufor; budzet reguly
    jest czescia jej definicji i nie rosnie razem z limitem.
    """
    return ekspozycja + kwota <= sufit(kapital)


def wielkosc_wejscia(kapital: Decimal, ekspozycja: Decimal,
                     cena: Decimal) -> tuple[Decimal, Decimal]:
    """(notional, qty) wejscia: reszta do 95% kapitalu w CALYCH akcjach, w dol.

    CZYSTA FUNKCJA. (0, 0), gdy reszta nie kupi ani jednej akcji. Ekspozycja
    to wartosc rynkowa pozycji i zlecen (jak `max_total_exposure`), wiec
    pozostalosc po ensemble, ktora jeszcze nie wyszla, zmniejsza wejscie
    zamiast przebijac sufit.
    """
    c = cena if isinstance(cena, Decimal) else Decimal(str(cena))
    reszta = (sufit(kapital) - ekspozycja).quantize(GROSZ, rounding=ROUND_DOWN)
    if reszta <= 0 or c <= 0:
        return Decimal(0), Decimal(0)
    qty = (reszta / c).to_integral_value(rounding=ROUND_DOWN)
    if qty <= 0:
        return Decimal(0), Decimal(0)
    return (qty * c).quantize(GROSZ, rounding=ROUND_DOWN), qty


def uzasadnienie(cel: Cel, symbol: str) -> str:
    """Pierwsze zdania `rationale` - liczby, na ktorych zapadla decyzja."""
    if cel.sma is not None:
        znak = ">" if cel.w_rynku else "≤"
        filtr = (f"{cel.symbol}: zamknięcie {cel.zamkniecia[-1][0]:%Y-%m-%d} "
                 f"{cel.zamkniecie:.2f} {znak} SMA {OKNO_SMA} mies. {cel.sma:.2f} "
                 f"({cel.zamkniecia[0][0]:%Y-%m}…{cel.zamkniecia[-1][0]:%Y-%m})")
    else:
        filtr = f"filtr: {cel.powod}"
    stan = (f"cel {UDZIAL:.0%} kapitału w {cel.symbol}" if cel.w_rynku
            else "cel gotówka")
    poza = ("" if symbol == cel.symbol else
            f" {symbol} poza regułą — pozycja wychodzi.")
    return (f"Reguła {OPIS} (D-3; alokacja, nie hipoteza przewagi; parametry "
            f"zamrożone). Miesiąc decyzji {cel.miesiac:%Y-%m}: {filtr}; {stan}.{poza}")


def odmowa_budzetu(kapital: Decimal, ekspozycja: Decimal | None,
                   cena: Decimal) -> str:
    """Powod `block` wlasnego sufitu - ten sam ksztalt co odmowa momentum."""
    reszta = sufit(kapital) - ekspozycja if ekspozycja is not None else Decimal(0)
    return (f"Budżet reguły {OPIS}: reszta do sufitu {UDZIAL:.0%} kapitału "
            f"({sufit(kapital):.0f}) to {max(reszta, Decimal(0)):.2f}, nie kupi "
            f"jednej akcji po {cena}")


# --------------------------------------------------------------- odczyt z bazy

# Instrumenty reguly: SPY po symbolu (aktywny, na gieldzie z adapterem - nie
# placeholder `ibkr` z 0008) ORAZ kazdy instrument z otwarta pozycja tego
# konta. Bez drugiej czesci pozostalosc po ensemble nie bylaby rozpatrywana
# i zostalaby na koncie na zawsze.
SQL_INSTRUMENTY = """
SELECT i.id, i.symbol, i.kind, i.quote_ccy, i.lot_size
  FROM ref.instrument i
  JOIN ref.venue v ON v.id = i.venue_id
 WHERE i.engine = %(engine)s
   AND ((i.symbol = %(symbol)s AND i.active AND v.code = ANY(%(gieldy)s))
        OR i.id IN (SELECT p.instrument_id FROM exec.position p
                     WHERE p.engine = %(engine)s AND p.mode = %(mode)s
                       AND p.closed_at IS NULL))
 ORDER BY i.symbol, i.id
"""

SQL_SWIECE = """
SELECT event_time AS t, close
  FROM market.candle
 WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
   AND event_time >= %(od)s AND event_time < %(do)s
 ORDER BY event_time
"""


def instrumenty_reguly(cur, engine: str, mode: str) -> list[dict]:
    cur.execute(SQL_INSTRUMENTY, {"engine": engine, "mode": mode, "symbol": SYMBOL,
                                  "gieldy": list(OBSLUGIWANE)})
    return cur.fetchall()


def swiece(cur, instrument_id: int, od: datetime, do: datetime) -> list[dict]:
    cur.execute(SQL_SWIECE, {"iid": instrument_id, "tf": TIMEFRAME_ZRODLA,
                             "od": od, "do": do})
    return [{"t": r["t"], "close": float(r["close"])}
            for r in cur.fetchall() if r["close"] is not None]


def cel_z_bazy(cur, instrumenty: list[dict], bar_start: datetime,
               h: Horizon, czekaj: bool = True) -> Cel:
    """Cel na miesiac swiecy `bar_start` z `market.candle`.

    Podpis jak `strategia_momentum.cel_z_bazy`; `czekaj` nie ma tu znaczenia -
    zamkniecie poprzedniego miesiaca jest w bazie od wielu godzin, zanim
    zapadnie pierwsza decyzja nowego miesiaca. Zapytanie konczy sie na
    polnocy NY pierwszego dnia miesiaca decyzji.
    """
    miesiac = miesiac_decyzji(bar_start)
    spy = next((i for i in instrumenty if i["symbol"] == SYMBOL), None)
    if spy is None:
        return Cel(miesiac, False, powod=(
            f"brak {SYMBOL} w ref.instrument (aktywny, giełda z adapterem) — "
            f"filtr nieznany, cel = gotówka"))
    do = poczatek_miesiaca_utc(miesiac)
    return wyznacz_cel(swiece(cur, spy["id"], do - timedelta(days=HISTORIA_DNI), do),
                       miesiac)
