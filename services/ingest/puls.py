#!/usr/bin/env python3
"""Puls procesow zbierajacych dane - jedno miejsce, cztery workery.

DLACZEGO TO W OGOLE ISTNIEJE

Petla decyzyjna zapisuje `risk.engine_heartbeat`, dozorca `risk.dozorca_heartbeat`,
a `dozor.py --co zywotnosc` pilnuje obu. Workery danych nie zapisywaly pulsu
NIGDZIE, wiec proces ZAWIESZONY - a nie martwy - wygladal w `kubectl get pods`
na `Running` bez konca.

Licznik pieciu awarii pod rzad, ktory te moduly juz maja, tego przypadku
NIE LAPIE: zawieszony cykl nigdy sie nie konczy, wiec nigdy nie zwieksza
licznika. Licznik broni przed procesem, ktory PADA; puls broni przed procesem,
ktory STOI. To dwie rozne awarie i zaden z tych mechanizmow nie zastepuje
drugiego.

DLACZEGO WSPOLNY MODUL, A NIE CZTERY KOPIE ZAPYTANIA

Cztery kopie tego samego UPSERT-u rozjechalyby sie przy pierwszej zmianie
ksztaltu tabeli, a rozjazd nie objawilby sie awaria - objawilby sie jednym
workerem, ktory po cichu przestal bic. Tu jest jedno zapytanie i jedna
definicja tego, co znaczy „udany cykl".

DWIE ZASADY, KTORE TRZYMAJA TEN MECHANIZM UCZCIWYM

1. PULS BIJE PO UDANYM CYKLU, NIGDY NA STARCIE PROCESU.
   Proces, ktory wstal i od razu sie zawiesil, nie moze wygladac na zdrowy.
   Puls bity przy starcie dawalby mu pelen kwadrans zdrowia przy KAZDYM
   obrocie petli CrashLoopBackOff - czyli w nieskonczonosc.

2. CYKL ZAKONCZONY BLEDEM NIE ODSWIEZA `last_beat_at`.
   Puls „zyje" przy pieciu awariach pod rzad jest gorszy niz jego brak: mowi
   czujce dokladnie to, co czujka chce uslyszec, w chwili gdy jest najbardziej
   nieprawdziwe. Awaria dopisuje sie do OSOBNYCH kolumn (`ostatnia_proba_at`,
   `awarii_pod_rzad`, `ostatni_blad`), zeby czujka umiala odroznic „worker stoi"
   od „worker pracuje i nie daje rady" - ale alarm pozostaje JEDEN, bo jedna
   awaria ma dawac jedno powiadomienie.

CO ZNACZY „UDANY CYKL"

To, co w danym module ZNACZYLO TO JUZ WCZESNIEJ - dokladnie ten sam warunek,
ktory zeruje licznik `pod_rzad`. Druga, wlasna definicja awarii „na potrzeby
pulsu" bylaby gwarancja rozjazdu przy pierwszej zmianie: czujka mowilaby co
innego niz licznik restartow tego samego procesu.
"""
from __future__ import annotations

import os
import time

from psycopg.types.json import Jsonb

# Ile znakow opisu bledu trafia do bazy. Pelny slad zostaje w logu; tu ma sie
# zmiescic tyle, zeby powiadomienie mowilo, CZEGO awaria dotyczy, i nie wiecej -
# `ostatni_blad` z traceback'iem nadaje sie do czytania rownie dobrze co log,
# czyli wcale.
BLAD_MAX = 500

_UPSERT = (
    "INSERT INTO risk.worker_heartbeat"
    " (worker, interwal_s, last_beat_at, ostatnia_proba_at, awarii_pod_rzad,"
    "  ostatni_blad, detail)"
    " VALUES (%s, %s, now(), now(), 0, NULL, %s)"
    " ON CONFLICT (worker) DO UPDATE SET"
    "   interwal_s = EXCLUDED.interwal_s,"
    "   last_beat_at = now(), ostatnia_proba_at = now(),"
    "   awarii_pod_rzad = 0, ostatni_blad = NULL,"
    "   detail = EXCLUDED.detail")

