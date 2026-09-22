"""Nadzór nad nadzorem: chart nie może cicho wyłączyć własnych czujek.

Wszystkie trzy wady, które te testy pilnują, mają ten sam kształt — system
wygląda na zdrowy dokładnie wtedy, gdy przestał czuwać:

  1. TRENER BEZ `SECRETS_KEY`. `dozor.py --co stan-nauki` woła `notify()`,
     a `notify()` tworzy `SecretBox()` POZA blokiem `try` i PRZED pętlą po
     kanałach (services/api/notifications.py). Bez klucza `SecretBox` rzuca
     `RuntimeError` (services/api/security.py). Druga połowa wady czyniła
     pierwszą niewidoczną: kod wyjścia dozoru nie był zbierany, więc `exit $RC`
     niósł wyłącznie kod TRENERA. Model tracił przewagę, dozór wywracał się na
     pierwszej linii, Job świecił na zielono i ArgoCD meldował zdrowie.
     `--co stan-nauki` i `--co dryf` nie są wołane nigdzie indziej, więc był to
     CAŁY nadzór nad jakością modelu.

  2. `Forbid` BEZ `activeDeadlineSeconds`. Zawieszony przebieg trzyma Joba bez
     końca, a każdy kolejny termin jest po cichu POMIJANY — przy dobowym
     trenerze model zamarza, a czujka stanu nauki milczy, bo stan, który stoi,
     się nie zmienia. Przy czujce żywotności (co 10 minut) jeden zawieszony
     przebieg zatrzymuje naraz dziewięć sprawdzeń, czyli całą warstwę nadzoru.

  3. `livenessProbe` PYTAJĄCA O BAZĘ. `/api/health` robi `SELECT` na bazie,
     a sonda żywotności ZABIJA kontener. Restart nie naprawia wolnej bazy, za to
     kontener startowy `migrate` wchodzi w backoff. Zależność zewnętrzna należy
     do gotowości (wypisz pod z Service), nie do żywotności.

PODMIANA ŹRÓDŁA NA POTRZEBY DOWODU. `CHART_TRADER` i `API_MAIN` pozwalają
puścić te testy przeciw KOPII plików sprzed poprawki (`git show HEAD:...`
w katalogu tymczasowym) i zobaczyć, że padają — bez cofania czegokolwiek
w drzewie roboczym.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

KORZEN = Path(__file__).resolve().parents[1]
CHART = Path(os.environ.get("CHART_TRADER", KORZEN / "charts/trader"))
SZABLONY = CHART / "templates"
API_MAIN = Path(os.environ.get("API_MAIN", KORZEN / "services/api/main.py"))

# Stan przejsciowy 22.09.2026 - patrz test_zywotnosc_API_nie_pyta_o_baze.
# `/api/zywy` jest celem; `/api/health` wolno wskazywac WYLACZNIE dopoki
# produkcja chodzi na obrazie sprzed tej poprawki. Podbicie tagu zapala
# tamten test, wiec ta para stalych jest calym mechanizmem przypomnienia.
ZYWOTNOSC_DOCELOWA = "/api/zywy"
TAG_SPRZED_POPRAWKI = "sha-0ea6f54"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))

_KOMENTARZ_HELMA = re.compile(r"\{\{-?/\*.*?\*/-?\}\}", re.S)
_AKCJA_HELMA = re.compile(r"\{\{-?.*?-?\}\}", re.S)


def manifesty(nazwa_pliku: str) -> list[dict]:
    """Szablon Helma jako lista manifestów — bez uruchamiania `helm`.

    Testy chartu, które wymagają binarki `helm`, pomijają się same tam, gdzie
    jej nie ma, czyli zwykle w CI — a test, który się pomija, niczego nie
    pilnuje. Wiersze będące SAMĄ akcją Helma znikają, akcje w treści wiersza
    stają się napisem `SZABLON`. Dla pól, o które tu pytamy (ścieżki sond,
    limity czasu, zasoby, nazwy zmiennych), podstawienie jest bez znaczenia.
    """
    tekst = _KOMENTARZ_HELMA.sub("", (SZABLONY / nazwa_pliku).read_text(encoding="utf-8"))
    wiersze = []
    for wiersz in tekst.splitlines():
        goly = wiersz.strip()
        if goly.startswith("{{") and goly.endswith("}}") and _AKCJA_HELMA.fullmatch(goly):
            continue
        wiersze.append(_AKCJA_HELMA.sub("SZABLON", wiersz))
    return [d for d in yaml.safe_load_all("\n".join(wiersze)) if d]


def jedyny_cronjob(nazwa_pliku: str) -> dict:
    kron = [d for d in manifesty(nazwa_pliku) if d.get("kind") == "CronJob"]
    assert len(kron) == 1, f"{nazwa_pliku}: spodziewany dokładnie jeden CronJob"
    return kron[0]


def skrypt(cronjob: dict) -> list[str]:
    """Wiersze polecenia kontenera, bez komentarzy i pustych."""
    polecenie = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"]
    assert polecenie[:2] == ["sh", "-c"], "spodziewany skrypt powłoki"
    return [w.strip() for w in polecenie[2].splitlines()
            if w.strip() and not w.strip().startswith("#")]


# ------------------------------------------------------------------ zadanie 1

def test_trener_ma_SECRETS_KEY_bo_wola_dozor_a_dozor_wola_notify():
    """Bez tej zmiennej powiadomienie o stanie nauki nie ma prawa wyjść."""
    trener = jedyny_cronjob("trainer-cron.yaml")
    env = trener["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]
    klucz = [e for e in env if e["name"] == "SECRETS_KEY"]
    assert klucz, (
        "trainer-cron.yaml nie wystawia SECRETS_KEY, a skrypt woła "
        "`dozor.py --co stan-nauki`. `notify()` tworzy `SecretBox()` przed pętlą "
        "po kanałach i POZA `try`, więc bez klucza dozór wywraca się ZANIM "
        "wyśle cokolwiek — przy każdej zmianie stanu nauki i przy każdym dryfie")
    assert klucz[0].get("valueFrom", {}).get("secretKeyRef", {}).get("key") == "SECRETS_KEY", (
        "SECRETS_KEY ma iść z sekretu, tak samo jak w proba-cron.yaml — "
        "wartość wpisana wprost w chart trafia do repozytorium")


def test_trener_bierze_klucz_dokladnie_tak_samo_jak_proba_pozarowa():
    """Dwa zadania w jednym chartcie, jeden sekret — i jeden sposób sięgania po
    niego. Rozjazd formy nie objawia się niczym aż do dnia, w którym jedno
    z nich przestaje działać po zmianie nazwy sekretu."""
    def wpis(plik):
        env = jedyny_cronjob(plik)["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]
        return next(e for e in env if e["name"] == "SECRETS_KEY")
    assert wpis("trainer-cron.yaml") == wpis("proba-cron.yaml")


def test_kod_wyjscia_dozoru_ZBIERANY_do_wyniku_zadania():
    """Najgorszy tryb awarii nadzoru: czujka padła, a zadanie zielone.

    Do 2026-09-22 `exit $RC` niosło wyłącznie kod trenera, więc dozór mógł się
    wywracać codziennie i nie było tego widać ani w `kubectl get jobs`, ani
    w ArgoCD, ani w poczcie.
    """
    wiersze = skrypt(jedyny_cronjob("trainer-cron.yaml"))
    wolania = [i for i, w in enumerate(wiersze)
               if "dozor.py" in w and "--co dryf" not in w]
    assert wolania, "trener nie woła już dozoru stanu nauki — to był cały nadzór nad modelem"
    for i in wolania:
        dalej = " ; ".join(wiersze[i + 1:i + 3])
        assert "$?" in dalej and "RC=" in dalej, (
            f"kod wyjścia dozoru nie jest zbierany po wierszu {wiersze[i]!r}. "
            f"Dalej stoi {dalej!r}, a powinno `D=$?` i `[ \"$D\" = 0 ] || RC=$D` — "
            f"inaczej wywrócony dozór kończy się Jobem ZIELONYM")
    assert "exit $RC" in wiersze, "zadanie musi kończyć się zebranym kodem wyjścia"


def test_dozor_stanu_nauki_NIE_jest_wyciszony():
    """Kanarek do poprzedniego: `|| true` dopisane do tej linii cofa poprawkę
    i nie zmienia niczego, co widać z zewnątrz."""
    for wiersz in skrypt(jedyny_cronjob("trainer-cron.yaml")):
        if "dozor.py" in wiersz and "--co dryf" not in wiersz:
            assert "|| true" not in wiersz and "|| :" not in wiersz, (
                f"stan nauki wyciszony: {wiersz!r}")


def test_milczec_ma_prawo_WYLACZNIE_sciezka_dryfu():
    """`|| true` przy dryfie jest świadome i opisane w szablonie: `sprawdz_dryf`
    zwraca 1 jako STAN ('dryf trwały, powiadomienie poszło'), a nie jako awarię
    przebiegu — bez tego każdy przebieg z utrzymującym się dryfem świeciłby na
    czerwono codziennie, przez cały czas trwania problemu. Cena jest znana:
    ta jedna linia połyka też własną wywrotkę. Żadna inna linia tego skryptu
    takiego prawa nie ma."""
    wyciszone = [w for w in skrypt(jedyny_cronjob("trainer-cron.yaml"))
                 if "|| true" in w or "|| :" in w]
    assert all("--co dryf" in w for w in wyciszone), (
        f"wyciszone linie spoza ścieżki dryfu: {wyciszone}")


# ------------------------------------------------------------------ zadanie 2

ZADANIA_CYKLICZNE = sorted(p.name for p in SZABLONY.glob("*.yaml")
                           if "kind: CronJob" in p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("plik", ZADANIA_CYKLICZNE)
def test_kazdy_Forbid_ma_twardy_limit_czasu(plik):
    """`concurrencyPolicy: Forbid` bez `activeDeadlineSeconds` to bomba
    zegarowa: jeden zawieszony przebieg i KAŻDY kolejny termin przepada po
    cichu. Nie ma zdarzenia, nie ma błędu, nie ma czerwonego poda — jest cisza,
    która wygląda tak samo jak spokój."""
    spec = jedyny_cronjob(plik)["spec"]
    if spec.get("concurrencyPolicy") != "Forbid":
        pytest.skip("bez Forbid pominięty przebieg nie blokuje następnych")
    assert spec["jobTemplate"]["spec"].get("activeDeadlineSeconds"), (
        f"{plik}: Forbid bez activeDeadlineSeconds — zawieszony przebieg "
        f"zablokuje wszystkie następne, a jedynym objawem będzie cisza")
    assert spec.get("startingDeadlineSeconds"), (
        f"{plik}: brak startingDeadlineSeconds — kontroler zlicza pominięte "
        f"terminy i po stu przestaje planować zadanie CAŁKOWICIE")


def test_czujka_zywotnosci_umiera_przed_nastepnym_przebiegiem():
    """Limit dłuższy niż okres nie naprawia niczego: `Forbid` i tak pominie
    następny termin, tyle że zawieszony przebieg umrze kiedyś później."""
    wartosci = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    harmonogram = wartosci["zywotnoscCron"]["schedule"]
    krok = re.match(r"\*/(\d+) \* \* \* \*", harmonogram)
    assert krok, f"nieoczekiwany harmonogram czujki: {harmonogram!r}"
    okres = int(krok.group(1)) * 60
    limit = jedyny_cronjob("zywotnosc-cron.yaml")["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
    assert limit < okres, (
        f"activeDeadlineSeconds={limit} przy okresie {okres} s — zawieszony "
        f"przebieg dożyje następnego terminu i zabierze go razem z całą warstwą "
        f"dziewięciu czujek")


@pytest.mark.parametrize("plik", ["trainer-cron.yaml", "backup.yaml"])
def test_zadania_dobowe_daja_czlowiekowi_szanse_zobaczyc_zawieszenie(plik):
    """Zadanie dobowe z limitem rzędu doby formalnie limit ma, a praktycznie
    nie: zawieszenie kończy się mniej więcej wtedy, kiedy i tak ruszyłby
    następny przebieg. Połowa doby to granica, poniżej której padnięty Job
    pojawia się w tym samym dniu roboczym, w którym powstał."""
    limit = jedyny_cronjob(plik)["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"]
    assert 0 < limit <= 43200, f"{plik}: activeDeadlineSeconds={limit} przy przebiegu dobowym"


def test_zrzut_bazy_ma_zasoby_a_limit_pamieci_nie_jest_zanizony():
    """Kontener bez `resources` na węźle 7,8 GB dzielonym z trzema projektami
    może zabrać pamięć procesowi, który handluje. Limit ZA NISKI jest jednak
    gorszy niż jego brak: OOMKill w połowie `pg_dump` to brak kopii, o którym
    dowiadujemy się w dniu odtwarzania."""
    pod = jedyny_cronjob("backup.yaml")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    dump = next(c for c in pod["containers"] if c["name"] == "dump")
    zasoby = dump.get("resources")
    assert zasoby, "kontener `dump` nie ma bloku resources"
    for strona in ("limits", "requests"):
        assert {"cpu", "memory"} <= set(zasoby.get(strona, {})), f"brak {strona} cpu/memory"
    pamiec = zasoby["limits"]["memory"]
    mega = int(re.match(r"(\d+)(Mi|Gi)$", pamiec).group(1)) * (1024 if pamiec.endswith("Gi") else 1)
    assert mega >= 256, (
        f"limit pamięci {pamiec} dla `pg_dump --compress=9` jest na styk — "
        f"OOMKill w połowie zrzutu zostawia system BEZ kopii")


# ------------------------------------------------------------------ zadanie 3

def _kontener_api() -> dict:
    api = next(d for d in manifesty("api.yaml") if d["kind"] == "Deployment")
    return api["spec"]["template"]["spec"]["containers"][0]


def test_zywotnosc_API_nie_pyta_o_baze():
    """Sonda żywotności ZABIJA kontener, więc nie ma prawa zależeć od niczego,
    czego restart nie naprawia. `/api/health` robi `SELECT count(*) FROM
    schema_migrations`: trzy wolne odpowiedzi w oknie kopii (03:17) albo trenera
    (04:40) i pod API ginie, a `migrate` wchodzi w backoff — dwuminutowa przerwa
    bazy robi się kilkuminutową niedostępnością jedynego miejsca, w którym widać,
    co się dzieje.

    Stan przejściowy 22.09.2026: ścieżka stoi z powrotem na `/api/health`,
    bo `/api/zywy` istnieje w kodzie, ale NIE w obrazie chodzącym na produkcji
    (`sha-0ea6f54`). ArgoCD wdraża chart natychmiast po merge'u, a tag obrazu
    podbija się ręcznie — sonda pytała więc o endpoint nieznany uruchomionemu
    procesowi i kubelet zabijał kontener API co niecałe półtorej minuty.

    Dlatego reguła jest tu DWUCZĘŚCIOWA i sama się rozwiązuje: dopóki tag jest
    ten sprzed poprawki, wolno wskazywać `/api/health`; z chwilą podbicia tagu
    ten test ZACZNIE PADAĆ, dopóki ktoś nie przywróci `/api/zywy`. Wyłączenie
    strażnika na czas obejścia zamieniłoby obejście w stan stały."""
    sonda = _kontener_api()["livenessProbe"]
    sciezka = sonda["httpGet"]["path"]
    kod = API_MAIN.read_text(encoding="utf-8")
    assert f'"{sciezka}"' in kod, (
        f"{sciezka} nie istnieje w {API_MAIN.name} — sonda pyta o endpoint, "
        f"którego proces nie zna, i zabije kontener po failureThreshold próbach")

    tag = VALUES["api"]["image"]["tag"]
    if tag == TAG_SPRZED_POPRAWKI:
        assert sciezka == "/api/health", (
            "dopóki produkcja chodzi na obrazie sprzed tej poprawki, liveness "
            "MUSI wskazywać endpoint, który ten obraz zna")
        return

    assert sciezka != "/api/health", (
        f"tag obrazu to {tag}, czyli nie jest już obrazem sprzed poprawki — "
        "przywróć liveness na /api/zywy. Endpoint chodzący do bazy zabija poda "
        "przy WOLNEJ bazie, a restart wolnej bazy nie naprawia")


def test_endpoint_zywotnosci_niczego_nie_odpytuje():
    """Kanarek do poprzedniego. Endpoint może się nazywać `/api/zywy` i i tak
    chodzić do bazy — wtedy przeniesienie sondy nie zmieniło nic.

    W stanie przejściowym (liveness na `/api/health`, patrz test wyżej) sprawdza
    endpoint DOCELOWY, a nie ten wpisany w chart — inaczej milczałby dokładnie
    wtedy, gdy obejście trwa, i nie zauważyłby, że `/api/zywy` w międzyczasie
    zaczął chodzić do bazy."""
    sciezka = _kontener_api()["livenessProbe"]["httpGet"]["path"]
    if sciezka == "/api/health":
        sciezka = ZYWOTNOSC_DOCELOWA
    kod = API_MAIN.read_text(encoding="utf-8")
    poczatek = kod.index(f'"{sciezka}"')
    nastepny = kod.find("\n@app.", poczatek)
    cialo = kod[poczatek:nastepny if nastepny != -1 else len(kod)]
    # Sam docstring opisuje `/api/health` i `SELECT`, więc patrzymy wyłącznie
    # na kod wykonywalny.
    cialo = re.sub(r'"{3}.*?"{3}', "", cialo, flags=re.S)
    for zakazane in ("fetch_one", "fetch_all", "execute", "await "):
        assert zakazane not in cialo, (
            f"endpoint żywotności {sciezka} używa {zakazane!r} — sonda, która "
            f"zabija kontener, nie może zależeć od niczego poza własnym procesem")


def test_gotowosc_zostaje_na_bazie_i_ma_ludzki_limit_czasu():
    """Zależność zewnętrzna należy do GOTOWOŚCI: pod bez bazy ma wypaść
    z Service i wrócić sam. Domyślny `timeoutSeconds: 1` jest progiem na
    obciążenie, a nie na awarię — w oknie kopii albo trenera wypisywałby
    zdrowego poda w najgorszym możliwym momencie."""
    sonda = _kontener_api()["readinessProbe"]
    assert sonda["httpGet"]["path"] == "/api/health", (
        "gotowość ma pytać o bazę — inaczej pod bez bazy zostaje w Service "
        "i oddaje błędy")
    assert sonda.get("timeoutSeconds", 1) >= 5, (
        f"timeoutSeconds={sonda.get('timeoutSeconds', 1)} — domyślna sekunda "
        f"to próg na obciążenie bazy, nie na jej awarię")


def test_sonda_zabijajaca_kontener_ma_progi_WPISANE():
    """Wartości domyślne (1 s, 3 próby) są niewidoczne w pliku, a decydują
    o tym, kiedy kubelet zabije proces. Próg, którego nie widać w przeglądzie
    zmian, nie jest progiem uzgodnionym."""
    sonda = _kontener_api()["livenessProbe"]
    for pole in ("timeoutSeconds", "failureThreshold"):
        assert pole in sonda, f"livenessProbe bez jawnego {pole}"
