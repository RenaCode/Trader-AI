"""Delisting i zawieszenie handlu: co się dzieje, gdy rynek znika pod pozycją.

DLACZEGO TO JEST OSOBNY PLIK TESTÓW

`services/ingest/instrumenty.py` pobierał z giełdy pole `status` i zapisywał je
do `meta->>'status_gieldy'` — i nigdy go nie czytał. Zapis bez odczytu jest
gorszy niż brak zapisu, bo wygląda na obsłużony: kolumna w bazie mówi
„sprawdzone i w porządku" o czymś, czego nikt nie sprawdził.

Koszt jest policzalny. Przy delistingu giełda anuluje otwarte zlecenia,
zatrzymuje handel, a po zamknięciu okna wypłat konwertuje niewycofane środki po
ówczesnej cenie rynkowej — proces liczony w miesiącach. Uniwersum ma dziesięć
par, więc jedna zdelistowana pozycja to do ~10% ekspozycji zamrożonej na
kwartał i wycenionej w chwili, na którą nie mamy wpływu.

Że to nie jest scenariusz hipotetyczny, widać w pomiarze: 2026-09-04 podmiot
unijny miał 140 par spot, w tym 4 pary *USDT; 2026-09-07 — 133 pary i ANI
JEDNEJ pary *USDT. Siedem par mniej w trzy dni.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from services.ingest.instrumenty import (
    klasyfikuj_status, ocen_dostepnosc, podmien_odpowiedz, zastosuj_dostepnosc,
)

KORZEN = Path(__file__).resolve().parents[1]
TERAZ = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _inst(symbol="ADAUSDC", *, id=1, tradeable=True, status_gieldy="Trading",
          wylaczony=None):
    return {"id": id, "symbol": symbol, "tradeable": tradeable,
            "status_gieldy": status_gieldy, "wylaczony_przez_status": wylaczony}


def _gielda(status="Trading"):
    return {"krok_ilosci": D("0.1"), "min_ilosc": D("1"), "min_wartosc": D("5"),
            "krok_ceny": D("0.0001"), "status": status}


def _pozycja(id=11, qty=D("100"), cena=D("0.85")):
    return {"id": id, "side": "long", "qty": qty, "avg_price": cena,
            "opened_at": TERAZ - timedelta(days=3)}


# --- slownik statusow -------------------------------------------------------

def test_status_handlowy_to_cisza():
    assert klasyfikuj_status("Trading") == "handel"
    # Gielda potrafi oddac inna wielkosc liter albo spacje; porownanie wprost
    # zamienialoby to w falszywy alarm przy kazdym przebiegu.
    assert klasyfikuj_status(" trading ") == "handel"


def test_zawieszenie_to_NIE_TO_SAMO_co_wycofanie():
    """Przerwa techniczna mija w kilkadziesiąt minut, wycofanie kończy się
    przymusową konwersją po cudzej cenie za kilka miesięcy. Zlanie ich w jedno
    „nie Trading" znaczy albo trwałe wyrzucenie instrumentu z uniwersum po
    przerwie, albo cichy powrót do rynku, który jest wygaszany."""
    assert klasyfikuj_status("PreLaunch") == "zawieszenie"
    assert klasyfikuj_status("Halt") == "zawieszenie"
    assert klasyfikuj_status("Delivering") == "wycofanie"
    assert klasyfikuj_status("Closed") == "wycofanie"


def test_NIEZNANE_slowo_nie_przechodzi_jako_handel():
    """„Nie znam tego statusu, więc pewnie da się handlować" to ten sam błąd, co
    zapisywanie statusu bez czytania: cisza udająca zgodę. Giełda może jutro
    nazwać wygaszanie rynku słowem, którego nie ma na żadnej z list."""
    assert klasyfikuj_status("SomethingNew") == "wycofanie"
    assert klasyfikuj_status("") == "wycofanie"
    assert klasyfikuj_status(None) == "wycofanie"


def test_brak_instrumentu_w_odpowiedzi_to_wycofanie():
    """Mocniejszy przypadek niż zły status: giełda go NIE ZNA, więc nie ma nawet
    czego zawieszać. Pary *USDT zniknęły z podmiotu unijnego w trzy dni."""
    assert klasyfikuj_status("Trading", obecny_u_gieldy=False) == "wycofanie"


# --- werdykt o dostepnosci --------------------------------------------------

def test_wycofanie_ZDEJMUJE_instrument_z_uniwersum():
    """Sam alarm nie wystarcza. Dopóki `tradeable` zostaje, pętla decyzyjna może
    wejść w rynek, który jest wygaszany — a wtedy wejście zamienia się w pozycję
    nie do zamknięcia."""
    (w,) = ocen_dostepnosc([_inst()], {"ADAUSDC": _gielda("Closed")}, {})
    assert w["klasa"] == "wycofanie"
    assert w["zdjac"] is True
    assert w["kod"] == "instrument.delisted"


def test_zawieszenie_i_wycofanie_maja_ROZNE_typy_zdarzen():
    """Jeden wspólny alarm ustawiłby próg uwagi na tym częstszym — czyli uczył
    przewijać ten, który jedyny wymaga działania."""
    (halt,) = ocen_dostepnosc([_inst()], {"ADAUSDC": _gielda("Halt")}, {})
    (delist,) = ocen_dostepnosc([_inst()], {"ADAUSDC": _gielda("Delivering")}, {})
    assert halt["kod"] == "instrument.suspended"
    assert delist["kod"] == "instrument.delisted"
    assert halt["kod"] != delist["kod"]


def test_pary_wylaczone_swiadomie_NIE_alarmuja():
    """Migracja 0020 wyłączyła czternaście par USDT, których podmiot unijny nie
    oferuje. Alarm o nich przy każdym przebiegu nauczyłby czytać tę czujkę
    nieuważnie akurat wtedy, gdy dotyczy pary, którą naprawdę handlujemy."""
    assert ocen_dostepnosc([_inst("LTCUSDT", tradeable=False,
                                  status_gieldy=None)], {}, {}) == []


def test_pozycja_na_znikniętym_instrumencie_alarmuje_MIMO_braku_tradeable():
    """Zamrożony kapitał nie przestaje być zamrożony przez to, że instrument
    wypadł z uniwersum decyzyjnego — a to jest jedyny przypadek, w którym
    wyłączona para nadal nas kosztuje."""
    (w,) = ocen_dostepnosc([_inst("LTCUSDT", tradeable=False, status_gieldy="Trading")],
                           {}, {"LTCUSDT": [_pozycja()]})
    assert w["kod"] == "instrument.delisted"
    assert "OTWARTA POZYCJA" in w["tresc"]
    # Liczby, bo alarmu bez liczb nie da sie ocenic bez logowania do bazy.
    assert "100" in w["tresc"] and "0.85" in w["tresc"]


def test_modul_NIE_zamyka_pozycji_tylko_ja_zglasza():
    """Zamknięcie to decyzja handlowa: cena, moment, limity ryzyka. Ten proces
    chodzi bez klucza API (endpoint instrumentów jest publiczny), więc automat
    „sprzedaj, bo status się zmienił" sprzedawałby w panice po ogłoszeniu —
    i to jest dokładnie najgorszy możliwy moment."""
    (w,) = ocen_dostepnosc([_inst()], {"ADAUSDC": _gielda("Closed")},
                           {"ADAUSDC": [_pozycja()]})
    assert "NIE JEST ZAMYKANA AUTOMATYCZNIE" in w["tresc"]
    assert "decyzją handlową" in w["tresc"]


def test_powtorny_przebieg_bez_zmiany_to_CISZA():
    """Wycofanie trwa miesiącami. Alarm co godzinę przez kwartał jest nie do
    odróżnienia od tła — o niezmiennym problemie przypomina dozor w rytmie
    dobowym."""
    juz_zdjety = _inst(tradeable=False, status_gieldy="Closed", wylaczony="wycofanie")
    (w,) = ocen_dostepnosc([juz_zdjety], {"ADAUSDC": _gielda("Closed")},
                           {"ADAUSDC": [_pozycja()]})
    assert w["kod"] is None


def test_instrument_wciaz_handlowalny_alarmuje_TAKZE_bez_zmiany_statusu():
    """Stan „status się nie zmienił, ale bramka nadal otwarta" znaczy, że
    poprzedni przebieg nie doszedł do zapisu. Cisza w tym miejscu zostawiłaby
    wycofany instrument w uniwersum na zawsze."""
    (w,) = ocen_dostepnosc([_inst(status_gieldy="Closed")],
                           {"ADAUSDC": _gielda("Closed")}, {})
    assert w["kod"] == "instrument.delisted"


def test_po_zawieszeniu_instrument_wraca_SAM():
    """Inaczej dwudziestominutowa przerwa techniczna kurczyłaby uniwersum
    o 10% na stałe, a nikt by tego nie zauważył — bo zniknięcie instrumentu
    z uniwersum wygląda jak brak sygnałów na nim."""
    (w,) = ocen_dostepnosc(
        [_inst(tradeable=False, status_gieldy="Halt", wylaczony="zawieszenie")],
        {"ADAUSDC": _gielda("Trading")}, {})
    assert w["przywroc"] is True


def test_po_WYCOFANIU_instrument_NIE_wraca_sam():
    """Rynek uruchomiony ponownie po wycofaniu ma inną płynność i inną historię —
    wejście w niego jest decyzją, a nie przywróceniem stanu poprzedniego."""
    (w,) = ocen_dostepnosc(
        [_inst(tradeable=False, status_gieldy="Closed", wylaczony="wycofanie")],
        {"ADAUSDC": _gielda("Trading")}, {})
    assert w["przywroc"] is False
    assert w["klasa"] == "powrot_reczny"


def test_automat_NIE_cofa_decyzji_czlowieka():
    """`tradeable` zmienia się migracją albo świadomą decyzją (0020). Instrument
    wyłączony ręcznie nie ma naszego śladu — i automat ma go nie dotykać."""
    assert ocen_dostepnosc([_inst(tradeable=False, status_gieldy="Trading")],
                           {"ADAUSDC": _gielda("Trading")}, {}) == []


# --- zapis do bazy ----------------------------------------------------------

class _Zapis:
    def __init__(self):
        self.sql = []
        self.powiadomienia = []

    async def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    async def fetch_all(self, sql, params=None):
        return [{"id": 1}]

    async def notify(self, user_id, kod, tytul, tresc, **kw):
        self.powiadomienia.append((kod, tytul, kw.get("severity")))


def _uruchom(w_bazie, z_gieldy, pozycje=None, sucho=False):
    z = _Zapis()
    pilne = asyncio.run(zastosuj_dostepnosc(
        w_bazie, z_gieldy, pozycje or {}, execute=z.execute, fetch_all=z.fetch_all,
        notify=z.notify, sucho=sucho))
    return z, pilne


def test_data_sprawdzenia_zapisywana_dla_KAZDEGO_instrumentu():
    """To jest cała różnica między „status jest Trading" a „status BYŁ Trading,
    gdy ktoś ostatnio pytał". Gdyby data szła tylko przy zmianie, zatrzymane
    zadanie zostawiłoby kolumnę wyglądającą na świeże potwierdzenie."""
    z, _ = _uruchom([_inst("ADAUSDC", id=1), _inst("SOLUSDC", id=2)],
                    {"ADAUSDC": _gielda(), "SOLUSDC": _gielda()})
    zapisy = [s for s, _ in z.sql if "status_sprawdzony_at = now()" in s]
    assert len(zapisy) == 2, "data sprawdzenia zapisana nie dla wszystkich"


def test_zdjecie_tradeable_zostawia_SLAD_kto_je_zdjal():
    """Bez śladu nie da się odróżnić instrumentu wyłączonego decyzją człowieka
    od wyłączonego automatem — a wtedy nie da się bezpiecznie nic przywrócić."""
    z, pilne = _uruchom([_inst()], {"ADAUSDC": _gielda("Closed")})
    assert pilne == 1
    assert any("tradeable = false" in s and "wylaczony_przez_status" in s
               for s, _ in z.sql)


def test_zapis_dotyka_WYLACZNIE_tabeli_instrumentow():
    """Kanarek na zakres uprawnień tego modułu: gdyby ktoś dopisał tu domykanie
    pozycji, ten test pokazałby to jako zapis do `exec.*`. Moduł chodzi bez
    klucza API i bez wiedzy o cenie — nie ma czym podjąć decyzji handlowej."""
    z, _ = _uruchom([_inst()], {"ADAUSDC": _gielda("Closed")},
                    {"ADAUSDC": [_pozycja()]})
    assert z.sql, "brak jakiegokolwiek zapisu — test niczego nie pilnuje"
    for s, _ in z.sql:
        assert s.startswith("UPDATE ref.instrument"), f"zapis poza ref.instrument: {s}"


def test_alarm_o_wycofaniu_jest_krytyczny():
    """Ostrzeżenie ma cooldown i daje się wyciszyć. Wycofanie z otwartą pozycją
    nie mija samo i nie da się go „przyjąć do wiadomości"."""
    z, _ = _uruchom([_inst()], {"ADAUSDC": _gielda("Closed")},
                    {"ADAUSDC": [_pozycja()]})
    assert ("instrument.delisted", z.powiadomienia[0][1], "critical") in z.powiadomienia


