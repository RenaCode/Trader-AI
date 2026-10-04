#!/usr/bin/env python3
"""Uruchamia polecenie CronJobu i dopisuje jego wynik do `risk.job_run`.

    python services/engine/przebieg_zadania.py --nazwa trainer -- sh -c '...'

DLACZEGO (audyt Fable F-8.4, migracja 0074)

Nieudany job byl widoczny wylacznie w `kubectl get jobs`. Kubernetes nie wysyla
powiadomien, a historia jobow ginie po `failedJobsHistoryLimit`. Opakowanie
dopisuje jeden wiersz PO zakonczeniu polecenia; `dozor --co zadania` czyta je
w `trader-zywotnosc`.

KOD WYJSCIA JEST KODEM POLECENIA. Opakowanie nie zmienia tego, co widzi
Kubernetes - nieudany zapis do bazy jest ostrzezeniem w logu, nie awaria
zadania. Brak wiersza czujka i tak zobaczy jako cisze.

SYGNALY IDA DO POLECENIA. Opakowanie jest PID 1 kontenera, wiec to ono dostaje
SIGTERM przy `activeDeadlineSeconds` albo usunieciu poda. Proba pozarowa ma
wlasny handler SIGTERM, ktory przywraca prog limitu - sygnal musi do niej dojsc,
inaczej zginie razem z podem z podniesionym progiem.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit


def zapisz(nazwa: str, czas_s: float, kod: int) -> bool:
    """Jeden wiersz w `risk.job_run`. Czas startu liczony zegarem BAZY
    (`now() - czas trwania`), tak jak `finished_at` - dwa zegary rozjechalyby
    sie i przebieg moglby skonczyc sie przed swoim poczatkiem."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(f"przebieg_zadania: DATABASE_URL nie ustawiony — wynik {nazwa} "
              f"(kod {kod}) NIE ZAPISANY", file=sys.stderr)
        return False
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=10) as c:
            c.execute(
                "INSERT INTO risk.job_run (nazwa, started_at, exit_code, pod)"
                " VALUES (%s, now() - make_interval(secs => %s), %s, %s)",
                (nazwa, round(czas_s, 3), kod, os.environ.get("HOSTNAME")))
        return True
    except Exception as e:                                    # noqa: BLE001
        print(f"przebieg_zadania: wynik {nazwa} (kod {kod}) NIE ZAPISANY — "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return False


def czekaj_na_baze(limit_s: float = 60.0, odstep_s: float = 2.0,
                   polacz=socket.create_connection) -> bool:
    """Czeka, az port bazy przyjmie polaczenie TCP - zanim ruszy polecenie.

    KUBE-ROUTER WPUSZCZA NOWY POD Z OPOZNIENIEM. Regula NetworkPolicy dla
    swiezego poda zadania (etykieta `trader.renacode.com/component: job`)
    pojawia sie kilka sekund po jego starcie, a do tego czasu baza odpowiada
    `Connection refused`. 2026-10-04 02:40 UTC trener 4h polaczyl sie w
    pierwszej sekundzie i padl; tor 1h, ruszajacy pare minut pozniej, przeszedl.

    Brak `DATABASE_URL` albo hosta = nic do sprawdzania. Po `limit_s` polecenie
    rusza i tak: jego wlasny blad bedzie czytelniejszy niz ten tutaj."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return True
    adres = urlsplit(url)
    if not adres.hostname:
        return True
    port = adres.port or 5432
    koniec = time.monotonic() + limit_s
    while True:
        try:
            polacz((adres.hostname, port), timeout=3).close()
            return True
        except OSError as e:
            if time.monotonic() >= koniec:
                print(f"przebieg_zadania: baza {adres.hostname}:{port} nieosiagalna "
                      f"po {limit_s:.0f} s ({type(e).__name__}) — uruchamiam mimo to",
                      file=sys.stderr)
                return False
            time.sleep(odstep_s)


def uruchom(nazwa: str, polecenie: list[str]) -> int:
    czekaj_na_baze()
    start = time.monotonic()
    proc = subprocess.Popen(polecenie)

    def przekaz(sig, _ramka):
        proc.send_signal(sig)

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, przekaz)
    kod = proc.wait()
    # Zabity sygnalem: Popen oddaje -N, powloka i Kubernetes mowia 128+N.
    if kod < 0:
        kod = 128 - kod
    zapisz(nazwa, time.monotonic() - start, kod)
    return kod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nazwa",
                    help="nazwa zadania w risk.job_run, np. trainer")
    # SAMO CZEKANIE, BEZ POLECENIA I BEZ WIERSZA W `risk.job_run` - dla
    # kontenerow, ktore nie sa CronJobem (hook `kopia-przed-migracja`), a lacza
    # sie z baza w pierwszej sekundzie zycia poda. Kod 1, gdy baza nie odpowie:
    # tam polecenie bez bazy i tak nie ma sensu, a nieudany kontener ponowi Job.
    ap.add_argument("--tylko-czekaj", type=float, metavar="S", dest="tylko_czekaj",
                    help="poczekaj najwyzej S sekund na port bazy i zakoncz")
    ap.add_argument("polecenie", nargs=argparse.REMAINDER,
                    help="polecenie po `--`")
    args = ap.parse_args(argv)
    if args.tylko_czekaj is not None:
        return 0 if czekaj_na_baze(limit_s=args.tylko_czekaj) else 1
    if not args.nazwa:
        ap.error("--nazwa jest wymagane (poza --tylko-czekaj)")
    polecenie = args.polecenie[1:] if args.polecenie[:1] == ["--"] else args.polecenie
    if not polecenie:
        ap.error("brak polecenia po `--`")
    return uruchom(args.nazwa, polecenie)


if __name__ == "__main__":
    sys.exit(main())
