"""CronJoby: jawna strefa, kontekst bez uprawnien, obraz bazy po digescie (2026-10-03).

Trzy uwagi z audytu infrastruktury (G4, G5, G6), kazda o tym samym ksztalcie -
chart zalezal od czegos, czego nie wpisal:

  * strefa harmonogramu: bez `timeZone` liczy sie w strefie HOSTA k3s,
  * kontekst bezpieczenstwa: c2561e4 objal nim tylko Deploymenty, a Joby
    chodzace na tym samym obrazie (trener, proba, post-mortem z kluczem
    `SECRETS_KEY`) dostawaly domyslne uprawnienia i token API klastra,
  * obraz bazy: ruchomy tag `pg17` przy `IfNotPresent`.

Test czyta szablony jako tekst - bez `helm` - tak jak test_chart_kontekst_bezpieczenstwa.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
SZABLONY = KORZEN / "charts/trader/templates"
VALUES = yaml.safe_load((KORZEN / "charts/trader/values.yaml").read_text(encoding="utf-8"))
CRONY = sorted(p.name for p in SZABLONY.glob("*.yaml")
               if "kind: CronJob" in p.read_text(encoding="utf-8"))
Z_OBRAZEM_API = [p for p in CRONY
                 if ".Values.api.image.pullPolicy" in (SZABLONY / p).read_text(encoding="utf-8")]


def _tekst(plik: str) -> str:
    return (SZABLONY / plik).read_text(encoding="utf-8")


def test_jest_czego_pilnowac():
    assert len(CRONY) >= 9 and "backup.yaml" in CRONY and "trainer-cron.yaml" in CRONY
    assert len(Z_OBRAZEM_API) == len(CRONY) - 1, "backup jako jedyny ma inny obraz"


@pytest.mark.parametrize("plik", CRONY)
def test_kazdy_cronjob_ma_jawna_strefe(plik):
    t = _tekst(plik)
    assert re.search(r"^  timeZone: .*\.Values\.strefaCzasowa", t, re.M), (
        f"{plik}: harmonogram w strefie hosta - zmiana VPS przesunie okno nocne")
    # Jedno pole: post-mortem mial wlasne `Etc/UTC`, a drugi klucz w YAML-u
    # albo nadpisuje pierwszy, albo wywraca parser - zalezy od narzedzia.
    assert len(re.findall(r"^  timeZone:", t, re.M)) == 1, f"{plik}: dwie strefy"


def test_strefa_jest_jedna_i_jest_warszawa():
    """Jedna strefa dla wszystkich: zmiana czasu przesuwa zadania RAZEM,
    wiec odstepy kopia -> trener -> proba -> post-mortem zostaja stale."""
    assert VALUES["strefaCzasowa"] == "Europe/Warsaw"


@pytest.mark.parametrize("plik", Z_OBRAZEM_API)
def test_cronjob_z_obrazem_api_bez_uprawnien(plik):
    t = _tekst(plik)
    assert t.count('include "trader.podBezUprawnien"') == 1, plik
    assert t.count('include "trader.kontenerBezUprawnien"') == t.count(
        ".Values.api.image.pullPolicy"), f"{plik}: kontener bez kontekstu"
    assert "automountServiceAccountToken: false" in t, (
        f"{plik}: pod dostaje token API klastra, z ktorym nie rozmawia")


def test_kopia_bez_tokenu_i_z_ograniczonym_rootem():
    t = _tekst("backup.yaml")
    assert "automountServiceAccountToken: false" in t
    assert 'add: ["CHOWN", "FOWNER"]' in t, "kontener startowy z pelnym rootem"
    zrzut = t[t.index("- name: dump"):]
    assert "runAsNonRoot: true" in zrzut and "runAsUser: 1000" in zrzut


def test_obraz_bazy_przypiety_po_digescie():
    obraz = VALUES["db"]["image"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", obraz.get("digest") or ""), (
        "obraz bazy bez digestu - ruchomy tag przy IfNotPresent")
    for plik in ("db.yaml", "backup.yaml"):
        t = _tekst(plik)
        assert 'include "trader.obrazBazy"' in t
        assert ".Values.db.image.repository }}:{{ .Values.db.image.tag }}\"" not in t, (
            f"{plik}: obraz bazy z samego tagu, obok helpera")


# ------------------------------------------- trener za bramką migracji (A)
def test_trener_ma_bramke_migracji_ta_sama_rola_co_kontener():
    """Poprawka A (2026-10-05): trener chodzi raz w tygodniu, więc odmowa przy
    starszym schemacie albo `connection refused` w pierwszej sekundzie poda
    kosztuje tydzień bez nowej wersji (04.10 tor 4h). Bramka czeka do 600 s.

    Ta sama wąska rola, co kontener roboczy: bramka z inną rolą mierzyłaby
    uprawnienia kogoś innego niż ten, kto za chwilę wystartuje.
    """
    t = _tekst("trainer-cron.yaml")
    m = re.search(r"^(\s*)initContainers:\n(.*?)^\1containers:", t, re.M | re.S)
    assert m, "trener bez kontenera startowego - rusza na starym schemacie"
    assert 'include "trader.bramkaMigracji" (dict "ctx" $ "rola" "silnik")' in m.group(2)
    assert t.count('include "trader.rolaDbEnv" (dict "ctx" $ "rola" "silnik")') == 1, (
        "kontener trenera nie chodzi rolą silnika - bramka pyta inną rolą")


helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm niedostępny")


@helm
def test_RENDER_trener_bramka_z_haslem_WYLACZNIE_wlasnej_roli():
    wynik = subprocess.run(["helm", "template", str(KORZEN / "charts/trader"),
                            "-s", "templates/trainer-cron.yaml"],
                           capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    spec = yaml.safe_load(wynik.stdout)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    bramka, = spec["initContainers"]
    trener = spec["containers"][0]
    assert bramka["name"] == "czekaj-na-migracje"
    assert bramka["image"] == trener["image"]
    env = {e["name"]: e for e in bramka["env"]}
    assert set(env) == {"PGPASSWORD", "DATABASE_URL", "PYTHONUNBUFFERED"}
    roboczy = {e["name"]: e for e in trener["env"]}
    assert env["DATABASE_URL"] == roboczy["DATABASE_URL"]
    assert env["PGPASSWORD"] == roboczy["PGPASSWORD"]
    assert "Migracje nie dogonily" in bramka["command"][-1]


@helm
def test_trener_tygodniowy_ma_JEDNA_powtorke_w_limicie_czasu():
    """Audyt 2026-10-09, W-4: planowy trener 04.10 padł na `connection refused`
    przy `backoffLimit: 0` - jedna przejściowa awaria sieci = tydzień bez nowej
    wersji. Powtórka musi się mieścić w twardym limicie czasu Joba (liczonym
    dla wszystkich prób razem), a powtórek nie może być bez końca."""
    wynik = subprocess.run(["helm", "template", str(KORZEN / "charts/trader"),
                            "-s", "templates/trainer-cron.yaml"],
                           capture_output=True, text=True)
    assert wynik.returncode == 0, wynik.stderr
    job = yaml.safe_load(wynik.stdout)["spec"]["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 1
    assert job["activeDeadlineSeconds"] <= 3 * 3600


# SPRZATANIE ZAKONCZONYCH JOBOW (2026-10-15). Bez TTL nieudany Job wisial
# czerwony tygodniami mimo udanych pozniejszych przebiegow; za krotki TTL
# kasowalby porazke, zanim nastepny przebieg mial szanse jej zaprzeczyc.
_ODSTEP_S = {"*/10 * * * *": 600, "7 */6 * * *": 6 * 3600, "13 */6 * * *": 6 * 3600,
             "40 4 * * 0": 7 * 86400}


def _odstep_s(harmonogram: str) -> int:
    if harmonogram in _ODSTEP_S:
        return _ODSTEP_S[harmonogram]
    if harmonogram == "0 0 31 2 *":  # nigdy - zadania uruchamiane recznie
        return 0
    minuta, godzina, dzien, miesiac, tydzien = harmonogram.split()
    assert (dzien, miesiac, tydzien) == ("*", "*", "*") and minuta.isdigit() and godzina.isdigit(), (
        f"nieznany harmonogram {harmonogram!r} - dopisz odstep do _ODSTEP_S")
    return 86400


@pytest.mark.parametrize("plik", CRONY)
def test_kazdy_cronjob_sprzata_po_nastepnym_przebiegu(plik):
    t = _tekst(plik)
    m = re.search(r"^      ttlSecondsAfterFinished: \{\{ \.Values\.sprzatanieZadan\.(\w+) \}\}", t, re.M)
    assert m, f"{plik}: brak ttlSecondsAfterFinished w jobTemplate.spec"
    ttl = VALUES["sprzatanieZadan"][m.group(1)]
    harmonogram = re.search(r'^  schedule: "?([^"\n]+)"?', t, re.M).group(1).strip()
    if "Values." in harmonogram:
        klucz = re.search(r"\.Values\.(\w+)\.schedule", harmonogram).group(1)
        harmonogram = VALUES[klucz]["schedule"]
    odstep = _odstep_s(harmonogram)
    assert ttl > odstep, f"{plik}: TTL {ttl} s <= odstep {odstep} s - porazka zniknie przed nastepnym przebiegiem"
    assert ttl <= max(odstep * 2, 7 * 86400), f"{plik}: TTL {ttl} s - stara porazka wisi za dlugo"
