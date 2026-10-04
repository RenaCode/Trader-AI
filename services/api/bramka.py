"""Alarmy krytyczne poza panel: bramka powiadomien RenaCode (WhatsApp, zapasowo mail).

DECYZJA WLASCICIELA 2026-10-04. Alarm krytyczny, ktory zostaje w dzwonku, jest
alarmem dla kogos, kto akurat patrzy w dashboard - czyli dla nikogo w chwili,
w ktorej jest potrzebny. Bramka (renacode-infra, charts/bramka) jest wspolnym
kanalem aplikacji w klastrze: sama wybiera WhatsApp, gdy sesja dziala, a mail,
gdy nie. Klient zna tylko adres i SWOJ klucz wysylkowy.

CO TU IDZIE. Kazde powiadomienie o `severity = critical` - niezaleznie od
kodu zdarzenia, bo lista kodow bylaby druga kopia katalogu `notify.event_type`
i rozjechalaby sie przy pierwszym nowym alarmie. Panel zostaje bez zmian:
bramka jest DODATKOWYM torem, `notify()` zapisuje wiersz w dzwonku zanim tu
dojdzie.

DLACZEGO TLUMIENIE TUTAJ, A NIE TYLKO W CZUJKACH. Czujki maja wlasne rytmy
(np. `dozor.ocen_kill_switch`), ale `always_on` omija cooldown katalogu,
a `trader-zywotnosc` chodzi co 10 minut. Trwaly rozjazd z gielda dawal wiec
144 alarmy na dobe - w dzwonku to szum, na telefonie to powod, zeby wyciszyc
numer bota. Wtedy nie dochodzi juz NIC. Dwie granice:

  * TEN SAM ALARM (kod + silnik + tytul) - pierwszy idzie od razu, kolejne co
    1 h, 2 h, 4 h, potem co 6 h (`wymagana_przerwa_min`). Alarm, ktory trwa,
    przypomina o sobie coraz rzadziej; nowy alarm (inny tytul) idzie od razu.
  * WSZYSTKIE ALARMY - najwyzej LIMIT_NA_GODZINE wyslanych na godzine.

Licznik to `notify.delivery` z `kind = 'bramka'` (migracja 0092), wiec widza go
wszystkie procesy - API, dozorcy, CronJoby - i przezywa restart. Stlumiony
alarm tez zostawia wiersz (`suppressed_cooldown`) z powodem w `error`.

NIEUDANA WYSYLKA NIGDY NIE PRZERYWA WOLAJACEGO. `przekaz()` nie rzuca: brak
klucza, martwa bramka, blad zapisu sladu - wszystko konczy sie linia w logu
i wierszem `failed`, a nie wyjatkiem w czujce albo w obsludze zadania.

NIEUDANE PROBY TEZ MAJA RYTM (przeglad 2026-10-04). Wczesniej liczone byly
tylko udane wysylki, wiec przy martwej bramce KAZDE wywolanie `notify()` tego
samego alarmu probowalo od nowa i czekalo na timeout - w czujce, ktora ma
jeszcze kilkanascie sprawdzen do zrobienia. Teraz ten sam alarm po nieudanej
probie czeka 10, 20, 40 min... (najwyzej 6 h, `przerwa_po_bledzie_min`), a
pierwsza ponowna proba i tak przypada na nastepny przebieg `trader-zywotnosc`
(co 10 min). Polaczenie ma krotki termin (`TIMEOUT`, 3 s): martwa bramka
odpowiada odmowa albo milczeniem, a na odmowe nie ma po co czekac 25 s.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

URL_DOMYSLNY = "http://bramka.bramka.svc.cluster.local/v1/wyslij"

# Bramka czeka na WhatsApp do 15 s (rownolegle do wszystkich odbiorcow), a gdy
# nie doszlo do nikogo - na Mailgun. 25 s miesci oba bez trzymania czujki
# w nieskonczonosc; CronJob i tak ma wlasny termin. NAWIAZANIE polaczenia ma
# osobny, krotki termin: w klastrze trwa milisekundy, wiec 3 s bez odpowiedzi
# znaczy martwy pod albo NetworkPolicy, a nie wolna bramke.
TIMEOUT_S = 25.0
TIMEOUT = httpx.Timeout(TIMEOUT_S, connect=3.0)

PIERWSZA_PRZERWA_MIN = 60
MAKS_PRZERWA_MIN = 360
PIERWSZA_PRZERWA_PO_BLEDZIE_MIN = 10
LIMIT_NA_GODZINE = 10

# WhatsApp przyjmuje dlugie wiadomosci, ale tresc dluzsza niz ekran telefonu
# nie jest czytana. Pelna tresc jest w panelu - tu ma dojsc sedno.
MAKS_TRESC = 3000

PRIORYTET = {"critical": "alarm", "warning": "wazne", "info": "informacja"}

_brak_klucza_zgloszony = False


def klucz() -> str | None:
    """Klucz wysylkowy tradera (`BRAMKA_KLUCZ_TRADER` w bramce). None = brak."""
    k = os.environ.get("BRAMKA_KLUCZ", "").strip()
    return k or None


def skonfigurowana() -> bool:
    return klucz() is not None


def wymagana_przerwa_min(wyslanych_24h: int) -> int:
    """Ile minut od ostatniej wysylki TEGO SAMEGO alarmu. CZYSTA FUNKCJA.

    0 wyslanych = od razu; potem 60, 120, 240 i dalej co 360 minut. Licznik
    obejmuje dobe, wiec alarm, ktory ucichl na dobe, zaczyna od nowa.
    """
    if wyslanych_24h <= 0:
        return 0
    return min(PIERWSZA_PRZERWA_MIN * 2 ** (wyslanych_24h - 1), MAKS_PRZERWA_MIN)


def przerwa_po_bledzie_min(nieudanych_24h: int) -> int:
    """Ile minut od ostatniej NIEUDANEJ proby tego samego alarmu. CZYSTA FUNKCJA.

    Krocej niz po udanej wysylce: alarm, ktory nie doszedl, nadal nie dotarl
    do czlowieka - ma probowac znowu, tylko nie przy kazdym wywolaniu.
    0 nieudanych = od razu; potem 10, 20, 40... i najwyzej co 360 minut.
    """
    if nieudanych_24h <= 0:
        return 0
    return min(PIERWSZA_PRZERWA_PO_BLEDZIE_MIN * 2 ** (nieudanych_24h - 1),
               MAKS_PRZERWA_MIN)


def ocen_powtorke(wyslanych_24h: int, od_ostatniej_min: float | None,
                  wyslanych_w_godzinie: int, nieudanych_24h: int = 0,
                  od_nieudanej_min: float | None = None) -> str | None:
    """Powod stlumienia albo None (wysylamy). CZYSTA FUNKCJA."""
    przerwa = wymagana_przerwa_min(wyslanych_24h)
    if przerwa and od_ostatniej_min is not None and od_ostatniej_min < przerwa:
        return (f"ten sam alarm wysłany {od_ostatniej_min:.0f} min temu "
                f"({wyslanych_24h}× w dobie), następny najwcześniej po {przerwa} min")
    przerwa = przerwa_po_bledzie_min(nieudanych_24h)
    if przerwa and od_nieudanej_min is not None and od_nieudanej_min < przerwa:
        return (f"ten sam alarm NIE przeszedł przez bramkę {od_nieudanej_min:.0f} min "
                f"temu ({nieudanych_24h}× w dobie), następna próba najwcześniej po "
                f"{przerwa} min")
    if wyslanych_w_godzinie >= LIMIT_NA_GODZINE:
        return (f"limit {LIMIT_NA_GODZINE} alarmów na godzinę wyczerpany "
                f"— reszta jest w panelu")
    return None


def tresc_wiadomosci(body: str, event_code: str, nid: int | None) -> str:
    """Tresc dla bramki: sedno z panelu i jednoliniowy przypis, skad to jest."""
    tresc = (body or "").strip()
    if len(tresc) > MAKS_TRESC:
        tresc = tresc[:MAKS_TRESC].rstrip() + "…\n\n(pełna treść w panelu)"
    numer = f" #{nid}" if nid is not None else ""
    # Ostatni jednoliniowy blok konczacy sie `)` bramka sklada jako szary przypis.
    przypis = f"(zdarzenie {event_code}{numer}, panel trader.renacode.com)"
    return f"{tresc}\n\n{przypis}" if tresc else przypis


async def wyslij(temat: str, tresc: str, *, priorytet: str = "alarm",
                 rodzaj: str | None = None, klient: httpx.AsyncClient | None = None
                 ) -> dict:
    """Jedno `POST /v1/wyslij`. Zwraca odpowiedz bramki; rzuca, gdy nie przyjela."""
    k = klucz()
    if not k:
        raise RuntimeError("brak BRAMKA_KLUCZ w środowisku")
    url = os.environ.get("BRAMKA_URL", "").strip() or URL_DOMYSLNY
    cialo = {"temat": temat[:200], "tresc": tresc, "priorytet": priorytet}
    if rodzaj:
        cialo["rodzaj"] = rodzaj[:40]
    naglowki = {"X-Api-Key": k}
    if klient is None:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(url, json=cialo, headers=naglowki)
    else:
        r = await klient.post(url, json=cialo, headers=naglowki)
    try:
        odp = r.json()
    except ValueError:
        odp = {}
    # Bramka odpowiada 502 z `ok: false`, gdy zaden kanal nie przyjal wiadomosci;
    # 401/403 znaczy zly klucz albo klucz bez prawa wysylki.
    if r.status_code != 200 or not odp.get("ok"):
        szczegol = odp.get("blad") or odp.get("bledy") or r.text[:200]
        raise RuntimeError(f"bramka zwróciła {r.status_code}: {szczegol}")
    return odp


# `ten` = ten sam alarm (kod + tytul + silnik). Limit godzinowy liczy tylko
# wyslane - to on chroni telefon, a nieudana proba na telefon nie doszla.
def url_stanu(url_wysylki: str) -> str:
    """`.../v1/wyslij` -> `.../v1/stan`. CZYSTA FUNKCJA."""
    baza = url_wysylki.rstrip("/")
    if baza.endswith("/v1/wyslij"):
        baza = baza[: -len("/v1/wyslij")]
    return f"{baza}/v1/stan"


async def stan(*, klient: httpx.AsyncClient | None = None) -> dict:
    """`GET /v1/stan` z kluczem tradera: czy bramka ZNA ten klucz i ktory kanal
    jest aktywny. Rzuca, gdy klucza brak albo bramka go odrzuca (401/403).

    Bez wysylania wiadomosci - dla czujki `dozor --co kanaly`, ktorej pytanie
    brzmi "czy alarm ma jak wyjsc", a nie "wyslij cos, zeby sprawdzic"."""
    k = klucz()
    if not k:
        raise RuntimeError("brak BRAMKA_KLUCZ w środowisku")
    url = url_stanu(os.environ.get("BRAMKA_URL", "").strip() or URL_DOMYSLNY)
    if klient is None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as c:
            r = await c.get(url, headers={"X-Api-Key": k})
    else:
        r = await klient.get(url, headers={"X-Api-Key": k})
    try:
        odp = r.json()
    except ValueError:
        odp = {}
    if r.status_code != 200:
        raise RuntimeError(f"bramka zwróciła {r.status_code}: "
                           f"{odp.get('blad') or r.text[:200]}")
    return odp


SQL_POWTORKI = (
    "SELECT count(*) FILTER (WHERE x.ten AND x.status = 'sent') AS ten_sam,"
    "       round(extract(epoch FROM now() - max(x.created_at) FILTER ("
    "             WHERE x.ten AND x.status = 'sent')) / 60) AS od_ostatniej_min,"
    "       count(*) FILTER (WHERE x.status = 'sent'"
    "                          AND x.created_at > now() - interval '1 hour') AS w_godzinie,"
    "       count(*) FILTER (WHERE x.ten AND x.status = 'failed') AS nieudanych,"
    "       round(extract(epoch FROM now() - max(x.created_at) FILTER ("
    "             WHERE x.ten AND x.status = 'failed')) / 60) AS od_nieudanej_min"
    "  FROM (SELECT d.status::text AS status, d.created_at,"
    "               (n.event_code = %s AND n.title = %s"
    "                AND n.engine::text IS NOT DISTINCT FROM %s) AS ten"
    "          FROM notify.delivery d"
    "          JOIN notify.notification n ON n.id = d.notification_id"
    "         WHERE d.kind = 'bramka' AND d.status IN ('sent', 'failed')"
    "           AND d.created_at > now() - interval '24 hours') x")

SQL_SLAD = (
    "INSERT INTO notify.delivery"
    " (notification_id, channel_id, kind, status, attempts, error, sent_at)"
    " VALUES (%s, NULL, 'bramka', %s, %s, %s,"
    "         CASE WHEN %s = 'sent' THEN now() END)")


async def _slad(execute, nid: int, status: str, proby: int, blad: str | None) -> None:
    try:
        await execute(SQL_SLAD, (nid, status, proby, blad and blad[:400], status))
    except Exception as e:                                    # noqa: BLE001
        print(f"[bramka] ślad dostarczenia #{nid} ({status}) nie zapisany: "
              f"{type(e).__name__}: {e}", flush=True)


async def przekaz(nid: int, event_code: str, severity: str, title: str, body: str,
                  engine: str | None, dane: dict | None, *, fetch_one, execute,
                  klient: httpx.AsyncClient | None = None) -> str:
    """Alarm z dzwonka do bramki. Zwraca status (`sent`, `failed`,
    `suppressed_cooldown`, `brak-klucza`). NIGDY NIE RZUCA."""
    global _brak_klucza_zgloszony
    try:
        if not skonfigurowana():
            if not _brak_klucza_zgloszony:
                print("[bramka] brak BRAMKA_KLUCZ — alarmy krytyczne zostają "
                      "w panelu", flush=True)
                _brak_klucza_zgloszony = True
            return "brak-klucza"

        stan = await fetch_one(SQL_POWTORKI, (event_code, title, engine)) or {}
        od = stan.get("od_ostatniej_min")
        od_bledu = stan.get("od_nieudanej_min")
        powod = ocen_powtorke(int(stan.get("ten_sam") or 0),
                              None if od is None else float(od),
                              int(stan.get("w_godzinie") or 0),
                              int(stan.get("nieudanych") or 0),
                              None if od_bledu is None else float(od_bledu))
        if powod:
            print(f"[bramka] {event_code} #{nid} stłumiony: {powod}", flush=True)
            await _slad(execute, nid, "suppressed_cooldown", 0, powod)
            return "suppressed_cooldown"

        rodzaj = engine or (dane or {}).get("komponent") or "system"
        try:
            odp = await wyslij(title, tresc_wiadomosci(body, event_code, nid),
                               priorytet=PRIORYTET.get(severity, "alarm"),
                               rodzaj=str(rodzaj), klient=klient)
        except Exception as e:                                # noqa: BLE001
            blad = f"{type(e).__name__}: {e}"
            print(f"[bramka] {event_code} #{nid} NIE wysłany: {blad}", flush=True)
            await _slad(execute, nid, "failed", 1, blad)
            return "failed"

        odbiorcy = odp.get("odbiorcy") or {}
        opis = f"kanal={odp.get('kanal')}"
        if odbiorcy:
            opis += f" ({odbiorcy.get('doszlo')}/{odbiorcy.get('wszystkich')})"
        print(f"[bramka] {event_code} #{nid} wysłany: {opis}", flush=True)
        # Sukces czesciowy (doszlo do czesci odbiorcow) zostaje sukcesem - tak
        # samo liczy go bramka - ale niedoreczenia ida do `error`, zeby byly
        # widoczne w sladzie, a nie tylko w logu bramki.
        bledy = odp.get("bledy") or None
        await _slad(execute, nid, "sent", 1,
                    f"{opis}; {json.dumps(bledy, ensure_ascii=False)}" if bledy else None)
        return "sent"
    except Exception as e:                                    # noqa: BLE001
        print(f"[bramka] {event_code} #{nid}: przekazanie przerwane: "
              f"{type(e).__name__}: {e}", flush=True)
        return "failed"


async def _test() -> int:
    """Wiadomosc probna prosto do bramki - bez wiersza w dzwonku i bez tlumienia."""
    odp = await wyslij(
        "TEST — alarm tradera przez bramkę (nie reaguj)",
        "Wiadomość próbna: sprawdzenie, że alarmy krytyczne Trader-AI dochodzą "
        "przez bramkę RenaCode. Nic się nie stało.\n\n(test ręczny, bez wpisu w panelu)",
        priorytet="informacja", rodzaj="test")
    print(json.dumps(odp, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--test", action="store_true",
                    help="wyślij wiadomość próbną (priorytet informacja)")
    args = ap.parse_args()
    if not args.test:
        ap.print_help()
        return 2
    try:
        return asyncio.run(_test())
    except Exception as e:                                    # noqa: BLE001
        print(f"test bramki nieudany: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