# AWARIA TO UPDATE, NIE UPSERT - i to jest decyzja, nie niedopatrzenie.
#
# Gdyby awaria potrafila ZALOZYC wiersz, trzeba by czyms wypelnic `last_beat_at`,
# a kazda mozliwa wartosc jest klamstwem: `now()` twierdzi, ze cykl sie udal,
# a znacznik z przeszlosci wywolalby alarm o workerze, ktory wlasnie pierwszy raz
# wstaje i ma prawo do jednej nieudanej proby. Wiersz zaklada wiec PIERWSZY UDANY
# CYKL - dzieki temu istnienie wiersza znaczy dokladnie jedno: „ten worker
# kiedykolwiek domknal cykl".
#
# Worker, ktory nie domknal ANI JEDNEGO cyklu od wdrozenia, nie jest przez to
# bez nadzoru: pieciokrotna awaria konczy proces (CrashLoopBackOff widac
# w `kubectl get pods`), a brak danych lapie `dozor.py --co swiezosc`.
_AWARIA = (
    "UPDATE risk.worker_heartbeat SET"
    "   interwal_s = %s, ostatnia_proba_at = now(),"
    "   awarii_pod_rzad = %s, ostatni_blad = %s"
    " WHERE worker = %s")


def bij(conn, worker: str, interwal_s: int, detail: dict | None = None) -> None:
    """Puls po UDANYM cyklu. Wywolywac na koncu cyklu, nigdy przy starcie.

    ZAPIS JEST CZESCIA CYKLU, WIEC WOLNO MU CYKL PRZEWROCIC.

    Wywolujacy nie lapie tu wyjatku i to jest celowe. Gdyby nieudany zapis pulsu
    byl polykany, wiersz starzalby sie przy w pelni zdrowym workerze i czujka
    zglosilaby smierc procesu, ktory zyje. Falszywy alarm psuje czujke
    SKUTECZNIEJ niz jej brak - uczy odbiorce, ze mozna ja pominac. Niech wiec
    blad zapisu pulsu bedzie tym, czym jest: awaria cyklu, ktora idzie do
    licznika i do logu razem z pozostalymi.

    COMMIT TUTAJ, nie na koncu bloku `with`: cykl, ktory po pulsie wywroci sie
    na czyms innym i cofnie transakcje, cofnalby razem z nia dowod na to, ze
    dane jednak doszly.
    """
    conn.execute(_UPSERT, (worker, int(interwal_s), Jsonb(detail or {})))
    conn.commit()


def zglos_awarie(conn, worker: str, interwal_s: int, pod_rzad: int,
                 blad: str) -> None:
    """Cykl zakonczony bledem. NIE RUSZA `last_beat_at`.

    Odwrotnie niz `bij`: tu wyjatek jest lapiany i tylko wypisywany. Jestesmy
    juz na sciezce awaryjnej, wiec bledem, ktory ma zobaczyc czlowiek, jest ten
    PIERWOTNY - a nie to, ze nie udalo sie go zapisac. Zamiana ich miejscami
    schowalaby przyczyne za skutkiem.

    Milczenie tego zapisu i tak nie tworzy dziury w nadzorze: `last_beat_at`
    stoi w miejscu niezaleznie od tego, czy udalo sie dopisac szczegoly, wiec
    czujka i tak sie odezwie - tyle ze skapiej.
    """
    try:
        # ROLLBACK PRZED ZAPISEM: cykl przerwany wyjatkiem zostawia transakcje
        # w stanie `aborted`, w ktorym KAZDE kolejne zapytanie odbija sie
        # bledem. Bez tego zapis awarii padalby dokladnie wtedy, gdy jest
        # potrzebny - czyli nigdy nie zadzialalby na produkcji.
        conn.rollback()
        conn.execute(_AWARIA, (int(interwal_s), int(pod_rzad),
                               (blad or "")[:BLAD_MAX], worker))
        conn.commit()
    except Exception as e:                                    # noqa: BLE001
        # Szeroki `except` jest tu SWIADOMY. Jestesmy na sciezce awaryjnej:
        # wyjatek z zapisu szczegolow przykrylby wyjatek, ktory ma zobaczyc
        # czlowiek. Zamiana przyczyny na skutek jest gorsza niz skapszy wpis.
        print(f"    (nie udalo sie zapisac awarii do pulsu: {type(e).__name__}: {e})")


