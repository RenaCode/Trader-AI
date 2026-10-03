"""Bramka migracji przy workerach: czy worker NAPRAWDĘ nie wstanie na starym
schemacie — i czy nie zapłacił za to hasłami cudzych ról.

DLACZEGO OSOBNY PLIK

InitContainer `migrate` stoi wyłącznie przy API. Dziewięć Deploymentów workerów
chodzi na TYM SAMYM obrazie co API i ma `strategy: Recreate`, więc przy podbiciu
tagu restartują się natychmiast — równolegle z migracją, nie po niej. Przez cały
czas jej trwania nowy kod workerów pracuje na starym schemacie. Gdy migracja
padnie, API nie wstaje i to widać; pętla decyzyjna i dozorca ryzyka chodzą dalej
i nie mówi o tym nic.

Rozwiązaniem jest kontener startowy `czekaj-na-migracje` (workers.yaml) plus
nadanie odczytu w migracji 0050. Ten plik pilnuje OBU połówek naraz, bo osobno
każda z nich wygląda na poprawną:

  * bramka bez nadania pada w kontenerze startowym na „permission denied",
    w pętli ponawiania, gdzie wygląda dokładnie jak „baza jeszcze nie wstała";
  * nadanie bez bramki jest martwym GRANT-em.

I trzecia rzecz, najważniejsza — CZEGO BRAMKA NIE MOŻE MIEĆ.

Skopiowanie initContainera `migrate` byłoby prostsze i byłoby błędem: migrator
dostaje `POSTGRES_PASSWORD` właściciela oraz hasła wszystkich pięciu ról
procesów (`trader.rolaHaslaEnv`). Kopia wstawiłaby komplet haseł do każdego poda
workera — w tym do `engine`, który trzyma klucze giełdy i handluje. Silnik
z hasłem roli `trader_dozorca` mógłby zwolnić sobie kill-switcha; nie dlatego,
że ma taki kod, tylko dlatego, że MOŻE. Testy niżej pilnują, że bramka bierze tę
samą WĄSKĄ rolę co worker i ani jednego poświadczenia więcej.

DWIE WARSTWY SPRAWDZENIA

1. STATYCZNA (zawsze). Szablon i plik migracji czytane wprost. Działa w CI,
   które helma nie ma (.github/workflows/tests.yml).

2. RENDER (gdy jest `helm`). Szablon jest pętlą `range` — dopóki nikt nie wstawi
   do niej `if`, warstwa statyczna mówi to samo co render. Render sprawdza to,
   zamiast zakładać, i jako jedyny widzi RZECZYWISTĄ listę zmiennych
   środowiskowych każdego z dziewięciu podów.

TRADER_KORZEN

Testy czytają drzewo wskazane przez `TRADER_KORZEN`, domyślnie to repozytorium.
Zmienna istnieje po to, żeby dało się wycelować cały ten plik w KOPIĘ stanu
sprzed poprawki (`git show HEAD:... > kopia`) i zobaczyć go na czerwono, bez
cofania czegokolwiek w drzewie roboczym — w którym pracują równolegle inne
osoby.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(os.environ.get("TRADER_KORZEN") or Path(__file__).resolve().parents[1])
SZABLONY = KORZEN / "charts" / "trader" / "templates"
WORKERS = SZABLONY / "workers.yaml"
MIGRACJE = KORZEN / "db" / "migrations"

BRAMKA = "czekaj-na-migracje"

# Poświadczenia, których w podzie workera nie może być. Nazwa roli właściciela
# i helper rozdający hasła WSZYSTKICH ról — każde z osobna wystarczy, żeby
# rozdział uprawnień z 0034 przestał cokolwiek znaczyć.
ZAKAZANE = (
    "POSTGRES_PASSWORD",      # rola właściciela: DDL, ALTER ROLE, wszystko
    "trader.rolaHaslaEnv",    # hasła wszystkich pięciu ról procesów naraz
    "trader.appDbPasswordEnv",
    "APP_DB_PASSWORD",        # rola API
    "trader.databaseUrl",     # połączenie właściciela
    "trader.appDatabaseUrl",  # połączenie API
)


# --------------------------------------------------------------- warstwa statyczna

def _tekst_workers() -> str:
    return WORKERS.read_text(encoding="utf-8")


def _blok_bramki(tekst: str) -> str:
    """Fragment szablonu opisujący kontener startowy — od `initContainers:`
    do `containers:` na tym samym wcięciu.

    Nie „czy gdzieś w pliku stoi słowo": nadanie hasła dopisane w kontenerze
    roboczym nie ma tu nic do rzeczy, a dopisane w bramce ma wszystko.
    """
    m = re.search(r"^(\s*)initContainers:\n(.*?)^\1containers:",
                  tekst, re.M | re.S)
    assert m, ("workers.yaml nie ma sekcji initContainers — worker startuje "
               "natychmiast, równolegle z migracją, na starym schemacie")
    return m.group(2)


def test_szablon_ma_bramke_i_jest_ona_w_petli_workerow():
    """Bramka MUSI stać wewnątrz `range` po workerach i bez żadnego `if`.

    Jeden pominięty worker to jeden proces na starym schemacie — a przy
    `dozorcy` albo `engine` to dokładnie ten, na którym zależy najbardziej.
    """
    tekst = _tekst_workers()
    blok = _blok_bramki(tekst)
    assert f"- name: {BRAMKA}" in blok, "sekcja initContainers nie zawiera bramki"

    poczatek = tekst.index("{{- range $name, $w := .Values.workers }}")
    assert poczatek < tekst.index("initContainers:"), \
        "bramka stoi poza pętlą po workerach"

    # Żadnego warunku między początkiem bloku a nazwą kontenera: `if` w tym
    # miejscu znaczy worker, który bramki nie dostał, i nikt tego nie zobaczy.
    przed = blok.split(f"- name: {BRAMKA}")[0]
    assert "{{- if" not in przed and "{{ if" not in przed, \
        "bramka jest warunkowa — część workerów wstanie bez niej"


def test_bramka_NIE_DOSTAJE_hasel_innych_rol():
    """Własność, dla której ta bramka nie jest kopią initContainera `migrate`.

    `engine` trzyma klucze giełdy i handluje. Komplet haseł w jego podzie
    znaczy, że może połączyć się rolą `trader_dozorca` i zwolnić sobie
    kill-switcha — nie dlatego, że ma taki kod, tylko dlatego, że MOŻE.
    """
    blok = _blok_bramki(_tekst_workers())
    assert f"- name: {BRAMKA}" in blok, "nie ma bramki, więc nie ma czego sprawdzać"
    for zakazane in ZAKAZANE:
        assert zakazane not in blok, (
            f"bramka dostaje {zakazane} — pod workera widzi poświadczenia "
            f"spoza własnej roli, a rozdział uprawnień z 0034 jest wart tyle, "
            f"ile rozdział POŚWIADCZEŃ")


def test_bramka_bierze_te_sama_waska_role_co_worker():
    """Kanarek do poprzedniego: kontener BEZ ŻADNEGO hasła też przeszedłby tamten
    test — i nie połączyłby się z bazą w ogóle, czyli bramka byłaby pętlą
    ponawiania, która nigdy nie przepuszcza."""
    blok = _blok_bramki(_tekst_workers())
    assert 'trader.rolaDbEnv' in blok, "bramka nie bierze roli procesu"
    assert '($w.dbRole | default "silnik")' in blok, (
        "bramka nie idzie rolą TEGO workera — dozorca pytałby bazę rolą silnika")


def test_bramka_czyta_wymagany_numer_Z_OBRAZU_a_nie_z_nastawy():
    """„Baza jest co najmniej tak nowa jak mój kod" ma się mierzyć samo.

    Oczekiwany numer wpisany w chart byłby DRUGĄ kopią tej samej liczby, o dwa
    pliki od migracji, którą opisuje. Rozjazd dwóch takich list kosztował już
    21 godzin martwych cech 2026-09-04 — i nie objawił się awarią, tylko ciszą.
    """
    blok = _blok_bramki(_tekst_workers())
    assert 'pathlib.Path("db/migrations").glob' in blok, \
        "bramka nie czyta katalogu migracji z własnego obrazu"
    assert "SELECT version FROM public.schema_migrations" in blok, \
        "bramka nie pyta bazy o zastosowane migracje"


def test_bramka_ma_limit_czasu_i_KONCZY_SIE_BLEDEM():
    """Bez limitu `Recreate` znaczy ciszę: stary pod już nie działa, a nowy stoi
    w kontenerze startowym i nikt tego nie odróżni od wolnego startu.

    Limit musi być POD progiem czujki pulsu (dozor.py, MIN_PROG_S = 900 s):
    czekanie mieszczące się w limicie nie wywoła fałszywego alarmu, a jego
    wyczerpanie przeciwnie — pod pada, puls milknie i czujka zgłasza to sama.
    Limit powyżej progu znaczyłby alarm przy KAŻDYM normalnym wdrożeniu, czyli
    czujkę, która miga.
    """
    blok = _blok_bramki(_tekst_workers())
    m = re.search(r"^\s*LIMIT_S = (\d+)\s*$", blok, re.M)
    assert m, "bramka nie ma limitu czasu — czeka w nieskończoność"
    limit = int(m.group(1))

    from services.engine.dozor import MIN_PROG_S
    assert 0 < limit < MIN_PROG_S, (
        f"limit bramki to {limit} s przy progu czujki {MIN_PROG_S} s — "
        f"albo każde wdrożenie kończy się alarmem, albo limit jest tak krótki, "
        f"że pada na wolniejszym starcie")

    assert re.search(r"sys\.exit\(\s*[\"']Migracje nie dogonily", blok), (
        "bramka po limicie nie kończy się błędem — pod wstaje po cichu na "
        "starym schemacie, czyli dokładnie tak, jak przed tą zmianą")


# ------------------------------------------------------------------- migracja

def _plik_bramki() -> Path:
    """Migracja, której numer bramka wpisuje sobie jako `NADANIE`.

    Numer jest w szablonie JEDYNĄ liczbą odnoszącą się do konkretnej migracji.
    Nie jest progiem ani nastawą — jest nazwą jednego, niezmiennego pliku —
    ale nadal jest liczbą w dwóch miejscach, więc ten test wiąże je ze sobą.
    """
    blok = _blok_bramki(_tekst_workers())
    m = re.search(r"^\s*NADANIE = (\d+)\s*$", blok, re.M)
    assert m, "bramka nie deklaruje numeru migracji, która nadaje jej odczyt"
    numer = int(m.group(1))
    pliki = sorted(MIGRACJE.glob("%04d_*.sql" % numer))
    assert len(pliki) == 1, (
        f"NADANIE = {numer} w workers.yaml nie wskazuje dokładnie jednej "
        f"migracji (znaleziono {[p.name for p in pliki]})")
    return pliki[0]


def _zdania(sql: str) -> list[str]:
    bez_komentarzy = "\n".join(l.split("--")[0] for l in sql.splitlines())
    return [" ".join(z.split()) for z in bez_komentarzy.split(";") if z.split()]


def test_migracja_nadaje_odczyt_dokladnie_tym_rolom_ktorych_uzywa_chart():
    """Nadanie i bramka są w dwóch plikach. Rola, która ma bramkę i nie ma
    nadania, pada na „permission denied for table schema_migrations" —
    w kontenerze startowym, w pętli ponawiania, gdzie wygląda dokładnie jak
    „baza jeszcze nie wstała".

    NADANIE LICZONE Z MIGRACJI BRAMKI I WSZYSTKICH PÓŹNIEJSZYCH.

    Wcześniej ten test czytał wyłącznie plik, na który wskazuje `NADANIE`, więc
    każda nowa rola procesu musiałaby wracać do tamtego pliku po własny GRANT.
    To jest pułapka, a nie zabezpieczenie: rola założona w swojej własnej
    migracji (tak powstał `trader_news` w 0052) nadaje sobie ten odczyt na
    miejscu, razem z resztą swoich praw. Mierzoną WŁASNOŚCIĄ jest „żadna rola
    z bramką nie zostaje bez odczytu", a nie „wszystkie nadania stoją w jednym
    pliku" — pilnowanie tego drugiego kazałoby przepisywać historię migracji.
    Sam wskaźnik `NADANIE` nadal musi wskazywać dokładnie jeden istniejący plik
    (patrz `_plik_bramki`) i nadal wolno mu nadawać tylko USAGE i SELECT.
    """
    bramka = _plik_bramki()
    pliki = [p for p in sorted(MIGRACJE.glob("*.sql")) if p.name >= bramka.name]
    values = yaml.safe_load(
        (KORZEN / "charts" / "trader" / "values.yaml").read_text(encoding="utf-8"))

    uzywane = {values["db"]["role"][w.get("dbRole", "silnik")]["user"]
               for w in values["workers"].values() if w.get("enabled")}
    assert uzywane, "żaden worker nie jest włączony — test nic by nie mierzył"

    nadane = set()
    for plik in pliki:
        for z in _zdania(plik.read_text(encoding="utf-8")):
            m = re.match(r"GRANT SELECT ON public\.schema_migrations TO (.+)$",
                         z, re.I)
            if m:
                nadane |= {r.strip() for r in m.group(1).split(",")}
    brakuje = uzywane - nadane
    assert not brakuje, (
        f"workery chodzą rolami {sorted(brakuje)}, którym migracja nie daje "
        f"SELECT na public.schema_migrations — ich bramka padnie w starcie")


def test_migracja_nadaje_WYLACZNIE_USAGE_i_SELECT():
    """Zapis do tej tabeli należy do migratora, który chodzi rolą właściciela.

    Proces tła z prawem zapisu potrafiłby zameldować migrację, której nie
    zastosowano — i otworzyć sobie własną bramkę. Wtedy bramka mierzyłaby swój
    własny wpis zamiast stanu schematu.
    """
    tresc = _plik_bramki().read_text(encoding="utf-8")

    wolno = {"USAGE", "SELECT"}
    dozwolone_cele = {"SCHEMA public", "public.schema_migrations"}
    grantow = 0
    for z in _zdania(tresc):
        if z.upper() in ("BEGIN", "COMMIT"):
            continue
        assert not re.match(r"ALTER DEFAULT PRIVILEGES", z, re.I), (
            "domyślne uprawnienia w schemacie public — każda przyszła tabela "
            "w `public` byłaby czytelna dla procesów tła od chwili powstania")
        m = re.match(r"GRANT (.+?) ON (.+?) TO (.+)$", z, re.I)
        assert m, f"nieoczekiwane zdanie w migracji bramki: {z!r}"
        grantow += 1
        prawa = {p.strip().upper() for p in m.group(1).split(",")}
        assert prawa <= wolno, f"migracja nadaje {sorted(prawa - wolno)}"
        assert m.group(2).strip() in dozwolone_cele, (
            f"migracja dotyka {m.group(2)!r} — bramce wystarczy jedna tabela")
    assert grantow >= 2, "migracja nie nadaje ani USAGE, ani SELECT"


# ----------------------------------------------------------------- warstwa renderu

bez_helma = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm niedostępny — warstwa statyczna wyżej sprawdza to samo na "
           "szablonie (CI nie ma helma, patrz .github/workflows/tests.yml)")


@pytest.fixture(scope="module")
def workery_z_renderu() -> list[dict]:
    """Dziewięć Deploymentów workerów tak, jak zobaczy je klaster."""
    wynik = subprocess.run(
        ["helm", "template", str(KORZEN / "charts" / "trader")],
        capture_output=True, text=True)
    assert wynik.returncode == 0, f"helm template padł:\n{wynik.stderr}"
    return [d for d in yaml.safe_load_all(wynik.stdout)
            if d and d.get("kind") == "Deployment"
            and d["metadata"].get("labels", {}).get(
                "trader.renacode.com/component") == "worker"]


@bez_helma
def test_RENDER_kazdy_worker_ma_bramke(workery_z_renderu):
    assert workery_z_renderu, "render nie dał ani jednego workera"
    bez_bramki = []
    for d in workery_z_renderu:
        nazwy = [c["name"] for c in
                 (d["spec"]["template"]["spec"].get("initContainers") or [])]
        if BRAMKA not in nazwy:
            bez_bramki.append(d["metadata"]["name"])
    assert not bez_bramki, (
        f"workery bez bramki: {bez_bramki} — wstaną na starym schemacie "
        f"w chwili, w której migracja jeszcze trwa")


@bez_helma
def test_RENDER_bramka_ma_TYLKO_haslo_wlasnej_roli(workery_z_renderu):
    """To samo co warstwa statyczna, ale na rzeczywistej liście zmiennych.

    Helper dopisany dwa pliki dalej (`_helpers.tpl`) nie zmienia tekstu
    `workers.yaml`, a zmienia to, co ląduje w podzie. Warstwa statyczna by tego
    nie zobaczyła.
    """
    # `PGPASSWORD`, nie hasło w URL-u - patrz tests/test_haslo_poza_url.py.
    wolno = {"PGPASSWORD", "DATABASE_URL", "PYTHONUNBUFFERED"}
    for d in workery_z_renderu:
        bramka = next(c for c in d["spec"]["template"]["spec"]["initContainers"]
                      if c["name"] == BRAMKA)
        nazwy = {e["name"] for e in bramka["env"]}
        assert nazwy <= wolno, (
            f"{d['metadata']['name']}: bramka dostaje {sorted(nazwy - wolno)}")
        # Dokładnie JEDEN klucz z sekretu: hasło własnej roli. Drugi klucz
        # w tej liście to drugie poświadczenie w podzie workera, niezależnie
        # od tego, jak się nazywa zmienna.
        sekrety = [e["valueFrom"]["secretKeyRef"]["key"] for e in bramka["env"]
                   if "valueFrom" in e]
        assert len(sekrety) == 1, (
            f"{d['metadata']['name']}: bramka czyta {len(sekrety)} kluczy "
            f"z sekretu — {sekrety}")


@bez_helma
def test_RENDER_bramka_laczy_sie_rola_SWOJEGO_workera(workery_z_renderu):
    """Dozorca pytający bazę rolą silnika przeszedłby każdy test wyżej,
    a jego pod trzymałby hasło roli, która nie jest jego."""
    values = yaml.safe_load(
        (KORZEN / "charts" / "trader" / "values.yaml").read_text(encoding="utf-8"))
    oczekiwany = {
        f"trader-{n}": values["db"]["role"][w.get("dbRole", "silnik")]
        for n, w in values["workers"].items() if w.get("enabled")}

    for d in workery_z_renderu:
        nazwa = d["metadata"]["name"]
        rola = oczekiwany[nazwa]
        bramka = next(c for c in d["spec"]["template"]["spec"]["initContainers"]
                      if c["name"] == BRAMKA)
        env = {e["name"]: e for e in bramka["env"]}
        assert env["DATABASE_URL"]["value"].startswith(
            f"postgresql://{rola['user']}@"), (
            f"{nazwa}: bramka łączy się nie swoją rolą — "
            f"{env['DATABASE_URL']['value'].split('@')[0]}")
        assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["key"] \
            == rola["secretKey"]

        # I ta sama rola, co kontener roboczy — inaczej bramka mierzyłaby
        # uprawnienia kogoś innego niż ten, kto za chwilę wystartuje.
        roboczy = {e["name"]: e for e in
                   d["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert roboczy["DATABASE_URL"]["value"] == env["DATABASE_URL"]["value"]


@bez_helma
def test_RENDER_bramka_idzie_tym_samym_obrazem_co_worker(workery_z_renderu):
    """Bramka porównuje katalog migracji ZE SWOJEGO obrazu z bazą. Inny obraz
    niż worker znaczyłby, że przepuszcza kod, którego nie sprawdziła."""
    for d in workery_z_renderu:
        spec = d["spec"]["template"]["spec"]
        bramka = next(c for c in spec["initContainers"] if c["name"] == BRAMKA)
        assert bramka["image"] == spec["containers"][0]["image"], \
            f"{d['metadata']['name']}: bramka i worker na różnych obrazach"
