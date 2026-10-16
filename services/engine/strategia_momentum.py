"""Strategia "momentum + filtr trendu" na koncie DEMO. POMIAR EGZEKUCJI, NIE PRZEWAGA.

DLACZEGO (decyzja wlasciciela "A", 2026-10-04, renacode-infra: docs/projekty/trader/hipotezy.md)

Poszukiwanie przewagi jest zamkniete wnioskiem "brak przewagi spelniajacej
kryteria": H-1 (ensemble 4h) STOP przy -11,2 pb na runde pod pesymistycznym
modelem wypelnien, H-3 (R4, ta regula) STOP - +155..+188 pb na runde po
kosztach, ale zysk z kilku miesiecy hossy altow: okna dodatnie 46% przy progu
65%, bije B2 w 44% okien przy progu 55%.

ZASTRZEZENIE (przeglad 2026-10-04, T-11): wszechswiat H-3 to dzisiejsze pary
USDC zastosowane wstecz (survivorship), wiec +155..+188 pb to GORNE
oszacowanie, nie pomiar - szczegoly w renacode-infra: docs/projekty/trader/hipotezy.md.

Na demo chodzi wiec prosta, jawnie nazwana regula z ZAMROZONYMI parametrami
R4, po to, zeby zmierzyc EGZEKUCJE: wypelnialnosc wejsc, poslizg, opoznienie
i prowizje wzgledem modelu pesymistycznego, pod ktorym liczono H-1 i H-3
(`services/engine/poslizg.py --strategia momentum_filtr_trendu`). Przez wiekszosc
miesiecy ma byc pod kreska i to jest stan oczekiwany, nie awaria.

PARAMETRY SA ZAMROZONE. Liczby to `r4_momentum.PARAMETRY_R4`, czyli dokladnie
te, ktore zmierzyl przebieg H-3 (899ec9f, wynik 5952e87): momentum 30 d bez
ostatniej doby, top 3, filtr BTC > EMA 50 d, decyzja na zamknieciu doby UTC.
Budzet to wariant P z H-3: 15% kapitalu, 5% na pozycje. Test
`tests/test_strategia_momentum.py` przypina te wartosci - zmiana ktorejkolwiek
to nowa hipoteza z wpisem w rejestrze, a nie poprawka tego pliku.

CO ROBI W PETLI (`loop.run_once --strategia momentum`)

Sklad celu liczy sie z zamkniec swiec 4h znanych w chwili OSTATNIEJ polnocy UTC
- na kazdej swiecy dnia ten sam, bo wejscie jest to samo. Z niego wynika akcja
(`akcja`): pozycja spoza skladu wychodzi na kazdej swiecy, az wyjdzie (wyjscia
nie czekaja doby, tak jak w R4 wyjscie zawsze dochodzi do skutku); wejscie
WYLACZNIE na swiecy konczacej dobe - niewypelnione przepada do nastepnej
decyzji dobowej, tak jak w modelu H-3. Reszta petli bez zmian: bramki konta,
kill-switch, szeroki spread, `decyzja_przeterminowana`, risk kernel, ta sama
egzekucja PostOnly z wyjsciem takerem po odmowie (T6).

STRATEGIA NIE ZALEZY OD MODELU. Wersja modelu (aktywna v39 i `candidate`
z tygodniowego treningu) nie jest czytana; `journal.decision.model_version_id`
zostaje NULL, a `strategia` mowi, ktora regula podjela decyzje (migracja 0091).

ODSTEPSTWA OD SYMULACJI H-3 (znane, nazwane, nie korygowane):

  * cena limitu i zywotnosc zlecenia sa produkcyjne (`gielda_exec`), nie
    "cena 20 min po decyzji - pol spreadu, godzina" - roznice mierzy wlasnie
    ten przebieg;
  * wejscie w instrument z szerokim spreadem jest blokowane (F-1.5) - R4
    trzymalo wtedy pelny sklad, tu pozycji jest mniej;
  * wyjscie z rotacji zwalnia ekspozycje dopiero po wypelnieniu, wiec wejscie
    tego samego cyklu moze odbic sie od sufitu 15% i przepasc do jutra;
  * pozycje otwarte przez ensemble przed przelaczeniem sa traktowane jak kazde
    inne: spoza skladu wychodza, w skladzie zostaja;
  * sufit 15% liczony po WARTOSCI RYNKOWEJ (jak `max_total_exposure`
    w kernelu), wiec po wzroscie trzymanych pozycji kolejne wejscie jest
    MNIEJSZE niz 5% - reszta do 15% (`kwota_wejscia`) - albo nie nastepuje,
    gdy reszta < 1% kapitalu. H-3 wchodzilo stalym nominalem bez sufitu.
    Zmiana z 2026-10-04 po przegladzie (T-8): poprzednia wersja wchodzila
    tylko wtedy, gdy trzymane pozycje byly warte nie wiecej niz koszt, czyli
    wejscia dochodzily do skutku glownie po stratach.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from types import MappingProxyType

from services.engine.baseline import _ema, _zamkniecia_dobowe
from services.engine.horizons import Horizon
from services.engine.r4_momentum import PARAMETRY_R4, WARIANTY, momentum, ranking
from services.engine.risk_rules import GROSZ

NAZWA = "momentum_filtr_trendu"
OPIS = "momentum + filtr trendu"

# ZAMROZONE (H-3). Widok tylko do odczytu na kopie - nikt nie podmieni liczby
# w trakcie dzialania procesu, a zmiana w r4_momentum.py wywraca test.
PARAMETRY = MappingProxyType(dict(PARAMETRY_R4))

# Wariant P z H-3: laczna ekspozycja 15% kapitalu, rowne wagi przy wejsciu.
UDZIAL_LACZNY = Decimal(str(WARIANTY["P"]))
UDZIAL_POZYCJI = UDZIAL_LACZNY / PARAMETRY["top"]

# Historia BTC do EMA 50 d. EMA startuje od sredniej pierwszych 50 zamkniec
# (`baseline._ema`), wiec wynik zalezy od dlugosci historii. Po 350 dobach
# waga startu to (49/51)^350 ~ 1e-6 - ponizej kazdej roznicy, ktora zmienilaby
# znak filtra. H-3 liczylo na calym panelu (1400 dni), wiec krotsza historia
# dawalaby INNY filtr niz zmierzony.
HISTORIA_FILTRA_DNI = 400

# Powody holdu wlasciwe tej strategii (CHECK w migracji 0091).
POWOD_FILTR = "filtr_trendu"
POWOD_POZA_SKLADEM = "poza_skladem"
POWOD_POZA_DECYZJA = "poza_decyzja_dobowa"
POWODY_HOLDU = (POWOD_FILTR, POWOD_POZA_SKLADEM, POWOD_POZA_DECYZJA)

DOBA = timedelta(days=1)


def koniec_swiecy(bar_start: datetime, h: Horizon) -> datetime:
    return bar_start + timedelta(minutes=h.bar_minutes)


def swieca_decyzyjna(bar_start: datetime, h: Horizon) -> bool:
    """Czy swieca zamyka dobe UTC - jedyna, na ktorej wolno WEJSC. CZYSTA."""
    k = koniec_swiecy(bar_start, h)
    return k.hour == 0 and k.minute == 0


def chwila_decyzji(bar_start: datetime, h: Horizon) -> datetime:
    """Ostatnia polnoc UTC nie pozniejsza niz zamkniecie swiecy. CZYSTA."""
    return koniec_swiecy(bar_start, h).replace(hour=0, minute=0, second=0,
                                               microsecond=0)


@dataclass(frozen=True)
class Cel:
    """Sklad docelowy z decyzji dobowej i wszystko, co trafia do dziennika."""
    chwila: datetime
    kompletny: bool                    # False = swieca BTC jeszcze niedomknieta
    filtr: bool = False                # BTC > EMA 50 d
    sklad: tuple[str, ...] = ()
    sygnaly: dict[str, float] = field(default_factory=dict)
    btc: str | None = None
    btc_zamkniecie: float | None = None
    btc_ema: float | None = None
    powod: str = ""

    def ranga(self, symbol: str) -> int | None:
        kolejnosc = ranking(self.sygnaly, len(self.sygnaly))
        return kolejnosc.index(symbol) + 1 if symbol in kolejnosc else None

    def snapshot(self, symbol: str) -> dict[str, float]:
        """`feature_snapshot` decyzji: dokladnie te liczby, na ktorych zapadla."""
        s = {"r4_filtr_btc": 1.0 if self.filtr else 0.0,
             "r4_w_skladzie": 1.0 if symbol in self.sklad else 0.0}
        if symbol in self.sygnaly:
            s["r4_momentum_30d"] = self.sygnaly[symbol]
            s["r4_ranga"] = float(self.ranga(symbol))
        if self.btc_zamkniecie is not None:
            s["r4_btc_zamkniecie"] = self.btc_zamkniecie
        if self.btc_ema is not None:
            s["r4_btc_ema50"] = self.btc_ema
        return s


def symbol_btc(symbole) -> str | None:
    """Ten sam wybor co pomiar H-3 (`r4_momentum.pomiar`)."""
    return next((s for s in sorted(symbole) if s.startswith("BTC")), None)


def wyznacz_cel(panele: dict[str, list[dict]], chwila: datetime,
                bar_minutes: int = 240) -> Cel:
    """Sklad R4 na chwile `chwila` (polnoc UTC). CZYSTA FUNKCJA.

    `panele`: symbol -> [{"t": poczatek swiecy 4h, "price": zamkniecie}],
    wylacznie swiece domkniete najpozniej o `chwila`. Te same funkcje co H-3:
    `r4_momentum.momentum`, `ranking`, a filtr to `baseline._trend_w_gore`
    rozpisane na czesci, zeby zamkniecie i EMA trafily do dziennika.
    """
    btc = symbol_btc(panele)
    zam = (_zamkniecia_dobowe(panele[btc], chwila, bar_minutes) if btc else [])
    ema = _ema(zam, PARAMETRY["ema_filtra_dni"])
    filtr = ema is not None and zam[-1] > ema
    indeksy = {s: {r["t"]: r for r in rows} for s, rows in panele.items()}
    sygnaly = {}
    for s, idx in indeksy.items():
        m = momentum(idx, chwila, bar_minutes, PARAMETRY)
        if m is not None:
            sygnaly[s] = m
    sklad = tuple(ranking(sygnaly, PARAMETRY["top"])) if filtr else ()
    if btc is None:
        powod = "brak BTC w uniwersum — filtr nieznany, cel = gotówka"
    elif ema is None:
        powod = (f"za krótka historia BTC ({len(zam)} zamknięć dobowych "
                 f"< {PARAMETRY['ema_filtra_dni']}) — cel = gotówka")
    else:
        powod = ""
    return Cel(chwila, True, filtr, sklad, sygnaly, btc,
               zam[-1] if zam else None, ema, powod)


def akcja(cel: Cel, pozycja: dict | None, symbol: str,
          decyzyjna: bool) -> tuple[str, str | None]:
    """(akcja, powod_holdu) dla jednego instrumentu. CZYSTA FUNKCJA.

    Pozycja spoza skladu wychodzi na KAZDEJ swiecy: wyjscie, ktore sie nie
    wypelnilo, jest ponawiane, zamiast czekac doby. Wejscie tylko na swiecy
    decyzyjnej (H-3: niewypelnione wejscie przepada do nastepnej decyzji).
    Long-only - pozycja krotka nie ma w tej strategii prawa bytu, wiec wychodzi.
    """
    if pozycja is not None:
        if pozycja.get("side") == "long" and symbol in cel.sklad:
            return "hold", "trzyma_pozycje"
        return "exit", None
    if not cel.filtr:
        return "hold", POWOD_FILTR
    if symbol not in cel.sklad:
        return "hold", POWOD_POZA_SKLADEM
    if not decyzyjna:
        return "hold", POWOD_POZA_DECYZJA
    return "enter_long", None


def budzet_pozycji(kapital: Decimal) -> Decimal:
    """Kwota jednego wejscia: 15% / 3 kapitalu, w DOL do grosza.

    W dol, bo trzy wejscia zaokraglone w gore przekroczylyby sufit o grosz
    i trzecie odbiloby sie od `max_total_exposure` (15%, `block`).
    """
    return (kapital * UDZIAL_POZYCJI).quantize(GROSZ, rounding=ROUND_DOWN)


def sufit(kapital: Decimal) -> Decimal:
    return kapital * UDZIAL_LACZNY


def miesci_sie(kapital: Decimal, ekspozycja: Decimal, kwota: Decimal) -> bool:
    """Wlasny sufit strategii: 15% kapitalu NIEZALEZNIE od `risk.limit_def`.

    Limit `max_total_exposure` jest dzis tez 15%, ale to wiersz w bazie, ktory
    wolno zmienic bez zmiany kodu. Budzet tej strategii jest czescia pomiaru
    (wariant P z H-3), wiec nie moze rosnac razem z limitem konta.
    """
    return ekspozycja + kwota <= sufit(kapital)


# Najmniejsze wejscie jako ulamek budzetu pozycji: 0,2 * 5% = 1% kapitalu.
# Reszta do sufitu ponizej tego to pyl - prowizja i krok ilosci zjadaja ja
# w wiekszej czesci niz pelne wejscie, a pomiar egzekucji nic z niej nie ma.
PODLOGA_RESZTY = Decimal("0.2")


def kwota_wejscia(kapital: Decimal, ekspozycja: Decimal) -> Decimal:
    """Kwota wejscia: budzet pozycji, a gdy sufit na to nie pozwala - reszta
    do sufitu, o ile nie jest pylem. 0 = nie wchodzimy. CZYSTA FUNKCJA.

    Sufit zostaje twardy i liczony po wartosci rynkowej, tak jak w kernelu -
    inaczej kernel i tak by zablokowal. Zmienia sie tylko wielkosc wejscia:
    zamiast "dokladnie 5% albo nic" - "5%, albo reszta do 15%" (T-8).
    """
    budzet = budzet_pozycji(kapital)
    reszta = (sufit(kapital) - ekspozycja).quantize(GROSZ, rounding=ROUND_DOWN)
    kwota = min(budzet, reszta)
    if kwota <= 0 or kwota < budzet * PODLOGA_RESZTY:
        return Decimal(0)
    return kwota


def uzasadnienie(cel: Cel, symbol: str) -> str:
    """Pierwsze zdania `rationale` - liczby, na ktorych zapadla decyzja."""
    if cel.btc_ema is not None:
        znak = ">" if cel.filtr else "≤"
        filtr = (f"filtr {cel.btc}: zamknięcie dobowe {cel.btc_zamkniecie:.2f} "
                 f"{znak} EMA {PARAMETRY['ema_filtra_dni']} d {cel.btc_ema:.2f}")
    else:
        filtr = f"filtr: {cel.powod}"
    if symbol in cel.sygnaly:
        mom = (f"momentum {PARAMETRY['okno_momentum_dni']} d bez ostatniej doby "
               f"{cel.sygnaly[symbol]:+.2%}, ranga {cel.ranga(symbol)}/{len(cel.sygnaly)}")
    else:
        mom = "momentum nieznane (brak ceny sprzed 30 dni albo z wczoraj)"
    sklad = ", ".join(cel.sklad) if cel.sklad else "gotówka"
    return (f"Strategia {OPIS} (parametry zamrożone z H-3; pomiar egzekucji, nie "
            f"hipoteza przewagi). Decyzja dobowa {cel.chwila:%Y-%m-%d %H:%M} UTC: "
            f"{filtr}; {mom}; skład: {sklad}.")


# --------------------------------------------------------------- odczyt z bazy

# Swieca 4h z 1h DOKLADNIE jak `train.load_panel` (time_bucket, ostatnie
# zamkniecie) - inaczej sygnal na zywo liczylby sie z innych cen niz w H-3.
# `n` = ile swiec 1h weszlo do kubelka, do sprawdzenia domkniecia.
SQL_ZAMKNIECIA = """
SELECT time_bucket(%(bar)s::interval, event_time) AS t,
       (array_agg(close ORDER BY event_time DESC))[1] AS price,
       count(*) AS n
  FROM market.candle
 WHERE instrument_id = %(iid)s AND timeframe = %(tf)s
   AND event_time >= %(od)s AND event_time < %(do)s
 GROUP BY 1
 ORDER BY 1