# ----------------------------------------------------------- sonda zywotnosci
#
# PLIK DLA `livenessProbe`, NIE DRUGI PULS - i odwrotnosc zasady 2 jest tu celowa.
#
# `bij` mowi czujce "cykl SIE UDAL", wiec cykl nieudany go nie odswieza. Plik
# mowi kubeletowi cos wezszego: "petla SIE OBRACA". Odswieza go KAZDY obrot -
# udany i nieudany - bo sonda ma lapac wylacznie proces ZAWIESZONY. Awarie
# lapia juz inne mechanizmy: licznik pieciu porazek konczy proces, a `bij`
# przestaje bic i czujka (`dozor.py --co workery`) budzi czlowieka.
#
# Gdyby plik odswiezal sie tylko po udanym cyklu, awaria bazy zatrzymalaby go
# we WSZYSTKICH workerach naraz i kubelet zrestartowalby caly potok w jednej
# minucie - restartem, ktory niczego nie naprawia, bo baza nadal lezy.
#
# TERMIN SIEDZI W PLIKU, NIE W CHARCIE. Proces wpisuje chwile, do ktorej ma
# prawo milczec (`teraz + termin`), a sonda porownuje ja z zegarem. Interwal
# zyje wiec dalej w jednym miejscu (`--interval`), a szablon nie dostaje jego
# trzeciej kopii - to byl drugi z powodow, dla ktorych sond do 2026-10-03 nie
# bylo (naglowek templates/workers.yaml).
PLIK_ZYWOTNOSCI = "/tmp/trader-zywy"

# Ile proces moze milczec, zanim kubelet uzna go za zawieszony.
#
# GODZINA, nie kwadrans. Czujka pulsu ma podloge 900 s (dozor.py, MIN_PROG_S),
# wiec przy zawieszeniu CZLOWIEK dostaje powiadomienie z nazwa workera DUZO
# wczesniej, niz kubelet cokolwiek zrestartuje. Sonda jest ostatnia linia, gdy
# nikt nie zareagowal - nie zamiennikiem alarmu, bo restart sam nikomu nic
# nie mowi.
#
# Zapas wzgledem najdluzszego cyklu: `features` na dwoch horyzontach to minuty,
# `history` z ponowieniami gieldy kilka minut. Godzina to rzad wielkosci zapasu,
# czyli sonda nie ubije dlugiego, ale zywego cyklu.
PODLOGA_TERMINU_S = 3600

# Dla workerow o dlugim interwale termin skaluje sie z nim: cztery opuszczone
# obroty petli to juz nie przypadek.
KROTNOSC_INTERWALU = 4


def termin_zywotnosci(interwal_s: int, podloga_s: int = PODLOGA_TERMINU_S) -> int:
    return max(int(podloga_s), KROTNOSC_INTERWALU * int(interwal_s))


def zyje(interwal_s: int, podloga_s: int = PODLOGA_TERMINU_S,
         plik: str | None = None) -> None:
    """Petla sie obraca. Wolac NA STARCIE procesu i po KAZDYM obrocie petli.

    Na starcie, bo pierwszy cykl tez moze sie zawiesic - a bez pliku sonda
    przepuszcza (patrz templates/workers.yaml: brak pliku znaczy "ten modul
    nie wpial jeszcze sondy", nie "zawieszony").

    Zapis atomowy (`os.replace`), bo sonda czyta plik w dowolnej chwili, a plik
    przeczytany w polowie zapisu bylby pustym napisem - czyli restartem zdrowego
    procesu. Blad zapisu tylko wypisujemy: proces, ktory nie umie pisac do /tmp,
    zostanie zrestartowany przez sonde, i to jest wlasciwa reakcja.
    """
    sciezka = plik or os.environ.get("PLIK_ZYWOTNOSCI", PLIK_ZYWOTNOSCI)
    termin = int(time.time()) + termin_zywotnosci(interwal_s, podloga_s)
    try:
        tymczasowy = f"{sciezka}.{os.getpid()}"
        with open(tymczasowy, "w", encoding="ascii") as f:
            f.write(str(termin))
        os.replace(tymczasowy, sciezka)
    except OSError as e:
        print(f"    (nie udalo sie zapisac pliku zywotnosci {sciezka}: {e})")
