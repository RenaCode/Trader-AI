"""Pobranie reguł handlu z giełdy do `ref.instrument`.

DLACZEGO TO NIE JEST DROBIAZG KONFIGURACYJNY

Dziesięć z dwunastu instrumentów dopuszczonych do handlu nie miało w bazie ani
kroku ilości, ani kroku ceny, a minimalnej wartości zlecenia nie miał ŻADEN.
Pierwsze zlecenie na produkcji zostałoby odrzucone jako błąd parametru — a taki
błąd w nocnym logu jest nie do odróżnienia od braku sygnału. System „nie
handluje" i nikt nie wie, że coś się zepsuło.

ENDPOINT JEST PUBLICZNY. Nie wymaga klucza, więc uzupełnienie reguł da się
zrobić zanim ktokolwiek dotknie prawdziwych pieniędzy — i nie zależy od tego,
którego podmiotu jest konto.

NIEKOMPLETNE REGUŁY BLOKUJĄ HANDEL, a nie tylko zapisują ostrzeżenie. Zwracany
kod wyjścia jest niezerowy, dopóki którykolwiek instrument dopuszczony do handlu
nie ma kompletu — bo to jest stan, w którym egzekucji uruchamiać nie wolno.

STATUS INSTRUMENTU JEST TU DRUGIM ZADANIEM, NIE DODATKIEM

Ta sama odpowiedź giełdy niesie pole `status`, a jej BRAK dla naszego symbolu
niesie coś jeszcze mocniejszego: giełda tego instrumentu nie zna. Przez długi
czas jedno lądowało w `meta` i nikt tego nie czytał, a drugie kończyło się
linijką „BRAK U GIEŁDY" w logu zadania, którego nikt nie ogląda.

Przy delistingu giełda anuluje otwarte zlecenia, zatrzymuje handel, a po
zamknięciu okna wypłat konwertuje niewycofane środki po ówczesnej cenie
rynkowej — proces liczony w MIESIĄCACH. Uniwersum ma dziesięć par, więc jedna
zdelistowana pozycja to do ~10% ekspozycji zamrożonej na kwartał i wycenionej
w chwili, na którą nie mamy wpływu. Bez odczytu statusu wygląda to jak zwykła
otwarta pozycja: żaden limit ryzyka nie widzi różnicy.

CO TEN MODUŁ ROBI, A CZEGO NIE ZROBI

Zdejmuje `tradeable` — to jest odmowa WCHODZENIA w rynek, którego już nie ma,
i jedyna bramka, którą da się zamknąć bez zgadywania ceny. NIE ZAMYKA POZYCJI:
zamknięcie jest decyzją handlową (wielkość, cena, moment, limity ryzyka),
a ten proces chodzi bez klucza API i bez wiedzy o rynku. Moduł, który wystawia
zlecenie „bo status się zmienił", sprzedaje w najgorszym możliwym momencie —
w panice po ogłoszeniu. Otwartą pozycję więc ZGŁASZA, alarmem krytycznym,
z liczbami.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def rozbieznosci(w_bazie: list[dict], z_gieldy: dict[str, dict]) -> list[dict]:
    """Co trzeba zmienić, żeby baza zgadzała się z giełdą. CZYSTA FUNKCJA.

    Zwraca wyłącznie instrumenty WYMAGAJĄCE zmiany. Instrument, którego giełda
    nie zna, też jest zmianą — ale opisaną jako brak, nie jako zerowe reguły:
    zero przeszłoby przez każde sprawdzenie i wywróciło się dopiero przy
    dzieleniu.
    """
    out = []
    for r in w_bazie:
        g = z_gieldy.get(r["symbol"])
        if g is None:
            out.append({"symbol": r["symbol"], "id": r["id"], "brak_u_gieldy": True})
            continue
        nowe = {"lot_size": g["krok_ilosci"], "tick_size": g["krok_ceny"],
                "min_notional": g["min_wartosc"]}
        zmiany = {k: v for k, v in nowe.items()
                  if v is not None and (r.get(k) is None or r[k] != v)}
        # PORÓWNANIE PO TEKŚCIE. `meta` jest jsonb, więc wraca jako string,
        # a giełda daje Decimal — porównanie wprost jest zawsze różne i meta
        # przepisywałaby się przy każdym uruchomieniu bez końca.
        min_z_gieldy = str(g["min_ilosc"]) if g["min_ilosc"] is not None else None
        if zmiany or r.get("min_ilosc_meta") != min_z_gieldy:
            out.append({"symbol": r["symbol"], "id": r["id"], "zmiany": zmiany,
                        "min_ilosc": g["min_ilosc"], "brak_u_gieldy": False})
    return out


# Slownik statusow gieldy. TRZY KUBLY, BO SKUTKI SA TRZECH RODZAJOW.
#
# Bybit v5 podaje przy instrumencie `status`. "Trading" znaczy, ze mozna
# handlowac; pozostale wartosci znacza dwie zupelnie rozne rzeczy:
#
#   * przerwa - handel wroci sam, zwykle w kilkadziesiat minut albo po starcie
#     nowego rynku (PreLaunch). Reakcja: nie wchodzimy, czekamy.
#   * wycofanie - rynek jest wygaszany i NIE WROCI. Reakcja: nie wchodzimy juz
#     nigdy bez decyzji czlowieka, a otwarta pozycja to problem kapitalowy.
#
# Zlanie ich w jedno "nie Trading" znaczyloby, ze albo przerwa techniczna
# wyrzuca instrument z uniwersum na stale, albo delisting wraca do uniwersum
# sam - i obie pomylki sa drogie.
STATUS_HANDLOWY = "trading"
STATUSY_ZAWIESZENIA = frozenset({
    "prelaunch", "prelisting", "prelist", "halt", "halted", "suspended",
    "paused", "break", "maintenance",
})
STATUSY_WYCOFANIA = frozenset({
    "delivering", "settling", "settled", "closed", "close", "delisted",
    "delist", "delisting", "terminated",
})


def klasyfikuj_status(status: str | None, *, obecny_u_gieldy: bool = True) -> str:
    """'handel' | 'zawieszenie' | 'wycofanie'. CZYSTA FUNKCJA.

    NIEZNANE SLOWO TRAKTUJEMY JAK WYCOFANIE — i to jest tu cala decyzja
    projektowa. Odwrotne domyslne ("nie znam tego statusu, wiec pewnie da sie
    handlowac") jest dokladnie tym samym bledem, co zapisywanie statusu bez
    czytania: cisza udajaca zgode. Gielda moze jutro nazwac wygaszanie rynku
    slowem, ktorego nie ma na zadnej z tych list; falszywy alarm kosztuje jedno
    spojrzenie czlowieka, przespane wycofanie kosztuje kwartal zamrozonego
    kapitalu.

    Brak instrumentu w odpowiedzi to przypadek MOCNIEJSZY niz zly status:
    gielda go juz nie zna, wiec nie ma nawet czego zawieszac.
    """
    if not obecny_u_gieldy:
        return "wycofanie"
    slowo = (status or "").strip().lower()
    if slowo == STATUS_HANDLOWY:
        return "handel"
    if slowo in STATUSY_ZAWIESZENIA:
        return "zawieszenie"
    return "wycofanie"


def _opis_pozycji(pozycje: list[dict]) -> str:
    """Otwarta pozycja opisana LICZBAMI, bo alarmu bez liczb nie da sie ocenic."""
    linie = []
    for p in pozycje:
        wartosc = float(p["qty"]) * float(p["avg_price"])
        linie.append(f"  • {p['side']} {p['qty']} @ {p['avg_price']} "
                     f"(≈{wartosc:.2f}, otwarta od "
                     f"{p['opened_at']:%Y-%m-%d %H:%M} UTC, pozycja #{p['id']})")
    return "\n".join(linie)


def ocen_dostepnosc(w_bazie: list[dict], z_gieldy: dict[str, dict],
                    pozycje: dict[str, list[dict]]) -> list[dict]:
    """Co gielda mowi o dostepnosci naszych instrumentow. CZYSTA FUNKCJA.

    Zwraca listę werdyktów wymagających działania. Werdykt ma:
        klasa    — 'wycofanie' | 'zawieszenie' | 'powrot' | 'powrot_reczny'
        zdjac    — czy odebrać `tradeable`
        przywroc — czy oddać `tradeable` (tylko po zawieszeniu, patrz niżej)
        kod      — typ zdarzenia powiadomienia albo None (cisza)

    ZAKRES: instrument DOPUSZCZONY DO HANDLU albo z OTWARTĄ POZYCJĄ.

    Migracja 0020 wyłączyła czternaście par USDT, których podmiot unijny nie
    oferuje — one nie są i nigdy nie będą w odpowiedzi jego giełdy. Alarmowanie
    o nich przy każdym przebiegu nauczyłoby czytać ten alarm nieuważnie akurat
    wtedy, gdy dotyczy pary, którą naprawdę handlujemy.

    PRZYWRACAMY TYLKO TO, CO SAMI ZDJĘLIŚMY, I TYLKO PO ZAWIESZENIU.

    `tradeable` zmienia się migracją albo świadomą decyzją (0020) — automat,
    który oddaje flagę instrumentowi wyłączonemu przez człowieka, po cichu
    cofa cudzą decyzję. Dlatego warunkiem powrotu jest własny ślad
    `wylaczony_przez_status = 'zawieszenie'`. Po wycofaniu flaga zostaje zdjęta
    nawet wtedy, gdy giełda znów poda Trading: rynek reaktywowany po delistingu
    to inny rynek (inna płynność, inna historia) i wejście w niego jest decyzją,
    nie przywróceniem stanu.
    """
    out: list[dict] = []
    for r in w_bazie:
        symbol = r["symbol"]
        g = z_gieldy.get(symbol)
        status = g["status"] if g else None
        klasa = klasyfikuj_status(status, obecny_u_gieldy=g is not None)
        moje = r.get("wylaczony_przez_status")
        otwarte = pozycje.get(symbol) or []

        if klasa == "handel":
            if moje is None:
                continue
            if r.get("tradeable"):
                # Slad po naszym wylaczeniu, ale flaga juz wrocila - ktos
                # przywrocil recznie. Zostaje sprzatniecie sladu, po cichu:
                # powiadomienie o tym, co czlowiek wlasnie sam zrobil, to szum.
                out.append({
                    "symbol": symbol, "id": r["id"], "klasa": "powrot",
                    "status": status, "zdjac": False, "przywroc": True,
                    "kod": None, "tytul": f"{symbol}: ślad po wyłączeniu sprzątnięty",
                    "tresc": "", "pozycje": otwarte,
                })
            elif moje == "zawieszenie":
                out.append({
                    "symbol": symbol, "id": r["id"], "klasa": "powrot",
                    "status": status, "zdjac": False, "przywroc": True,
                    "kod": "instrument.suspended",
                    "tytul": f"{symbol}: handel wznowiony",
                    "tresc": ("Giełda znów podaje status Trading. Instrument "
                              "wraca do uniwersum decyzyjnego — zdjęliśmy go "
                              "automatem na czas przerwy, więc automat go "
                              "oddaje."),
                    "pozycje": otwarte,
                })
            elif not r.get("tradeable") and moje == "wycofanie":
                out.append({
                    "symbol": symbol, "id": r["id"], "klasa": "powrot_reczny",
                    "status": status, "zdjac": False, "przywroc": False,
                    "kod": "instrument.suspended",
                    "tytul": f"{symbol}: giełda znów podaje Trading po wycofaniu",
                    "tresc": ("Instrument był oznaczony jako wycofywany i został "
                              "zdjęty z uniwersum decyzyjnego. Automat go NIE "
                              "przywraca: rynek uruchomiony ponownie po wycofaniu "
                              "ma inną płynność i inną historię, więc wejście "
                              "w niego jest decyzją, a nie powrotem do stanu "
                              "poprzedniego. Przywrócenie: ręcznie, po "
                              "sprawdzeniu księgi."),
                    "pozycje": otwarte,
                })
            continue

        # Poza handlem. Milczymy o instrumentach, ktorych i tak nie tykamy.
        if not r.get("tradeable") and not otwarte:
            continue

        # POWTARZANIE TEGO SAMEGO ALARMU JEST SZKODLIWE. Stan „wycofywany" trwa
        # miesiacami; alarm co godzine przez kwartal jest nie do odroznienia od
        # tla. Zglaszamy wiec ZMIANE stanu albo stan, na ktory JESZCZE NIE
        # ZAREAGOWALISMY (instrument dalej w uniwersum). O problemie, ktory trwa
        # mimo reakcji — zamrozonej pozycji — przypomina dozor
        # (`dozor.py --co instrumenty`) w swoim, dobowym rytmie.
        nowy_stan = (r.get("status_gieldy") != status) or bool(r.get("tradeable"))

        czym = (f"status „{status}” u giełdy" if g is not None
                else "brak instrumentu w odpowiedzi giełdy — giełda go NIE ZNA")
        if klasa == "wycofanie":
            tresc = [
                f"{symbol}: {czym}.",
                "",
                "Przy wycofaniu giełda anuluje otwarte zlecenia, zatrzymuje "
                "handel, a po zamknięciu okna wypłat konwertuje niewycofane "
                "środki po ówczesnej cenie rynkowej. Proces liczy się "
                "w miesiącach.",
                "",
                "Instrument został zdjęty z uniwersum decyzyjnego — silnik nie "
                "wejdzie w niego ponownie.",
            ]
            if otwarte:
                tresc += [
                    "",
                    "OTWARTA POZYCJA — NIE JEST ZAMYKANA AUTOMATYCZNIE:",
                    _opis_pozycji(otwarte),
                    "",
                    "Zamknięcie jest decyzją handlową (cena, moment, limity), "
                    "a ten proces chodzi bez klucza API i bez wiedzy o rynku. "
                    "Dopóki pozycja jest otwarta, jej wartość zależy od cudzej "
                    "decyzji o kursie konwersji, a limity ryzyka liczą ją jak "
                    "zwykłą ekspozycję — i blokuje ona budżet kolejnym wejściom.",
                ]
            else:
                tresc += ["", "Pozycji na tym instrumencie nie ma — kapitał nie "
                              "jest zamrożony."]
            out.append({
                "symbol": symbol, "id": r["id"], "klasa": "wycofanie",
                "status": status, "zdjac": bool(r.get("tradeable")),
                "przywroc": False,
                "kod": "instrument.delisted" if nowy_stan else None,
                "tytul": (f"{symbol} wycofywany z giełdy"
                          + (" — MASZ OTWARTĄ POZYCJĘ" if otwarte else "")),
                "tresc": "\n".join(tresc),
                "pozycje": otwarte,
            })
            continue

        tresc = [
            f"{symbol}: {czym}.",
            "",
            "Handel tym instrumentem jest wstrzymany po stronie giełdy. "
            "Instrument jest chwilowo poza uniwersum decyzyjnym i wróci sam, "
            "gdy giełda znów poda Trading.",
        ]
        if otwarte:
            tresc += [
                "",
                "OTWARTA POZYCJA (wyjścia też nie da się teraz złożyć):",
                _opis_pozycji(otwarte),
                "",
                "Zawieszenie zwykle mija samo. Jeśli nie minie do końca doby, "
                "przestaje być przerwą — sprawdź komunikaty giełdy, bo "
                "wycofanie bywa zapowiadane właśnie przedłużającym się "
                "wstrzymaniem.",
            ]
        out.append({
            "symbol": symbol, "id": r["id"], "klasa": "zawieszenie",
            "status": status, "zdjac": bool(r.get("tradeable")),
            "przywroc": False,
            "kod": "instrument.suspended" if nowy_stan else None,
            "tytul": f"{symbol}: handel wstrzymany przez giełdę",
            "tresc": "\n".join(tresc),
            "pozycje": otwarte,
        })
    return out


async def zastosuj_dostepnosc(w_bazie: list[dict], z_gieldy: dict[str, dict],
                              pozycje: dict[str, list[dict]], *, execute, fetch_all,
                              notify, sucho: bool = False,
                              engine: str = "crypto") -> int:
    """Zapisuje status, zamyka bramkę i zgłasza. Zwraca liczbę spraw PILNYCH.

    DATA SPRAWDZENIA ZAPISYWANA JEST ZAWSZE, NIE TYLKO PRZY ZMIANIE.

    To jest cała różnica między „status jest Trading" a „status BYŁ Trading,
    gdy ktoś ostatnio pytał". Gdy to zadanie przestanie chodzić, kolumna
    statusu zamarza na ostatniej wartości i w każdym zapytaniu wygląda jak
    potwierdzenie. Bez daty sprawdzenia nie da się odróżnić ciszy „wszystko
    gra" od ciszy „nikt nie pyta" — a dozor odzywa się właśnie o tę drugą.
    """
    werdykty = ocen_dostepnosc(w_bazie, z_gieldy, pozycje)

    if not sucho:
        for r in w_bazie:
            g = z_gieldy.get(r["symbol"])
            # `IS DISTINCT FROM` zamiast `<>`, bo NULL (gielda nie zna
            # instrumentu) to tu wartosc znaczaca, a nie brak porownania.
            await execute(
                "UPDATE ref.instrument SET status_sprawdzony_at = now(),"
                "   status_gieldy_od = CASE"
                "     WHEN status_gieldy IS DISTINCT FROM %(st)s THEN now()"
                "     ELSE status_gieldy_od END,"
                "   status_gieldy = %(st)s WHERE id = %(id)s",
                {"st": g["status"] if g else None, "id": r["id"]})

    if not werdykty:
        print("status u giełdy: wszystkie instrumenty w uniwersum handlują")
        return 0

    odbiorcy = [] if sucho else await fetch_all(
        'SELECT id FROM app."user" WHERE is_active')
    pilne = 0
    for w in werdykty:
        print(f"  [{w['klasa']}] {w['tytul']}")
        if w["klasa"] == "wycofanie":
            pilne += 1
        if sucho:
            continue
        if w["zdjac"]:
            # BRAMKA. `tradeable` czyta petla decyzyjna, trener i sprawdzenie
            # mozliwosci - zdjecie flagi jest jedynym miejscem, w ktorym da sie
            # zatrzymac wejscie w rynek bez zgadywania ceny wyjscia.
            await execute(
                "UPDATE ref.instrument SET tradeable = false,"
                " wylaczony_przez_status = %s WHERE id = %s", (w["klasa"], w["id"]))
            print(f"    zdjęto z uniwersum decyzyjnego ({w['klasa']})")
        if w["przywroc"]:
            await execute(
                "UPDATE ref.instrument SET tradeable = true,"
                " wylaczony_przez_status = NULL WHERE id = %s", (w["id"],))
            print("    przywrócono do uniwersum decyzyjnego")
        if not w["kod"]:
            continue
        sev = "critical" if w["kod"] == "instrument.delisted" else "warning"
        for u in odbiorcy:
            await notify(u["id"], w["kod"], w["tytul"], w["tresc"],
                         engine=engine, severity=sev,
                         data={"symbol": w["symbol"], "status": w["status"],
                               "klasa": w["klasa"],
                               "pozycji_otwartych": len(w["pozycje"])})
        if odbiorcy:
            print(f"    powiadomiono: {len(odbiorcy)} odbiorców ({w['kod']})")
    return pilne


def podmien_odpowiedz(z_gieldy: dict[str, dict], udawaj: list[str]) -> dict[str, dict]:
    """Podmienia status w ODPOWIEDZI GIEŁDY, w pamięci. CZYSTA FUNKCJA.

    DLACZEGO ŚCIEŻKA ALARMOWA MUSI DAĆ SIĘ WYWOŁAĆ NA ŻĄDANIE

    Wszystkie dziesięć par uniwersum ma dziś status `Trading`, więc ścieżki
    wycofania nie da się dziś wywołać danymi z giełdy. Zabezpieczenie, które ma
    zadziałać raz na rok w najgorszym możliwym momencie, nie byłoby wtedy
    sprawdzone NIGDY — dokładnie tak jak czujka żywotności, która dostała
    `--prog-min` po to samo.

    Format: `SYMBOL=Status`, a `SYMBOL=BRAK` usuwa instrument z odpowiedzi,
    czyli udaje sytuację „giełda go już nie zna". Podmiana dotyczy wyłącznie
    odpowiedzi trzymanej w pamięci tego procesu; niczego nie udaje w bazie.
    """
    out = dict(z_gieldy)
    for wpis in udawaj:
        symbol, _, status = wpis.partition("=")
        symbol = symbol.strip().upper()
        status = status.strip()
        if not symbol or not status:
            raise ValueError(f"oczekiwano SYMBOL=STATUS, dostalem {wpis!r}")
        if status.upper() == "BRAK":
            out.pop(symbol, None)
            continue
        wzor = out.get(symbol) or {"krok_ilosci": None, "min_ilosc": None,
                                   "min_wartosc": None, "krok_ceny": None}
        out[symbol] = {**wzor, "status": status}
    return out


def niekompletne(w_bazie: list[dict]) -> list[str]:
    """Instrumenty dopuszczone do handlu, którym wciąż brakuje reguł."""
    return sorted(r["symbol"] for r in w_bazie if r.get("tradeable")
                  and (r.get("lot_size") is None or r.get("tick_size") is None
                       or r.get("min_notional") is None))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo", choices=("demo", "live"),
                    help="ktorego podmiotu regul uzywamy")
    ap.add_argument("--sucho", action="store_true",
                    help="pokaz, co by sie zmienilo, i nie zapisuj")
    ap.add_argument("--udawaj-status", action="append", default=[],
                    dest="udawaj_status", metavar="SYMBOL=STATUS",
                    help="podmien status w odpowiedzi gieldy (SYMBOL=BRAK udaje "
                         "znikniecie instrumentu); do wywolania sciezki "
                         "alarmowej na zadanie, gdy wszystko realnie handluje")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")

    from services.api.db import close_pool, execute, fetch_all, fetch_one, open_pool
    from services.api.notifications import notify
    from services.exchanges.bybit import BybitAdapter

    await open_pool()
    try:
        # HOST PODMIOTU, NIE GLOBALNY. Reguly zlecen obowiazuja tam, gdzie
        # zlecenie ladzie, a podmiot unijny ma je INNE: ADAUSDC ma u niego krok
        # ilosci 0.1 zamiast 0.01. Zapisanie regul globalnych konczy sie
        # odbiciem kazdego zlecenia komunikatem o "too many decimals", ktory
        # wskazuje na nasze zaokraglanie zamiast na zrodlo reguly.
        host = await fetch_one(
            "SELECT demo_host, live_host FROM ref.venue WHERE code = 'bybit'")
        if not host:
            sys.exit("brak wpisu ref.venue dla bybit - nie wiadomo, kogo pytac")
        adres = host["demo_host"] if args.mode == "demo" else host["live_host"]

        # BEZ KLUCZA. Endpoint instrumentow jest publiczny, wiec adapter
        # powstaje z pustymi poswiadczeniami - i to jest cecha, nie brak:
        # uzupelnienie regul nie zalezy od tego, czy klucz w ogole istnieje.
        adapter = BybitAdapter(api_key="", api_secret="", mode=args.mode, host=adres)
        try:
            z_gieldy = await adapter.filtry_instrumentow("spot")
        finally:
            await adapter.close()
        print(f"reguły z {adres} (podmiot trybu {args.mode})")

        if args.udawaj_status:
            z_gieldy = podmien_odpowiedz(z_gieldy, args.udawaj_status)
            print(f"UDAWANIE: {', '.join(args.udawaj_status)} — odpowiedź giełdy "
                  f"podmieniona w pamięci")

        w_bazie = await fetch_all(
            "SELECT id, symbol, lot_size, tick_size, min_notional, tradeable,"
            "       status_gieldy, wylaczony_przez_status,"
            "       (meta->>'min_ilosc') AS min_ilosc_meta"
            " FROM ref.instrument WHERE active AND engine::text = %s ORDER BY symbol",
            (args.engine,))
        print(f"giełda zna {len(z_gieldy)} instrumentów; w bazie {len(w_bazie)} "
              f"aktywnych ({args.engine})")

        # POZYCJE TEGO PODMIOTU, KTOREGO REGULY WLASNIE CZYTAMY. Lista
        # instrumentow rozni sie miedzy podmiotem globalnym a unijnym, wiec
        # pozycja z drugiego trybu odpowiadalaby na pytanie o INNA gielde.
        pozycje: dict[str, list[dict]] = {}
        for p in await fetch_all(
                "SELECT i.symbol, p.id, p.side, p.qty, p.avg_price, p.opened_at"
                "  FROM exec.position p JOIN ref.instrument i ON i.id = p.instrument_id"
                " WHERE p.closed_at IS NULL AND i.engine::text = %s"
                "   AND p.mode::text = %s ORDER BY p.opened_at",
                (args.engine, args.mode)):
            pozycje.setdefault(p["symbol"], []).append(p)

        do_zmiany = rozbieznosci(w_bazie, z_gieldy)
        for z in do_zmiany:
            if z["brak_u_gieldy"]:
                print(f"  BRAK U GIEŁDY {z['symbol']} — reguł nie ma skąd wziąć")
                continue
            opis = ", ".join(f"{k}={v}" for k, v in z["zmiany"].items()) or "(tylko meta)"
            print(f"  {z['symbol']}: {opis}")
            if args.sucho:
                continue
            if z["zmiany"]:
                sety = ", ".join(f"{k} = %({k})s" for k in z["zmiany"])
                await execute(f"UPDATE ref.instrument SET {sety} WHERE id = %(id)s",
                              {**z["zmiany"], "id": z["id"]})
            # STATUS NIE IDZIE JUZ DO `meta`. Mial tam kopie, ktorej nikt nie
            # czytal; od migracji 0033 jest kolumna, po ktorej da sie filtrowac
            # i ktora ma date sprawdzenia. Dwa miejsca z ta sama prawda
            # rozjezdzaja sie przy pierwszej zmianie.
            await execute(
                "UPDATE ref.instrument SET meta = coalesce(meta,'{}'::jsonb)"
                " || jsonb_build_object('min_ilosc', %s::text) WHERE id = %s",
                (str(z["min_ilosc"]) if z["min_ilosc"] is not None else None,
                 z["id"]))

        pilne = await zastosuj_dostepnosc(
            w_bazie, z_gieldy, pozycje, execute=execute, fetch_all=fetch_all,
            notify=notify, sucho=args.sucho, engine=args.engine)

        if args.sucho:
            print("\n(sucho — nic nie zapisano)")
            return 0

        po = await fetch_all(
            "SELECT symbol, lot_size, tick_size, min_notional, tradeable"
            " FROM ref.instrument WHERE active AND engine::text = %s", (args.engine,))
        brakuje = niekompletne(po)
        if brakuje:
            print(f"\nNADAL BEZ KOMPLETU REGUŁ ({len(brakuje)}): {', '.join(brakuje)}")
            print("Egzekucji nie wolno uruchamiać, dopóki ta lista nie jest pusta —"
                  " zlecenie bez reguł zostanie odrzucone, a odrzucenie w logu"
                  " wygląda jak brak sygnału.")
            return 1
        print("\nwszystkie instrumenty dopuszczone do handlu mają komplet reguł")
        # KOD NIEZEROWY PRZY WYCOFANIU, choc powiadomienie juz poszlo. To jest
        # drugi, niezalezny sygnal: nieudane zadanie widac w `kubectl get jobs`
        # takze wtedy, gdy kanal powiadomien zawiodl. Czujka polegajaca na
        # jednej drodze zgloszenia ma ten sam problem co system, ktory pilnuje.
        if pilne:
            print(f"WYCOFYWANE INSTRUMENTY: {pilne} — patrz powiadomienia; "
                  "otwarta pozycja NIE zostala zamknieta automatycznie.")
            return 1
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