"""


def zamkniecia(cur, instrument_id: int, h: Horizon, od: datetime,
               do: datetime) -> list[dict]:
    """Swiece 4h zaczynajace sie w [od, do - 4h], czyli domkniete do `do`."""
    cur.execute(SQL_ZAMKNIECIA, {"bar": f"{h.bar_minutes} minutes",
                                 "iid": instrument_id, "tf": h.source_timeframe,
                                 "od": od, "do": do})
    return [{"t": r["t"], "price": float(r["price"]), "n": int(r["n"])}
            for r in cur.fetchall() if r["price"] is not None]


def cel_z_bazy(cur, instrumenty: list[dict], bar_start: datetime,
               h: Horizon, czekaj: bool = True) -> Cel:
    """Cel na ostatnia polnoc UTC z `market.candle`.

    NIEKOMPLETNY, gdy swieca BTC konczaca dobe nie ma jeszcze wszystkich swiec
    1h - potok `history` dopisuje ostatnia godzine kilka minut po polnocy,
    a filtr liczony bez niej czytalby zamkniecie sprzed czterech godzin jako
    dobowe. Petla odklada wtedy decyzje do nastepnego cyklu (jak MIN_POKRYCIE).

    `czekaj=False` - po oknie wejscia (20 min po polnocy) i na swiecach w ciagu
    dnia. Wejsc i tak juz nie ma, a czekanie na swiece, ktorej potok moze nie
    dopisac wcale, wiezilo by WYJSCIA do konca doby. Cel liczy sie wtedy bez
    czekania, a `powod` mowi o tym wprost.

    FILTR Z OSTATNIEJ DOMKNIETEJ DOBY, NIE Z NIEDOMKNIETEJ (przeglad
    2026-10-04). Do tej poprawki `czekaj=False` liczylo filtr z tego, co jest:
    "zamkniecie dobowe" bylo wtedy zamknieciem sprzed czterech godzin, a filtr,
    ktory przez to zmienil znak, oddawal cel = gotowka i LIKWIDOWAL caly sklad
    na podstawie swiecy, ktorej jeszcze nie ma. Niepelne dane dotycza wylacznie
    filtra: momentum 30 d liczy sie BEZ ostatniej doby (`pominiete_dni`), wiec
    jego ceny sa domkniete. Dlatego sklad zostaje z dzisiejszego rankingu,
    a filtr (i liczby w dzienniku) z doby poprzedniej - rotacja dziala,
    likwidacja z samego filtra czeka, az swieca sie domknie (najpozniej
    nastepny cykl). Parametry H-3 bez zmian.
    """
    chwila = chwila_decyzji(bar_start, h)
    btc = symbol_btc(i["symbol"] for i in instrumenty)
    dni_sygnalu = PARAMETRY["okno_momentum_dni"] + 2
    panele = {}
    for i in instrumenty:
        dni = HISTORIA_FILTRA_DNI if i["symbol"] == btc else dni_sygnalu
        panele[i["symbol"]] = zamkniecia(cur, i["id"], h, chwila - dni * DOBA, chwila)
    if btc is not None:
        ostatnia = chwila - timedelta(minutes=h.bar_minutes)
        swieca = next((r for r in panele[btc] if r["t"] == ostatnia), None)
        if swieca is None or swieca["n"] < h.swiec_zrodlowych_na_swiece:
            brak = (f"świeca {btc} {ostatnia:%m-%d %H:%M} niedomknięta "
                    f"({swieca['n'] if swieca else 0}/"
                    f"{h.swiec_zrodlowych_na_swiece} świec {h.source_timeframe})")
            if czekaj:
                return Cel(chwila, False, powod=brak)
            return cel_z_filtrem_poprzedniej_doby(panele, chwila, h.bar_minutes, brak)
    return wyznacz_cel(panele, chwila, h.bar_minutes)


def cel_z_filtrem_poprzedniej_doby(panele: dict[str, list[dict]], chwila: datetime,
                                   bar_minutes: int, brak: str) -> Cel:
    """Sklad z rankingu na `chwila`, filtr z doby wczesniej. CZYSTA FUNKCJA.

    Gdy swieca BTC konczaca dobe jest niedomknieta (`cel_z_bazy`, czekaj=False).
    Uzasadnienie w `cel_z_bazy`."""
    dzis = wyznacz_cel(panele, chwila, bar_minutes)
    wczoraj = wyznacz_cel(panele, chwila - DOBA, bar_minutes)
    sklad = tuple(ranking(dzis.sygnaly, PARAMETRY["top"])) if wczoraj.filtr else ()
    zmiana = ("" if wczoraj.filtr == dzis.filtr else
              f"; filtr z niepełnej świecy {'włączyłby' if dzis.filtr else 'wyłączyłby'} "
              f"się — zmiana czeka na domknięcie")
    return replace(dzis, filtr=wczoraj.filtr, sklad=sklad,
                   btc_zamkniecie=wczoraj.btc_zamkniecie, btc_ema=wczoraj.btc_ema,
                   powod="; ".join(x for x in (
                       f"{brak} — filtr z domkniętej doby "
                       f"{chwila - DOBA:%Y-%m-%d}{zmiana}, wejścia i tak po oknie",
                       wczoraj.powod) if x))