def test_tryb_suchy_niczego_nie_zapisuje_ani_nie_wysyla():
    z, pilne = _uruchom([_inst()], {"ADAUSDC": _gielda("Closed")}, sucho=True)
    assert pilne == 1 and z.sql == [] and z.powiadomienia == []


# --- wywolanie sciezki alarmowej na zadanie ---------------------------------

def test_da_sie_udawac_status_zeby_sprawdzic_sciezke_alarmowa():
    """Wszystkie dziesięć par uniwersum ma dziś status `Trading`, więc ścieżki
    wycofania nie da się wywołać danymi z giełdy. Zabezpieczenie, które ma
    zadziałać raz na rok w najgorszym momencie, nie byłoby sprawdzone NIGDY —
    dokładnie po to czujka żywotności dostała `--prog-min`."""
    podmieniona = podmien_odpowiedz({"ADAUSDC": _gielda()}, ["ADAUSDC=Closed"])
    assert podmieniona["ADAUSDC"]["status"] == "Closed"
    # Reguly zostaja nietkniete - udajemy status, nie cala odpowiedz.
    assert podmieniona["ADAUSDC"]["krok_ilosci"] == D("0.1")
    (w,) = ocen_dostepnosc([_inst()], podmieniona, {})
    assert w["kod"] == "instrument.delisted"


