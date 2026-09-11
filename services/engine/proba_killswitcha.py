"""Próba pożarowa kill-switcha — jako usługa, nie skrypt.

DLACZEGO TO NIE JEST PLIK .sh NA VPS-IE

Skrypt leżący na serwerze rozjeżdża się z kodem, który testuje: obraz idzie
przez CI i wdrożenie, plik na dysku nie idzie nigdzie. Po miesiącu nie wiadomo,
czy sprawdza wersję, która działa. Ten moduł jedzie w tym samym obrazie co
silnik, więc testuje dokładnie ten kod, który handluje.

Druga rzecz: próba uruchamiana ręcznie zostanie uruchomiona raz. Jako zadanie
cykliczne wykonuje się sama i odzywa się dopiero wtedy, gdy coś nie zadziała.

CO SPRAWDZA

Testy jednostkowe sprawdzają regułę: czy przekroczony limit żąda zatrzymania.
Nie sprawdzają, czy silnik ją woła, czy wiersz dociera do bazy, czy dozór to
zauważa i czy powiadomienie wychodzi. Kill-switch jest jedynym mechanizmem
w tym systemie, którego awaria nie objawia się niczym — silnik po jego
zadziałaniu wygląda tak samo jak silnik bez sygnału do wejścia.

    ogniwo 1  limit przekroczony -> żądanie zatrzymania
    ogniwo 2  żądanie -> wiersz w risk.killswitch_event
    ogniwo 3  aktywny -> silnik wstrzymał wejścia
    ogniwo 4  dozór -> powiadomienie

CO ZOSTAJE ZMIENIONE

Jedna wartość: próg kapitału podniesiony powyżej bieżącego stanu, żeby silnik
SAM wykrył przekroczenie. Przywracana w `finally`, więc wraca także po błędzie
i po przerwaniu — zostawiony podniesiony próg zatrzymałby silnik na stałe,
czyli spowodowałby dokładnie tę awarię, przed którą kill-switch ma chronić.

Próba używa progu kapitału, bo to jedyny z trzech limitów krytycznych z regułą
`reczne` — usługa wznowienia go nie ruszy i nie zwolni zdarzenia w połowie
pomiaru.

TRZY WYNIKI, NIE DWA

    ZALICZONA (0)          łańcuch zadziałał od limitu po powiadomienie
    NIEZALICZONA (1)       próba się WYKONAŁA i któreś ogniwo padło —
                           to jest awaria zabezpieczenia i ma krzyczeć
    NIEROZSTRZYGNIĘTA (0)  próby NIE DA SIĘ przeprowadzić, bo kill-switch już
                           stoi: nie ma czego wywołać ze stanu spoczynku
    NIESPRAWDZONE (2)      nierozstrzygnięta zbyt długo — patrz niżej

Do 2026-09-10 istniały dwa pierwsze i trzeci wpadał do drugiego, czyli aktywny
kill-switch był zgłaszany jako awaria samego kill-switcha. To błąd rozpoznania:
„nie dało się zmierzyć" to nie to samo co „zmierzono i wyszło źle". Skutek był
gorszy niż fałszywy alarm — zaufanie do próby spadało dokładnie wtedy, gdy
człowiek i tak wiedział o kill-switchu, a prawdziwa awaria łańcucha wyglądałaby
identycznie i utonęłaby w tych powtórkach.

NIEROZSTRZYGNIĘTA NIE JEST JEDNAK ZALICZONA. Kod zerowy znaczy tu tylko tyle, że
nie ma o czym budzić człowieka w nocy; komunikat mówi wprost, że w tym cyklu
NIKT nie sprawdził, czy bezpiecznik działa.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

LIMIT_PROBY = "min_equity"
# Ile czekać na cykl silnika (biegnie co 300 s) i na dozór (co 600 s).
CZEKAJ_SILNIK_S = int(os.environ.get("PROBA_CZEKAJ_SILNIK_S", "420"))
CZEKAJ_DOZOR_S = int(os.environ.get("PROBA_CZEKAJ_DOZOR_S", "660"))
KROK_S = 15

# ILE DÓB BEZ POTWIERDZENIA, ZANIM SAMA CISZA JEST AWARIĄ.
#
# Bezpiecznik niesprawdzony przez wiele dni to osobne ryzyko od bezpiecznika
# zepsutego — i jedyne, które rośnie samo. Trzeci wynik bez tego progu byłby
# workiem bez dna: „nie dało się zmierzyć" powtarzane w nieskończoność
# z zerowym kodem wyjścia niczym się nie różni od wyłączenia próby.
#
# Siedem, bo próg musi mieścić się między dwiema granicami:
#
#   OD DOŁU — zwykłą ludzką obsługę prawdziwego zatrzymania. Kill-switch, który
#   zadziałał w piątek wieczorem i został zwolniony w poniedziałek po analizie,
#   daje trzy doby nierozstrzygniętych prób. Próg 3 albo 5 zamieniałby normalny
#   tryb pracy w drugi alarm — o alarmie. Wyłączony byłby w tydzień.
#
#   OD GÓRY — opóźnienie wykrycia, które ten projekt już raz uznał za zbyt duże.
#   Próba jest dobowa właśnie dlatego, że przy tygodniowej o awarii bezpiecznika
#   dowiadujemy się średnio po trzech i pół dnia (patrz komentarz w
#   charts/trader/templates/proba-cron.yaml). Tydzień ciszy jest więc dokładnie
#   tą granicą, powyżej której „chwilowo nie sprawdzamy" staje się stanem.
#
# Świadomie BEZ odczytu ze środowiska: próg, który da się podnieść zmienną,
# zostanie podniesiony przy pierwszym niewygodnym przebiegu i nikt tego nie
# zauważy — tak samo jak nie widać wyłączonego limitu.
MAX_DNI_BEZ_POTWIERDZENIA = 7


class Niezaliczona(RuntimeError):
    """Ogniwo łańcucha nie zadziałało. To awaria zabezpieczenia, nie próby."""


class Nierozstrzygnieta(RuntimeError):
    """Próby NIE DAŁO SIĘ przeprowadzić. To nie jest wynik pomiaru.

    Niesie `dni` — ile dób minęło od ostatniego POTWIERDZENIA, że łańcuch
    działa. Bez tej liczby trzeci wynik byłby nieodróżnialny od sukcesu.
    """

    def __init__(self, powod: str, dni: float):
        super().__init__(powod)
        self.dni = dni


async def _czekaj_na(warunek, limit_s: int, opis: str):
    """Odpytuje warunek do skutku albo do limitu czasu. Zwraca wynik albo None."""
    minelo = 0
    while minelo < limit_s:
        wynik = await warunek()
        if wynik:
            return wynik
        await asyncio.sleep(KROK_S)
        minelo += KROK_S
    return None


async def _dni_od_potwierdzenia(fetch_one, engine: str, mode: str) -> float | None:
    """Ile dób od ostatniej ZALICZONEJ próby. None, gdy takiej nigdy nie było.

    ŚLAD JUŻ JEST W BAZIE — osobna tabela byłaby drugą kopią tej samej rzeczy.
    Każda zaliczona próba zwalnia zdarzenie, które sama wywołała, i zostawia
    parę kolumn, która nie powstaje inaczej:

        released_auto = true                       -> usługa wznowienia
        released_auto = false, released_by = ID    -> człowiek przez API
        released_auto = false, released_by = NULL  -> PRÓBA POŻAROWA

    Ta trójka jest już opisana niżej, przy samym zwolnieniu, i pilnuje jej test
    `test_proba_pozarowa_NIE_udaje_wznowienia_automatycznego`. Szukanie śladu po
    treści `release_note` działałoby do pierwszej zmiany sformułowania — ten sam
    błąd, przed którym ostrzega migracja 0029 przy `limit_name`.

    Liczymy od `released_at`, nie od `triggered_at`: potwierdzeniem jest moment,
    w którym łańcuch przeszedł do końca, a nie ten, w którym się zaczął.

    Doby liczy BAZA. Odejmowanie dat po stronie Pythona wymagałoby zgadywania
    strefy — a strefa pomylona o dwie godziny przy progu liczonym w dobach jest
    niewidoczna dokładnie do dnia, w którym ma zadziałać.
    """
    r = await fetch_one(
        "SELECT extract(epoch FROM now() - max(released_at)) / 86400.0 AS dni"
        " FROM risk.killswitch_event"
        " WHERE engine::text=%s AND mode::text=%s AND released_at IS NOT NULL"
        "   AND released_auto = false AND released_by IS NULL", (engine, mode))
    if r and r["dni"] is not None:
        return float(r["dni"])
    return None


async def przeprowadz(fetch_all, fetch_one, execute, engine: str = "crypto",
                      mode: str = "demo", sucho: bool = False) -> int:
    stan = await fetch_one(
        "SELECT round(equity::numeric, 2) AS equity FROM exec.equity_point"
        " WHERE engine=%s AND mode=%s ORDER BY event_time DESC LIMIT 1",
        (engine, mode))
    if not stan:
        raise Niezaliczona("brak zapisu kapitału — próba nie ma czym wyzwolić")

    lim = await fetch_one(
        "SELECT value FROM risk.limit_def WHERE engine::text=%s AND mode::text=%s"
        "   AND name=%s AND enabled", (engine, mode, LIMIT_PROBY))
    if not lim:
        raise Niezaliczona(f"limit {LIMIT_PROBY} nie istnieje")

    aktywne = await fetch_one(
        "SELECT count(*) AS n,"
        "       extract(epoch FROM now() - min(triggered_at)) / 86400.0"
        "           AS dni_blokady"
        " FROM risk.killswitch_event"
        " WHERE engine::text=%s AND mode::text=%s AND released_at IS NULL",
        (engine, mode))
    if aktywne["n"]:
        # NIE JEST TO NIEPOWODZENIE ŁAŃCUCHA, tylko brak możliwości pomiaru:
        # próba wywołuje kill-switch ze stanu spoczynku, a tego stanu nie ma.
        # (Nie odróżniłaby też swojego zdarzenia od zastanego i zwolniłaby
        # cudze — dlatego nie próbuje mimo wszystko.)
        dni = await _dni_od_potwierdzenia(fetch_one, engine, mode)
        if dni is None:
            # Ani jednej zaliczonej próby w dzienniku: świeże wdrożenie albo
            # taki system, w którym próba nigdy nie doszła do końca. Dolną
            # granicą ciszy jest wtedy wiek najstarszego czynnego zdarzenia —
            # przez ten czas próba na pewno nie mogła się wykonać. Zero
            # zerowałoby licznik na zawsze i próg byłby martwy.
            dni = float(aktywne["dni_blokady"] or 0.0)
        raise Nierozstrzygnieta(
            f"kill-switch jest już aktywny ({aktywne['n']}) — zwolnij go najpierw",
            dni)

    kapital = float(stan["equity"])
    prog_przed = float(lim["value"])
    prog_proby = round(kapital + 500)
    print(f"=== PRÓBA POŻAROWA KILL-SWITCHA: {engine}/{mode} ===")
    print(f"kapitał {kapital:.2f}   próg przed próbą {prog_przed:.2f}   aktywnych 0")

    if sucho:
        print(f"Tryb suchy: podniósłbym {LIMIT_PROBY} na {prog_proby} i czekał "
              f"na cykl silnika. Nic nie zmieniam.")
        return 0

    try:
        print(f"\n--- podnoszę {LIMIT_PROBY} na {prog_proby} (powyżej kapitału) ---")
        await execute(
            "UPDATE risk.limit_def SET value = %s, updated_at = now()"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s",
            (prog_proby, engine, mode, LIMIT_PROBY))

        print(f"\n--- OGNIWO 1-2: czekam na cykl silnika (do {CZEKAJ_SILNIK_S}s) ---")

        async def zdarzenie():
            return await fetch_one(
                "SELECT id, reason, limit_name FROM risk.killswitch_event"
                " WHERE engine::text=%s AND mode::text=%s AND released_at IS NULL"
                " ORDER BY triggered_at DESC LIMIT 1", (engine, mode))

        z = await _czekaj_na(zdarzenie, CZEKAJ_SILNIK_S, "zdarzenie")
        if not z:
            raise Niezaliczona(
                "silnik nie uruchomił kill-switcha mimo kapitału poniżej progu")
        print(f"  zadziałał: id={z['id']} limit={z['limit_name']} — {z['reason']}")
        if z["limit_name"] != LIMIT_PROBY:
            raise Niezaliczona(
                f"zadziałał inny limit ({z['limit_name']}) — próba mierzy nie to, "
                f"co zakłada")

        print(f"\n--- OGNIWO 4: czy dozór zgłosił (do {CZEKAJ_DOZOR_S}s) ---")

        async def powiadomienie():
            r = await fetch_one(
                "SELECT count(*) AS n FROM notify.notification"
                " WHERE event_code = 'killswitch.triggered'"
                "   AND created_at > (SELECT triggered_at FROM risk.killswitch_event"
                "                     WHERE id = %s)", (z["id"],))
            return r["n"] or None

        n = await _czekaj_na(powiadomienie, CZEKAJ_DOZOR_S, "powiadomienie")
        if not n:
            # Cichy kill-switch jest gorszy niz jego brak, bo daje zludzenie
            # kontroli. To jest osobne niepowodzenie od braku zadzialania.
            raise Niezaliczona(
                "kill-switch zadziałał, a nikt się o tym nie dowiedział")
        print(f"  powiadomień: {n}")

        # ZWOLNIENIE PRZEZ PROBE NIE JEST WZNOWIENIEM AUTOMATYCZNYM.
        #
        # `released_auto` istnieje po to, zeby dalo sie odpowiedziec na jedno
        # pytanie: CZY SYSTEM WROCIL DO HANDLU SAM. Ustawianie jej przez probe
        # zlewa dwa rozne zdarzenia w jedno - a wtedy odpowiedz na to pytanie
        # wymaga czytania tresci notatki, czyli flaga przestaje cokolwiek
        # rozstrzygac. Przy dobowej probie takich wierszy przybywa jeden dziennie
        # i po miesiacu to one zdominowalyby statystyke wznowien automatycznych.
        #
        # Proba sprzata wiec po sobie jako zwolnienie NIEAUTOMATYCZNE.
        #
        # ALE ZWALNIAJACYM NIE JEST CZLOWIEK, wiec `released_by` zostaje puste.
        # Ta kolumna jest typu bigint z kluczem obcym do app."user" (migracja
        # 0007) - miesci sie w niej wylacznie IDENTYFIKATOR KONTA, nie nazwa
        # procesu. Wpisanie tam napisu wywraca zapytanie na wiazaniu parametru,
        # a wpisanie ID wlasciciela byloby falszem w jedynym miejscu, ktore
        # odpowiada na pytanie "kto pozwolil systemowi znowu ryzykowac
        # pieniadze": interfejs pokazuje login wziety wlasnie stad
        # (services/api/routers/risk.py). Proba nie ma zreszta nadania na te
        # kolumne - 0034 daje jej UPDATE tylko na (released_at, released_auto,
        # release_note) - i to jest decyzja, a nie przeoczenie: proba nie jest
        # osoba i nie ma jej czym byc.
        #
        # Zrodlo zwolnienia rozstrzyga wiec TROJKA KOLUMN, bez czytania notatki:
        #
        #   released_auto = true                       -> wznowila sie uslugą
        #                                                 wznowienia (system SAM)
        #   released_auto = false, released_by = ID    -> zwolnil czlowiek
        #   released_auto = false, released_by = NULL  -> zwolnila proba pozarowa
        #
        # Notatka niesie reszte: co to bylo i skad przyszlo.
        await execute(
            "UPDATE risk.killswitch_event SET released_at = now(),"
            " released_auto = false, release_note = %s"
            " WHERE id = %s",
            ("PRÓBA POŻAROWA — zdarzenie wywołane celowo przez "
             "services/engine/proba_killswitcha.py", z["id"]))
        print("\n=== PRÓBA ZALICZONA: wszystkie ogniwa zadziałały ===")
        return 0
    finally:
        # PRZYWRACANIE W `finally`, nie na końcu bloku: próg musi wrócić także
        # wtedy, gdy próba padnie w połowie albo zostanie przerwana.
        print(f"\n--- przywracanie {LIMIT_PROBY} na {prog_przed:.2f} ---")
        await execute(
            "UPDATE risk.limit_def SET value = %s, updated_at = now()"
            " WHERE engine::text=%s AND mode::text=%s AND name=%s",
            (prog_przed, engine, mode, LIMIT_PROBY))
        po = await fetch_one(
            "SELECT value FROM risk.limit_def WHERE engine::text=%s AND mode::text=%s"
            "   AND name=%s", (engine, mode, LIMIT_PROBY))
        print(f"    próg po przywróceniu: {float(po['value']):.2f}")


async def uruchom(fetch_all, fetch_one, execute, **kw) -> int:
    """Przeprowadza próbę i zamienia jej wynik na KOD WYJŚCIA.

    Rozdział wyników od kodów siedzi tutaj, a nie w `main`, bo `main` zajmuje
    się pulą połączeń i argumentami — a to jest jedyne miejsce, w którym zapada
    decyzja „czy CronJob ma zaświecić na czerwono". Osobna funkcja znaczy też,
    że tę decyzję da się sprawdzić testem bez bazy.

        0  zaliczona albo nierozstrzygnięta w granicach ciszy
        1  NIEZALICZONA — łańcuch padł, zabezpieczenie nie działa
        2  NIESPRAWDZONE zbyt długo — cisza sama stała się awarią

    Dwójka zamiast jedynki nie jest ozdobnikiem: oba kody kończą pod na
    czerwono, ale mówią o dwóch różnych rzeczach i wymagają dwóch różnych
    reakcji (napraw łańcuch / zwolnij kill-switch albo zdecyduj świadomie, że
    handel stoi dalej). W logu zadania widać je bez czytania treści.
    """
    try:
        return await przeprowadz(fetch_all, fetch_one, execute, **kw)
    except Niezaliczona as e:
        print(f"\nNIEZALICZONA: {e}", file=sys.stderr)
        print("  To jest awaria samego zabezpieczenia, nie próby.", file=sys.stderr)
        return 1
    except Nierozstrzygnieta as e:
        print(f"\nNIEROZSTRZYGNIĘTA: {e}", file=sys.stderr)
        print("  ZABEZPIECZENIE NIE ZOSTAŁO W TYM CYKLU SPRAWDZONE — to nie "
              "jest potwierdzenie,", file=sys.stderr)
        print("  że kill-switch zadziała, tylko brak możliwości sprawdzenia.",
              file=sys.stderr)
        print(f"  Od ostatniego potwierdzenia: {e.dni:.1f} doby "
              f"(próg: {MAX_DNI_BEZ_POTWIERDZENIA}).", file=sys.stderr)
        if e.dni > MAX_DNI_BEZ_POTWIERDZENIA:
            print(f"\nNIESPRAWDZONE od {e.dni:.1f} doby — sama ta cisza jest "
                  f"już awarią.", file=sys.stderr)
            print("  Handel stoi, więc nic nie ryzykuje TERAZ. Ryzyko powstaje "
                  "w chwili zwolnienia", file=sys.stderr)
            print("  kill-switcha: system wróci do handlu z bezpiecznikiem, "
                  "którego od tygodnia", file=sys.stderr)
            print("  nikt nie sprawdził.", file=sys.stderr)
            return 2
        return 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="crypto")
    ap.add_argument("--mode", default="demo")
    ap.add_argument("--sucho", action="store_true",
                    help="wypisz plan, nie dotykaj limitu")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL nie jest ustawiony")
    if args.mode != "demo" and not os.environ.get("PROBA_NA_LIVE"):
        # Na koncie live proba wstrzymuje wejscia na kilkanascie minut. To nie
        # jest katastrofa, ale nie jest tez niczym - wymaga swiadomej zgody.
        sys.exit("Próba na koncie innym niż demo wymaga PROBA_NA_LIVE=1")

    from services.api.db import close_pool, execute, fetch_all, fetch_one, open_pool

    await open_pool()
    try:
        return await uruchom(fetch_all, fetch_one, execute,
                             engine=args.engine, mode=args.mode,
                             sucho=args.sucho)
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
