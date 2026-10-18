"""Czy rynek danego silnika jest TERAZ otwarty - jedno miejsce, oba silniki.

DLACZEGO TO W OGOLE ISTNIEJE

`loop.py` zapisal ten warunek sam, przy stalej `CENA_STARA_PO_MIN`:

    ZASTRZEZENIE DLA SILNIKA GIELDOWEGO. Prog zaklada rynek dzialajacy bez
    przerwy. Akcja poza sesja nie ma swiec 1m i KAZDA pozycja bedzie tu "bez
    swiezej ceny" (...). Zanim silnik `equity` ruszy, prog musi pochodzic
    z kalendarza sesji, a nie z tej stalej.

Bez tej bramki petla equity chodzilaby przez cala noc, widziala wylacznie ceny
sprzed zamkniecia i pomijala wszystko jako "bez swiezej ceny". To nie jest
bezpieczne przez przypadek - to jest cisza nieodrozialna od awarii potoku
danych. Te same dwie linie w logu ("pominiete bez ceny: 21") znaczylyby o 03:00
"rynek spi", a o 16:00 "dane stanely", i nic ich nie rozroznia.

ZRODLEM PRAWDY JEST GIELDA, NIE NASZ KALENDARZ

`ref.session_calendar` istnieje od migracji 0001 i NIE ZAPISAL DO NIEJ NIC
ZADEN PROCES - jest mechanizmem zbudowanym i niewpietym. Wypelnienie go
wymagaloby wlasnego zadania, wlasnego pojecia o swietach i wlasnej czujki na
wypadek, gdyby to zadanie stanelo; a i tak odpowiadaloby na pytanie, na ktore
gielda odpowiada sama, jednym zapytaniem, bez mozliwosci rozjechania sie
z rzeczywistoscia. Alpaca wystawia `/v2/clock` i to jest ten sam glos, ktory
przyjmie albo odrzuci nasze zlecenie.

Koszt: jedno zapytanie na cykl decyzyjny, czyli jedno na piec minut.

NIE WIEM JEST TRZECIM STANEM, I NIE ZNACZY "OTWARTE"

Gielda o sesjach, ktorej zegara nie umiemy zapytac, nie dostaje domyslu
"pewnie otwarte". Zwracamy `otwarta=None` i wolacz ma z tym zrobic to, co
robi sie z brakiem wiedzy przy operacji nieodwracalnej: nie handlowac. Domysl
"otwarte" zamienialby awarie odczytu w zlecenia skladane po godzinach.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from services.exchanges.fabryka import OBSLUGIWANE


@dataclass(frozen=True)
class StanSesji:
    """Odpowiedz na jedno pytanie: czy silnik ma teraz prawo decydowac.

    `otwarta` ma TRZY stany, nie dwa. `None` to "nie wiem" - patrz naglowek
    modulu. `zrodlo` mowi, skad ta odpowiedz pochodzi, zeby linia w logu
    odrozniala rynek ciagly od zegara gieldy od awarii odczytu.
    """
    otwarta: bool | None
    zrodlo: str
    venue: str | None = None
    nastepne_otwarcie: datetime | None = None
    nastepne_zamkniecie: datetime | None = None
    blad: str | None = None

    @property
    def wolno_decydowac(self) -> bool:
        """Jedyna regula, ktora wolacz powinien czytac. `None` jest NIE."""
        return self.otwarta is True

    def opis(self, teraz: datetime | None = None) -> str:
        """Jedno zdanie do logu. Zawsze mowi, DLACZEGO, nie tylko CO."""
        if self.otwarta is True:
            kiedy = (f", zamkniecie {self.nastepne_zamkniecie:%H:%M} UTC"
                     if self.nastepne_zamkniecie else "")
            return f"sesja otwarta ({self.zrodlo}{kiedy})"
        if self.otwarta is False:
            kiedy = (f", otwarcie {self.nastepne_otwarcie:%Y-%m-%d %H:%M} UTC"
                     if self.nastepne_otwarcie else "")
            return f"POZA SESJĄ ({self.zrodlo}{kiedy})"
        return f"STAN SESJI NIEZNANY ({self.zrodlo}): {self.blad or 'brak zegara giełdy'}"

    def swieca_przed_otwarciem(self, koniec_swiecy: datetime,
                               teraz: datetime) -> bool:
        """Czy swieca zamknieta o `koniec_swiecy` skonczyla sie przed otwarciem
        DZISIEJSZEJ sesji. Prawda wylacznie przy otwartej sesji Alpaki.

        Rynek ciagly, sesja zamknieta i "nie wiem" daja False - przy dwoch
        ostatnich petla i tak nie decyduje (bramka sesji).
        """
        if self.otwarta is not True or self.venue not in OTWARCIE_SESJI:
            return False
        return koniec_swiecy <= OTWARCIE_SESJI[self.venue](teraz.date())


# GODZINA OTWARCIA SESJI - JEDYNE MIEJSCE, W KTORYM NIE PYTAMY GIELDY.
#
# `/v2/clock` Alpaki mowi, czy jest otwarte, i podaje NASTEPNE otwarcie - przy
# otwartej sesji to jutrzejsze, wiec dzisiejszego z zegara nie odczytamy.
# Petli potrzebne jest ono do jednego: rozpoznania swiecy zamknietej przed
# otwarciem (przy swiecy 1h i otwarciu 13:30 UTC swieca 12:00-13:00 jest
# rozstrzygana najwczesniej 13:30, czyli zawsze po `MAKS_ZWLOKA_WEJSCIA`,
# i konczyla jako `decyzja_przeterminowana` - licznik, ktory ma mierzyc
# spoznienie potoku).
#
# Regularna sesja NYSE/Nasdaq otwiera sie o 09:30 czasu Nowego Jorku KAZDEGO
# dnia sesyjnego - dni skrocone koncza sie wczesniej, nie zaczynaja pozniej.
# Strefa z `zoneinfo` (baza stref jest w obrazie silnika, /usr/share/zoneinfo
# - sprawdzone w trader-engine-equity), wiec czas letni rozstrzyga baza IANA,
# nie wlasna kopia reguly.
#
# Pomylka tej funkcji zostawia stan sprzed 2026-10-18: swieca przed sesja
# konczy jako `decyzja_przeterminowana`. Zadne wejscie nie przechodzi przez nia
# dodatkowo.
STREFA_US = ZoneInfo("America/New_York")


def otwarcie_sesji_us(dzien: date) -> datetime:
    """09:30 czasu Nowego Jorku w dniu `dzien`, w UTC."""
    return datetime.combine(dzien, time(9, 30), tzinfo=STREFA_US).astimezone(timezone.utc)


OTWARCIE_SESJI = {"alpaca": otwarcie_sesji_us}


def venue_silnika(cur, engine: str) -> dict | None:
    """Gielda, z ktora UMIEMY rozmawiac, dla tego silnika.

    Ten sam warunek co `fabryka.adapter_danych`: wpis w `ref.venue` bez adaptera
    (placeholder `ibkr` z seeda 0008) nie moze tu wygrac z gielda, na ktorej
    naprawde stoi konto - inaczej silnik equity pytalby o godziny sesji
    brokera, ktorego nie uzywa.
    """
    cur.execute(
        "SELECT code, is_24_7 FROM ref.venue"
        " WHERE engine::text = %s AND code = ANY(%s) ORDER BY id LIMIT 1",
        (engine, list(OBSLUGIWANE)))
    return cur.fetchone()


async def stan_sesji(cur, engine: str, gielda=None, *,
                     teraz: datetime | None = None) -> StanSesji:
    """Czy rynek silnika `engine` jest otwarty.

    `gielda` to adapter egzekucji albo None. Adapter rynku ciaglego nie musi
    miec zegara i nie jest o niego pytany - `is_24_7` rozstrzyga wczesniej.
    """
    teraz = teraz or datetime.now(timezone.utc)
    venue = venue_silnika(cur, engine)
    if not venue:
        return StanSesji(None, "brak gieldy w ref.venue",
                         blad=f"silnik {engine} nie ma obslugiwanej gieldy")
    kod = venue["code"]

    # RYNEK CIAGLY KONCZY PYTANIE. Bybit nie ma `/v2/clock` i nie ma go miec -
    # pytanie "czy otwarte" nie istnieje dla rynku, ktory nie zamyka.
    if venue["is_24_7"]:
        return StanSesji(True, "rynek ciągły (is_24_7)", venue=kod)

    zegar = getattr(gielda, "zegar", None)
    if zegar is None:
        return StanSesji(
            None, "brak zegara giełdy", venue=kod,
            blad=(f"{kod} ma sesje, a adapter nie wystawia `zegar()` "
                  f"(egzekucja papierowa albo brak połączenia)"))
    try:
        z = await zegar()
    except Exception as e:                                    # noqa: BLE001
        # AWARIA ODCZYTU NIE JEST OTWARCIEM. Zwracamy "nie wiem" i wolacz
        # stoi - patrz naglowek modulu.
        return StanSesji(None, "zegar giełdy nie odpowiedział", venue=kod,
                         blad=f"{type(e).__name__}: {e}")
    return StanSesji(bool(z.otwarta), "zegar giełdy", venue=kod,
                     nastepne_otwarcie=z.nastepne_otwarcie,
                     nastepne_zamkniecie=z.nastepne_zamkniecie)