def test_da_sie_udawac_ZNIKNIECIE_instrumentu():
    assert podmien_odpowiedz({"ADAUSDC": _gielda()}, ["adausdc=BRAK"]) == {}


# --- czujka w dozorze -------------------------------------------------------

def _wiersz(symbol="ADAUSDC", *, tradeable=True, status="Trading",
            sprawdzony_h=1, pozycji=0, wartosc=0):
    return {"symbol": symbol, "tradeable": tradeable, "status_gieldy": status,
            "sprawdzony_h": sprawdzony_h, "pozycji": pozycji, "wartosc": wartosc}


def test_dozor_milczy_gdy_uniwersum_handluje():
    from services.engine.dozor import ocen_instrumenty

    assert ocen_instrumenty([_wiersz()]) == []


def test_dozor_zglasza_ze_NIKT_NIE_SPRAWDZAL_statusu():
    """Brak odczytu to nie jest zgodność. Zadanie, które przestało chodzić,
    zostawia kolumnę statusu zamrożoną na „Trading" — i od tej chwili baza
    codziennie zapewnia, że wszystko handluje."""
    from services.engine.dozor import ocen_instrumenty

    (kod, tytul, _), = ocen_instrumenty([_wiersz(sprawdzony_h=48)])
    assert kod == "instrument.suspended" and "nie był sprawdzany" in tytul
    # Nigdy niesprawdzony to ten sam problem, nie osobny przypadek brzegowy.
    assert ocen_instrumenty([_wiersz(sprawdzony_h=None)])


def test_dozor_przypomina_o_ZAMROZONYM_kapitale():
    """Alarm o zmianie stanu idzie raz, a wycofanie trwa miesiącami. Pozycja na
    wycofywanym instrumencie jest problemem codziennie, aż do zamknięcia."""
    from services.engine.dozor import ocen_instrumenty

    (kod, tytul, tresc), = ocen_instrumenty(
        [_wiersz(tradeable=False, status="Closed", pozycji=1, wartosc=85)])
    assert kod == "instrument.delisted"
    assert "85" in tresc and "ZAMROŻONY" in tresc
    assert "DECYZJĄ HANDLOWĄ" in tresc


def test_dozor_wykrywa_ZE_BRAMKA_SIE_NIE_ZAMKNELA():
    """Instrument poza handlem, który wciąż ma `tradeable`, znaczy, że zapis
    poprzedniego przebiegu nie doszedł — a silnik może wystawić zlecenie na
    rynek, którego nie ma."""
    from services.engine.dozor import ocen_instrumenty

    (kod, tytul, _), = ocen_instrumenty([_wiersz(status="Delivering")])
    assert kod == "instrument.delisted" and "wciąż w uniwersum" in tytul


def test_dozor_odroznia_NIEZNANY_GIELDZIE_od_niesprawdzonego():
    """Status NULL przy świeżej dacie sprawdzenia znaczy „pytaliśmy, giełda go
    nie zna" — czyli wycofanie. Ten sam NULL bez daty znaczy tylko tyle, że
    nikt nie pytał. Zlanie tych dwóch przypadków dałoby fałszywy alarm
    o delistingu CAŁEGO uniwersum tuż po migracji 0033 — bo wtedy żadna data
    sprawdzenia jeszcze nie istnieje."""
    from services.engine.dozor import ocen_instrumenty

    kody = {k for k, _, _ in ocen_instrumenty([_wiersz(status=None, pozycji=1)])}
    assert kody == {"instrument.delisted"}
    # Bez daty sprawdzenia zostaje wylacznie zarzut o brak odczytu.
    kody = {k for k, _, _ in ocen_instrumenty(
        [_wiersz(status=None, sprawdzony_h=None, pozycji=1)])}
    assert kody == {"instrument.suspended"}


def test_prog_swiezosci_statusu_zostawia_zapas_na_dobowy_harmonogram():
    from services.engine.dozor import PRZEGLAD_STATUSU_H

    assert PRZEGLAD_STATUSU_H >= 24, "próg krótszy niż doba to fałszywy alarm"
    assert PRZEGLAD_STATUSU_H <= 48, "powyżej doby zapasu da się przespać delisting"


def test_sciezke_alarmowa_dozoru_da_sie_wywolac_na_zadanie():
    """`--prog-statusu-h 0` robi tu to samo, co `--prog-min` przy czujce
    żywotności: pozwala sprawdzić, że alarm faktycznie wychodzi, bez czekania
    na prawdziwą awarię."""
    from services.engine.dozor import ocen_instrumenty

    assert ocen_instrumenty([_wiersz()], prog_h=0)
    zrodlo = (KORZEN / "services/engine/dozor.py").read_text(encoding="utf-8")
    assert "--prog-statusu-h" in zrodlo and "prog_h=args.prog_statusu_h" in zrodlo


def test_dozor_NIE_ma_wlasnej_listy_statusow():
    """Druga lista słów rozjechałaby się przy pierwszym nowym statusie giełdy —
    i to właśnie ta czujka twierdziłaby wtedy, że jest spokojnie."""
    zrodlo = (KORZEN / "services/engine/dozor.py").read_text(encoding="utf-8")
    assert "from services.ingest.instrumenty import klasyfikuj_status" in zrodlo
    assert "STATUSY_WYCOFANIA = " not in zrodlo


# --- schemat ----------------------------------------------------------------

def test_migracja_daje_kolumny_i_typy_zdarzen():
    m = (KORZEN / "db/migrations/0033_status_instrumentu.sql").read_text(encoding="utf-8")
    for kolumna in ("status_gieldy", "status_gieldy_od", "status_sprawdzony_at",
                    "wylaczony_przez_status"):
        assert kolumna in m, f"brak kolumny {kolumna}"
    assert "'instrument.delisted'" in m and "'instrument.suspended'" in m
    # Alarmu o wycofaniu nie da sie wyciszyc: kapital jest zamrozony niezaleznie
    # od tego, czy ktos chce o tym sluchac.
    assert "'ryzyko', 'critical', true" in m


def test_status_ma_JEDNO_zrodlo_a_nie_kopie_w_meta():
    """Dwa miejsca z tą samą prawdą rozjeżdżają się przy pierwszej zmianie —
    a wtedy pytanie „jaki jest status" przestaje mieć jedną odpowiedź."""
    m = (KORZEN / "db/migrations/0033_status_instrumentu.sql").read_text(encoding="utf-8")
    assert "meta - 'status_gieldy'" in m, "stara kopia w meta nie jest usuwana"
    zrodlo = (KORZEN / "services/ingest/instrumenty.py").read_text(encoding="utf-8")
    assert "'status_gieldy', %s::text" not in zrodlo, "status wciąż pisany do meta"
