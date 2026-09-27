"""Werdykt dozoru: kiedy warto sie odezwac, a kiedy zamilknac.

DLACZEGO TO JEST TESTOWANE OSOBNO I DOKLADNIE

Powiadomienie, ktore przychodzi zawsze, przestaje cokolwiek znaczyc — odbiorca
uczy sie je pomijac wlasnie wtedy, gdy przyjdzie to jedno wazne. Prog musi wiec
byc ostry w obie strony: przepuszczac zmiany istotne i milczec przy szumie.

Skala szumu jest w tym projekcie ZMIERZONA: cztery kolejne przebiegi tego samego
kodu, rozniace sie wylacznie przesunieciem okna o dzien, daly 52,5% -> 57,5% ->
60,0% -> 62,5%. Dziesiec punktow bez zadnej zmiany. Stad prog na przedziale
ufnosci poprzedniego przebiegu, a nie na roznicy odsetka.
"""
import pytest

from services.engine.dozor import zmiany

BAZA = {
    "status": "learning",
    "windows_total": 40,
    "windows_positive": 28,          # 70%
    "positive_ci_low": 0.546,
    "positive_ci_high": 0.819,
    "median_oos_sharpe": 0.094,
    "max_psi": 0.4,
}


def wariant(**zmiana):
    return {**BAZA, **zmiana}


# --- cisza ------------------------------------------------------------------
def test_powtorzony_wynik_to_cisza():
    assert zmiany(BAZA, wariant()) == []


def test_ruch_w_granicach_szumu_to_cisza():
    """Kanarek na caly sens tego modulu.

    30/40 to 75% wobec 70% poprzednio — piec punktow procentowych. Zmierzony szum
    dobowy siega dziesieciu, a 75% miesci sie w przedziale 54,6–81,9%. Gdyby to
    wywolalo powiadomienie, dozor odzywalby sie niemal co noc.
    """
    assert zmiany(BAZA, wariant(windows_positive=30)) == []


def test_dryf_ponizej_progu_to_cisza():
    assert zmiany(BAZA, wariant(max_psi=0.9)) == []


def test_dryf_powyzej_progu_ale_MALEJACY_to_cisza():
    """Dryf, ktory opada, nie wymaga uwagi — problem sam sie cofa."""
    assert zmiany(wariant(max_psi=1.8), wariant(max_psi=1.2)) == []


# --- zmiana istotna ---------------------------------------------------------
def test_zmiana_statusu_zawsze_przechodzi():
    p = zmiany(BAZA, wariant(status="degraded"))
    assert any("status" in x for x in p)


def test_odsetek_ponizej_przedzialu_przechodzi():
    # 20/40 = 50%, ponizej dolnej granicy 54,6%
    p = zmiany(BAZA, wariant(windows_positive=20))
    assert any("poza przedzialem" in x for x in p)


def test_odsetek_powyzej_przedzialu_tez_przechodzi():
    """Poprawa poza przedzialem to rowniez zdarzenie — moze oznaczac blad,
    a nie sukces. Ten projekt mial juz wynik zawyzony o 74% przez prowizje
    liczona poza miara."""
    p = zmiany(BAZA, wariant(windows_positive=35))     # 87,5% > 81,9%
    assert any("poza przedzialem" in x for x in p)


def test_ubytek_okien_przechodzi_nawet_gdy_odsetek_rosnie():
    """Najwazniejszy przypadek w calym module.

    26 okien zamiast 40 przy WYZSZYM odsetku wyglada jak poprawa, a jest utrata
    jednej trzeciej dowodow — zwykle dlatego, ze kalibracja znalazla uklad wag,
    ktory prawie nie handluje. Dokladnie tak zachowywalo sie losowe
    przeszukiwanie, zanim je usunieto.
    """
    p = zmiany(BAZA, wariant(windows_total=26, windows_positive=20))   # 76,9%
    assert any("ubylo okien" in x for x in p)


def test_zmiana_znaku_mediany_przechodzi():
    p = zmiany(BAZA, wariant(median_oos_sharpe=-0.011))
    assert any("zmienila znak" in x for x in p)


def test_dryf_powyzej_progu_i_rosnacy_przechodzi():
    p = zmiany(BAZA, wariant(max_psi=1.4))
    assert any("dryf" in x for x in p)


# --- przypadki brzegowe -----------------------------------------------------
def test_pierwszy_przebieg_odzywa_sie_raz():
    p = zmiany(None, BAZA)
    assert len(p) == 1 and "pierwszy" in p[0]


def test_przebieg_bez_okien_informatywnych_to_alarm():
    """Zero okien to nie jest wynik ujemny — to brak wyniku, czyli awaria
    ciagu danych albo kalibracji. Musi sie odezwac natychmiast."""
    p = zmiany(BAZA, wariant(windows_total=0, windows_positive=0))
    assert len(p) == 1 and "ani jednego okna" in p[0]


def test_brak_przedzialu_u_poprzednika_nie_wywraca():
    stary = wariant(positive_ci_low=None, positive_ci_high=None)
    assert zmiany(stary, wariant(windows_positive=20)) == []


def test_kilka_powodow_naraz_jest_raportowanych_lacznie():
    p = zmiany(BAZA, wariant(status="degraded", windows_positive=12,
                             median_oos_sharpe=-0.05))
    assert len(p) >= 3


# --- kontrola swiezosci cech (services/engine/train.py) ---------------------
def test_prog_swiezosci_jest_ten_sam_co_w_petli_decyzyjnej():
    """Trener i pętla decyzyjna muszą używać JEDNEJ liczby.

    Osobna stała dobrana w trenerze rozjechałaby się z progiem, którego pętla
    używa przy odczycie point-in-time — i wtedy trener uczyłby się na cechach,
    których produkcja już nie przyjmuje.
    """
    from services.engine.horizons import HORIZONS

    for bar, h in HORIZONS.items():
        assert h.max_staleness_min > 0, bar
        # Prog musi byc dluzszy niz swieca, inaczej kazdy przebieg tuz po
        # zamknieciu bariery falszywie zglaszalby awarie.
        assert h.max_staleness_min > h.bar_minutes, (
            f"{bar}: prog {h.max_staleness_min} min nie jest dluzszy niz swieca "
            f"{h.bar_minutes} min")


# --- kanaly wiadomosci ograniczone do istniejacych silnikow -----------------
def test_kanaly_makro_zostaja_przy_filtrze_silnika():
    """Kanal bez przypisanego silnika (Fed, EBC) musi przechodzic ZAWSZE.

    Polityka pieniezna dotyczy kazdego rynku i nie da sie jej przypisac do
    jednego z nich. Gdyby filtr je odcinal, ograniczenie do jednego silnika
    kasowaloby jedyne zrodlo makro, jakie projekt ma.
    """
    import re

    from pathlib import Path

    zrodlo = Path("services/ingest/news_worker.py").read_text()
    # Warunek musi przepuszczac NULL niezaleznie od listy silnikow.
    assert re.search(r"engine IS NULL OR engine::text = ANY", zrodlo), (
        "filtr silnika nie przepuszcza kanalow makro (engine IS NULL)")
    assert re.search(r"%s::text\[\] IS NULL OR", zrodlo), (
        "pusta lista silnikow musi znaczyc 'wszystkie kanaly', nie 'zadnych'")


def _cialo(nazwa: str) -> str:
    """Ciało jednej funkcji z dozor.py — do NASTĘPNEJ konstrukcji najwyższego
    poziomu, czyli do pierwszego wiersza zaczynającego się od pierwszej kolumny.

    Cięcie „od tej funkcji do `async def main`" było kruche: wstawienie nowej
    funkcji pomiędzy nie psuło kodu, tylko test — czyli zgłaszało fałszywy
    problem tam, gdzie go nie było. Cięcie do następnej KORUTYNY było kruche tak
    samo, tylko ciszej: dopisanie stałej albo zwykłej funkcji tuż za sprawdzaną
    czujką doklejało ją do jej ciała, więc asercja o OSTATNIEJ instrukcji
    przestawała cokolwiek znaczyć — a test dalej przechodził.
    """
    import re
    from pathlib import Path

    zrodlo = Path("services/engine/dozor.py").read_text()
    start = zrodlo.index(f"async def {nazwa}")
    reszta = zrodlo[start:]
    # Naglowek moze byc lamany, ale jego dalsze wiersze sa wciete - wiec pierwszy
    # wiersz od pierwszej kolumny po naglowku to juz cos poza ta funkcja.
    po_naglowku = reszta.index("\n") + 1
    kolejna = re.search(r"\n(?=\S)", reszta[po_naglowku:])
    return reszta[:po_naglowku + kolejna.start()] if kolejna else reszta


# --- czujka zywotnosci silnikow --------------------------------------------
def test_prog_martwoty_jest_wielokrotnoscia_cyklu_petli():
    """Pętla budzi się co 300 s. Próg musi zostawiać zapas na wolniejszy cykl
    i na start poda (`sleep 90` plus kontrola pętli), inaczej każde wdrożenie
    zgłaszałoby fałszywą awarię."""
    from services.engine.dozor import MARTWY_PO_MIN

    CYKL_MIN = 5
    assert MARTWY_PO_MIN >= 3 * CYKL_MIN, \
        f"próg {MARTWY_PO_MIN} min to mniej niż trzy cykle pętli"
    assert MARTWY_PO_MIN <= 60, \
        "próg powyżej godziny czyni czujkę bezużyteczną"


def test_czujka_pomija_silnik_zatrzymany_swiadomie():
    """Silnik `equity` ma status `halted` i uderzenie sprzed dziesięciu dni, bo
    nie został jeszcze zbudowany. To nie jest awaria."""
    from pathlib import Path

    zrodlo = Path("services/engine/dozor.py").read_text()
    assert 'if s["status"] != "running":' in zrodlo, \
        "czujka nie odróżnia zatrzymania od awarii"


def test_czujka_tlumi_powtorne_zgloszenie_tej_samej_awarii():
    """`engine.down` jest always_on, więc omija zwykły cooldown — bez własnego
    tłumienia czujka krzyczałaby przy każdym przebiegu."""
    from pathlib import Path

    zrodlo = Path("services/engine/dozor.py").read_text()
    assert "created_at > %s" in zrodlo and "event_code = 'engine.down'" in zrodlo, \
        "brak tłumienia opartego o czas ostatniego uderzenia serca"


def test_czujka_liczy_kod_wyjscia_JEDNA_regula_dla_calego_pliku():
    """Kod wyjścia ma iść za tłumieniem powiadomień, a nie obok niego.

    Drugi, niezależny od kanału powiadomień ślad jest wart tyle samo co
    wcześniej — ale wart jest go ZGŁOSZENIE, a nie każdy z 144 przebiegów doby.
    Zadanie chodzi co dziesięć minut, więc `return 1` na końcu czujki znaczyło
    „144 padnięte Joby na dobę, dopóki ktoś tego nie naprawi" — a sygnał, który
    przychodzi zawsze, przestaje cokolwiek znaczyć dokładnie tak samo w
    `kubectl get jobs`, jak w powiadomieniach.

    Asercja jest o WSPÓLNEJ funkcji, a nie o kształcie tej jednej czujki:
    reguła policzona osobno w każdej z nich rozjedzie się przy pierwszej, która
    zapomni wypisać powód zera.
    """
    ogon = _cialo("sprawdz_zywotnosc")
    assert ogon.rstrip().endswith('return kod_wyjscia(zgloszone, trwale, '
                                  '"silnik bez pulsu")'), \
        "czujka liczy kod wyjścia po swojemu, obok wspólnej reguły"
    assert "trwale += 1" in ogon, \
        "awaria już zgłoszona nie jest nigdzie policzona, więc zero z tej " \
        "czujki wyjdzie nieme"


# --- czujka żywotności: puls NADZORU kontra enum SILNIKÓW -------------------
#
# Ta czujka pilnuje dwóch źródeł pulsu: `risk.engine_heartbeat` (silniki
# handlowe) i `risk.dozorca_heartbeat` (nadzorca, który sam może umrzeć).
# Klucz w tej drugiej tabeli to identyfikator PROCESU — `dozorca-ryzyka:crypto:demo`
# — a `notify.notification.engine` ma typ `ref.engine`, czyli enum SILNIKÓW.
#
# Do 2026-09-12 czujka wkładała jedno w drugie i padała z
# `InvalidTextRepresentation` DOKŁADNIE W CHWILI, W KTÓREJ MIAŁA KRZYKNĄĆ.
# Zmierzone na produkcji: dozorca leżał 887 minut (pętla kończyła każdy cykl
# `permission denied for schema journal`), nikt nie dostał ani jednego
# powiadomienia, a `trader-zywotnosc` świecił na czerwono z powodem
# niezwiązanym ze stanem silnika — czyli awaria nadzorcy chowała się za awarią
# czujki, która miała ją zgłosić.
#
# ATRAPA ZNA TYP KOLUMNY, bo inaczej ten test nie miałby na czym paść: atrapa
# przyjmująca każde `engine=` czyni tę klasę błędu niewykrywalną z definicji.
# Wartości enuma czytamy Z MIGRACJI, a nie przepisujemy tutaj — druga kopia
# rozjechałaby się z bazą przy pierwszym rozszerzeniu typu i cisza tego pliku
# przestałaby cokolwiek znaczyć.

class NieZnanySilnik(AssertionError):
    """To, czym Postgres odpowiada na napis spoza `ref.engine`.

    Dziedziczy z AssertionError celowo: to nie jest błąd atrapy, tylko
    NIEZDANY TEST — kod wkłada do kolumny wartość, której typ nie zna.
    """


def _enum_silnikow() -> set[str]:
    import re
    from pathlib import Path

    sql = Path("db/migrations/0001_extensions_and_reference.sql").read_text()
    blok = re.search(r"CREATE TYPE ref\.engine\s+AS ENUM\s*\(([^)]*)\)", sql)
    assert blok, "nie znaleziono definicji ref.engine w migracji 0001"
    wartosci = set(re.findall(r"'([^']+)'", blok.group(1)))
    assert wartosci, "definicja ref.engine sparsowana na pusto"
    return wartosci


def _puls(etykieta, *, engine=None, mode="demo", status="running",
          wiek_min=1.0):
    """Wiersz taki, jaki oddaje zapytanie czujki. `engine` = NULL dla nadzoru."""
    from datetime import datetime, timezone

    return {"engine": engine, "etykieta": etykieta, "mode": mode,
            "status": status, "wiek_min": wiek_min,
            "last_beat_at": datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)}


def _notify_z_enumem(poszly):
    """Atrapa `notify`, która odrzuca `engine` spoza `ref.engine` — tak jak baza."""
    silniki = _enum_silnikow()

    async def notify(user_id, code, tytul, tresc, **kwargs):
        silnik = kwargs.get("engine")
        if silnik is not None and silnik not in silniki:
            raise NieZnanySilnik(
                f'invalid input value for enum ref.engine: "{silnik}"')
        poszly.append((code, tytul, tresc, kwargs))

    return notify


def _zywotnosc(wiersze, *, wyslane_po=None, **kw):
    """Uruchamia `sprawdz_zywotnosc` na atrapie znającej typ `ref.engine`.

    Zwraca (kod, powiadomienia). `wyslane_po` to zbiór ETYKIET, dla których
    udajemy powiadomienie zapisane już po ostatnim uderzeniu serca — czyli
    awarię ZGŁOSZONĄ.
    """
    import asyncio

    from services.engine.dozor import sprawdz_zywotnosc

    wyslane = wyslane_po or set()
    poszly = []

    async def fetch_all(sql, params=None):
        if "notify.notification" in sql:
            return [{"x": 1}] if params[0] in wyslane else []
        # KTO POWINIEN BIC - pytanie osobne od tego, kto bije. Pusta lista
        # znaczy tu "zadna para engine/mode nie ma wlaczonych limitow", czyli
        # nie ma spodziewanego dozorcy; te testy pytaja o MARTWY puls, a nie
        # o puls, ktory nigdy nie powstal (ten ma wlasny plik:
        # tests/test_dozorca_dwa_tory.py). Bez tej galezi atrapa oddawalaby
        # wiersze pulsow na pytanie o limity, czyli odpowiadalaby nie na to,
        # o co zapytano.
        if "risk.limit_def" in sql:
            return []
        if "SELECT nazwa FROM risk.dozorca_heartbeat" in sql:
            return [{"nazwa": w["etykieta"]} for w in wiersze
                    if w.get("mode") == "nadzor"]
        if "heartbeat" in sql:
            return wiersze
        if "is_active" in sql:
            return [{"id": 1}]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")

    kod = asyncio.run(sprawdz_zywotnosc(fetch_all, _notify_z_enumem(poszly), **kw))
    return kod, poszly


def test_kanarek_atrapa_LAPIE_znany_blad():
    """Bez tego cisza pozostałych testów nie znaczyłaby „czysto", tylko
    „atrapa przyjmuje wszystko".

    Karmimy ją DOKŁADNIE tym wywołaniem, którym czujka padła na produkcji —
    i osobno wartością prawdziwą, bo atrapa łapiąca wszystko nie łapie nic.
    """
    import asyncio

    silniki = _enum_silnikow()
    assert "crypto" in silniki
    assert "dozorca-ryzyka:crypto:demo" not in silniki, \
        "enum silników handlowych zna identyfikator procesu nadzoru — wtedy " \
        "to nie jest już enum silników"

    poszly = []
    notify = _notify_z_enumem(poszly)
    with pytest.raises(NieZnanySilnik):
        asyncio.run(notify(1, "engine.down", "t", "b",
                           engine="dozorca-ryzyka:crypto:demo"))
    asyncio.run(notify(1, "engine.down", "t", "b", engine="crypto"))
    asyncio.run(notify(1, "engine.down", "t", "b", engine=None))
    assert len(poszly) == 2


def test_puls_dozorcy_NIE_IDZIE_do_kolumny_enuma():
    """Nazwa procesu nadzoru jedzie w `data`, a kolumna `engine` zostaje pusta.

    To ta sama reguła, którą stosuje czujka pulsu workerów: „market" i „news"
    też nie są silnikami handlowymi.
    """
    kod, poszly = _zywotnosc([_puls("dozorca-ryzyka:crypto:demo",
                                    mode="nadzor", wiek_min=887.0)])
    assert kod == 1
    (code, tytul, tresc, kwargs), = poszly
    assert code == "engine.down" and kwargs["severity"] == "critical"
    assert kwargs["engine"] is None, \
        "identyfikator procesu poszedł do kolumny typu ref.engine"
    assert kwargs["data"]["proces"] == "dozorca-ryzyka:crypto:demo", \
        "nazwa procesu nie jedzie w `data`, więc tłumienie nie ma po czym " \
        "rozpoznać swojego powiadomienia"
    assert "dozorca-ryzyka:crypto:demo" in tytul, \
        "alarm nie mówi, KTÓRY proces zamilkł"
    assert "trader-dozorca" in tresc and "trader-engine" not in tresc, \
        "podpowiedź wysyła po log silnika kogoś, komu padł nadzorca"


def test_martwy_silnik_NADAL_wypelnia_kolumne_engine():
    """Poprawka nie ma prawa wylać dziecka z kąpielą: dla prawdziwego silnika
    `engine` musi zostać wypełniony, bo po nim filtruje się dzwonek."""
    kod, poszly = _zywotnosc([_puls("crypto", engine="crypto", wiek_min=99.0)])
    assert kod == 1
    (_, tytul, tresc, kwargs), = poszly
    assert kwargs["engine"] == "crypto"
    assert "proces" not in kwargs["data"]
    assert tytul.startswith("Silnik crypto") and "trader-engine" in tresc


def test_awaria_NADZORCY_nie_zabiera_ze_soba_alarmu_o_SILNIKU():
    """Sedno regresji: jeden zły wiersz wywracał CAŁĄ czujkę.

    Wyjątek z zapisu powiadomienia leciał przez `main` aż do `SystemExit`, więc
    silnik martwy w tym samym przebiegu nie doczekał się już żadnego alarmu.
    """
    kod, poszly = _zywotnosc([
        _puls("crypto", engine="crypto", wiek_min=99.0),
        _puls("dozorca-ryzyka:crypto:demo", mode="nadzor", wiek_min=887.0),
    ])
    assert kod == 2                                   # dwa zgłoszenia, dwa znaleziska
    assert [k["engine"] for _, _, _, k in poszly] == ["crypto", None]


def test_tlumienie_awarii_NADZORCY_dziala_po_nazwie_procesu():
    """`engine.down` jest always_on, więc omija zwykły cooldown. Gdyby
    tłumienie pytało o samo `engine::text`, awaria dozorcy nie tłumiłaby się
    NIGDY (kolumna jest wtedy NULL-em) — czujka krzyczałaby co dziesięć minut,
    aż ktoś wyciszy kanał, a wyciszony kanał jest gorszy niż brak czujki."""
    kod, poszly = _zywotnosc([_puls("dozorca-ryzyka:crypto:demo",
                                    mode="nadzor", wiek_min=887.0)],
                             wyslane_po={"dozorca-ryzyka:crypto:demo"})
    assert poszly == []
    assert kod == 0


def test_stan_juz_zgloszony_NIE_ZNIKA_z_logu(capsys):
    """Cisza w kodzie wyjścia nie ma prawa zabrać ze sobą logu — inaczej
    zadanie zielone i log bez śladu są nieodróżnialne od spokoju."""
    _zywotnosc([_puls("dozorca-ryzyka:crypto:demo", mode="nadzor",
                      wiek_min=887.0)],
               wyslane_po={"dozorca-ryzyka:crypto:demo"})
    log = capsys.readouterr().out
    assert "MARTWY" in log and "dozorca-ryzyka:crypto:demo" in log
    assert "silnik bez pulsu" in log, \
        "log nie mówi, DLACZEGO kod wyjścia jest zerem mimo znaleziska"


def test_kanarek_STARY_KSZTALT_WIERSZA_wciaz_zabija_czujke():
    """ODTWORZENIE AWARII Z PRODUKCJI, przez prawdziwą ścieżkę kodu.

    Pozostałe testy karmią czujkę wierszem w kształcie, jaki oddaje POPRAWIONE
    zapytanie — a to jest założenie, nie dowód. Tutaj podajemy kształt sprzed
    poprawki (`nazwa AS engine`) i czujka pada dokładnie tym wyjątkiem, którym
    padała w klastrze:

        invalid input value for enum ref.engine: "dozorca-ryzyka:crypto:demo"

    Ten test ma przechodzić ZAWSZE — po poprawce też. Pilnuje granicy między
    zapytaniem a wysyłką: dopóki do klucza `engine` trafia identyfikator
    procesu, powiadomienia NIE MA, bez względu na to, co robi reszta funkcji.
    """
    with pytest.raises(NieZnanySilnik):
        _zywotnosc([_puls("dozorca-ryzyka:crypto:demo",
                          engine="dozorca-ryzyka:crypto:demo",
                          mode="nadzor", wiek_min=887.0)])


def test_zapytanie_NIE_WPUSZCZA_nazwy_procesu_do_kolumny_enuma():
    """Druga strona tej samej granicy: skoro atrapa nie wykonuje SQL-a, ktoś
    musi patrzeć na sam kształt zapytania. `nazwa` ma iść do `etykieta`,
    a `engine` dla wiersza nadzoru ma być NULL-em — i to jawnym, nie
    domyślonym."""
    # BEZ KOMENTARZY: ten plik opisuje starą postać zapytania słowami, więc
    # asercja „nie ma tu napisu X" czytałaby własne uzasadnienie poprawki.
    zrodlo = "\n".join(w for w in _cialo("sprawdz_zywotnosc").splitlines()
                       if not w.lstrip().startswith("#"))
    assert "NULL::text AS engine, nazwa AS etykieta" in zrodlo, \
        "puls nadzorcy znowu wchodzi do kolumny typu ref.engine"
    assert "nazwa AS engine" not in zrodlo


def test_tlumienie_pyta_o_ETYKIETE_a_nie_o_sama_kolumne():
    """Asercja o kształcie zapytania, bo atrapa nie wykonuje `WHERE`."""
    assert "coalesce(engine::text, data->>'proces')" in _cialo("sprawdz_zywotnosc")


# --- czujka kill-switcha ----------------------------------------------------
def test_czujka_ryzyka_pomija_zdarzenia_zwolnione():
    """Kill-switch zwolniony ręcznie to problem OBSŁUŻONY — powtarzanie alarmu
    o nim nauczyłoby ignorować alarm o nowym."""
    assert "WHERE k.released_at IS NULL" in _cialo("sprawdz_ryzyko")


def test_czujka_ryzyka_tlumi_powtorne_zgloszenie():
    cialo = _cialo("sprawdz_ryzyko")
    assert "event_code = 'killswitch.triggered'" in cialo
    assert "created_at > %s" in cialo


def test_czujka_ryzyka_opisuje_stan_LOGIEM_a_nie_kodem_wyjscia():
    """Stan trwały ma być widoczny przy każdym przebiegu — ale w logu.

    Ta czujka przeszła dwie rundy poprawek w przeciwnych kierunkach i obie
    padły na tym samym: na próbie wciśnięcia opisu STANU w kanał o jednym bicie,
    który z natury niesie „czy ten przebieg się udał". Runda 1 (`return 1 if
    nowe else 0`) gubiła stan, bo bit był wolny. Runda 2 (`return 1` bezwarunkowo)
    zatkała bit stanem tak trwałym, że przestał cokolwiek znaczyć: kill-switch
    aktywny od 2190 minut dał 144 padnięte Joby na dobę i ArgoCD trwale
    Degraded.

    Dziś kod wyjścia idzie za tłumieniem powiadomień, a opis stanu ma własny,
    nieograniczony kanał: linię „KILL-SWITCH ..." wypisywaną BEZWARUNKOWO,
    przed jakimkolwiek pytaniem o rytm. Zero z tej czujki nigdy nie jest nieme —
    i to jest jedyny powód, dla którego wolno mu być zerem.

    Reszta tej czujki — rytm przypominania i cała ścieżka wysyłki — siedzi
    w `test_kill_switch_widocznosc.py`.
    """
    ogon = _cialo("sprawdz_ryzyko")
    assert "return 1 if nowe else 0" not in ogon
    assert ogon.rstrip().endswith('return kod_wyjscia(zgloszone, trwale, '
                                  '"kill-switch aktywny")'), \
        "czujka liczy kod wyjścia po swojemu, obok wspólnej reguły"

    # Linia stanu MUSI stać przed pytaniem o tłumienie, inaczej cisza w kanale
    # powiadomień zabiera ze sobą także log — i wtedy zero jest nieme.
    stan = ogon.index('print(f"  KILL-SWITCH')
    tlumienie = ogon.index("werdykt = ocen_kill_switch")
    assert stan < tlumienie, \
        "stan wypisywany dopiero po decyzji o tłumieniu — czyli nie zawsze"


# --- czujka kopii zapasowych ------------------------------------------------
def test_czujka_kopii_uzywa_wlasnego_typu_zdarzenia():
    """Doklejenie się do `engine.down` zaśmieciłoby historię: „silnik przestał
    odpowiadać" przy działającym silniku uczy czytać alarmy nieuważnie."""
    cialo = _cialo("sprawdz_kopie")
    assert '"backup.missing"' in cialo
    assert '"engine.down"' not in cialo


def test_prog_swiezosci_kopii_zostawia_zapas_na_dobowy_harmonogram():
    from services.engine.dozor import KOPIA_STARA_PO_H

    assert 24 < KOPIA_STARA_PO_H <= 36, \
        "próg musi przekraczać dobę (zapas na wolny zrzut), ale nie tolerować dwóch"


def test_pusty_dziennik_kopii_to_nie_awaria():
    """Tuż po wdrożeniu dziennik jest pusty, bo zadanie jeszcze nie przebiegło.
    Alarm w tej sytuacji byłby fałszywy — i pierwszym, jaki zobaczy użytkownik."""
    cialo = _cialo("sprawdz_kopie")
    assert "dziennik pusty" in cialo
    i_pusty = cialo.index("dziennik pusty")
    assert "return 0" in cialo[i_pusty:i_pusty + 200]


def test_czujka_kopii_wykrywa_skurczenie():
    """Zrzut, który stracił dane, nadal przechodzi pg_restore --list."""
    from services.engine.dozor import KOPIA_SKURCZ

    assert 0 < KOPIA_SKURCZ < 1


# --- werdykt o kopiach jako czysta funkcja ----------------------------------
def _kopia(mb: float, wiek_h: float = 1.0):
    from datetime import datetime, timezone

    return {"bajtow": int(mb * 1e6), "wiek_h": wiek_h, "plik": "x.dump",
            "finished_at": datetime(2026, 9, 6, 3, 17, tzinfo=timezone.utc)}


def test_swieza_kopia_o_stalym_rozmiarze_to_cisza():
    from services.engine.dozor import ocen_kopie

    assert ocen_kopie(_kopia(149), _kopia(148)) == []


def test_kopia_ktora_urosla_to_cisza():
    """Baza rośnie, więc zrzut ma rosnąć — to jest stan normalny."""
    from services.engine.dozor import ocen_kopie

    assert ocen_kopie(_kopia(160), _kopia(149)) == []


def test_skurczenie_o_polowe_jest_zglaszane():
    from services.engine.dozor import ocen_kopie

    powody = ocen_kopie(_kopia(40), _kopia(149))
    assert len(powody) == 1 and "skurczyła" in powody[0][0]


def test_bez_poprzedniej_kopii_nie_ma_z_czym_porownac():
    """Pierwsza kopia w historii nie może być uznana za skurczoną."""
    from services.engine.dozor import ocen_kopie

    assert ocen_kopie(_kopia(1), None) == []


def test_stara_kopia_jest_zglaszana():
    from services.engine.dozor import ocen_kopie

    powody = ocen_kopie(_kopia(149, wiek_h=30), _kopia(149))
    assert len(powody) == 1 and "świeżej" in powody[0][0]


def test_stara_I_skurczona_daje_dwa_powody():
    """Oba warunki są niezależne — jeden nie może przesłonić drugiego."""
    from services.engine.dozor import ocen_kopie

    assert len(ocen_kopie(_kopia(10, wiek_h=48), _kopia(149))) == 2


# ---------------------------------------------------- świeżość danych

def test_swiezosc_prog_wyprowadzony_z_DLUGOSCI_SWIECY():
    """Stała liczba minut byłaby albo krzykliwa przy świecy 4h, albo
    bezużyteczna przy 1h.

    Pięć godzin to norma przy świecy czterogodzinnej (niecałe dwie świece,
    a więc jeszcze w granicach oczekiwanego opóźnienia) i awaria przy godzinnej
    (pięć świec).
    """
    from services.engine.dozor import ocen_swiezosc

    dla_4h = [{"co": "market.candle", "minut_swiecy": 240, "wiek_min": 300}]
    dla_1h = [{"co": "market.candle", "minut_swiecy": 60, "wiek_min": 300}]
    assert ocen_swiezosc(dla_4h) == []
    assert len(ocen_swiezosc(dla_1h)) == 1


def test_brak_danych_jest_NAZWANY_inaczej_niz_dane_stare():
    """Pusta tabela i tabela z zaległościami wymagają różnych działań:
    jedno to potok, który nigdy nie ruszył, drugie to potok, który stanął."""
    from services.engine.dozor import ocen_swiezosc

    (_, tytul_brak, _), = ocen_swiezosc(
        [{"co": "feat.feature_value", "minut_swiecy": 240, "wiek_min": None}])
    (_, tytul_stare, _), = ocen_swiezosc(
        [{"co": "feat.feature_value", "minut_swiecy": 240, "wiek_min": 9999}])
    assert "Brak danych" in tytul_brak
    assert "nieświeże" in tytul_stare


def test_swiezosc_mowi_WPROST_ze_petla_tego_nie_zglosi():
    """To jest cały powód istnienia tej czujki: silnik bije dalej i handluje
    na tym, co ma. Stare cechy nie wyglądają jak awaria."""
    from services.engine.dozor import ocen_swiezosc

    (_, _, tresc), = ocen_swiezosc(
        [{"co": "feat.feature_value", "minut_swiecy": 240, "wiek_min": 9999}])
    assert "NIE ZGŁASZA" in tresc


def test_swiezosc_sprawdza_KAZDE_zrodlo_a_nie_pierwsze_z_brzegu():
    """Świeże świece przy zamrożonych cechach to stan, w którym wszystko
    wygląda dobrze z jednej strony i jest zepsute z drugiej."""
    from services.engine.dozor import ocen_swiezosc

    powody = ocen_swiezosc([
        {"co": "market.candle", "minut_swiecy": 240, "wiek_min": 10},
        {"co": "feat.feature_value", "minut_swiecy": 240, "wiek_min": 9999},
    ])
    assert len(powody) == 1
    # Zrodlo stoi dzis w OSOBNYM polu (pole 0), a nie tylko w tytule. Wolajacy
    # tlumi powiadomienia per zrodlo, wiec potrzebuje klucza, ktorego nie
    # trzeba wylowic z polskiego zdania.
    assert powody[0][0] == "feat.feature_value"
    assert "feat.feature_value" in powody[0][1]


def test_swiezosc_pyta_o_timeframe_ZRODLOWY_a_nie_o_swiece_docelowa():
    """Świeca 4h nie istnieje w `market.candle` — jest składana ze świec 1h.

    Pierwsza wersja pytała o timeframe „4h" i dostała „brak danych" przy
    w pełni zdrowym potoku. Fałszywy alarm psuje czujkę skuteczniej niż jej
    brak: uczy odbiorcę, że można ją pomijać.
    """
    import asyncio

    zapytania = []

    async def fetch_all(sql, params=None):
        # Czujka pyta NAJPIERW o stan sesji silnika (risk.engine_heartbeat).
        # Atrapa oddająca wiersze świeżości na KAŻDE zapytanie odpowiadałaby
        # nie na to, o co zapytano — i ukryłaby każdą pomyłkę w tamtym SQL-u.
        if "risk.engine_heartbeat" in " ".join(sql.split()):
            return [{"poza": False}]
        zapytania.append(params)
        return [{"co": "market.candle", "minut_swiecy": 60, "wiek_min": 30},
                {"co": "feat.feature_value", "minut_swiecy": 240, "wiek_min": 30}]

    async def notify(*a, **k):
        raise AssertionError("nie alarmujemy przy zdrowym potoku")

    from services.engine.dozor import sprawdz_swiezosc

    assert asyncio.run(sprawdz_swiezosc(fetch_all, notify, sucho=True, bar="4h")) == 0
    assert zapytania[0]["tf"] == "1h"          # zrodlo, nie swieca docelowa
    assert zapytania[0]["min"] == 60           # prog od zrodla, nie od 4h


def test_nieznany_timeframe_nie_dostaje_progu_ZGADNIETEGO(monkeypatch):
    """Próg wyliczony ze zgadniętej długości albo krzyczy bez powodu, albo
    milczy, gdy powinien krzyknąć.

    Test idzie CAŁĄ ŚCIEŻKĄ, a nie sprawdza samego słownika: pierwsza wersja
    asertowała `MINUT_TIMEFRAME.get(...) is None` i przechodziła także wtedy,
    gdy funkcja dokładała sobie wartość domyślną przy odczycie.
    """
    import asyncio

    from services.engine import dozor, horizons

    dziwny = horizons.Horizon(**{**vars(horizons.HORIZONS["4h"]),
                                 "source_timeframe": "7h"})
    monkeypatch.setattr(horizons, "get", lambda bar: dziwny)

    async def fetch_all(sql, params=None):
        raise AssertionError("nie pytamy bazy, gdy nie wiemy, z czym porównać wiek")

    async def notify(*a, **k):
        raise AssertionError("nie alarmujemy na podstawie zgadniętego progu")

    assert asyncio.run(dozor.sprawdz_swiezosc(fetch_all, notify, sucho=True)) == 0


def test_prog_swiezosci_lezy_POWYZEJ_naturalnego_maksimum():
    """`event_time` to czas OTWARCIA świecy, a zapisujemy tylko świece kompletne.

    Zmierzone na produkcji: o 15:50 najnowsza świeca 1h ma znacznik 14:00, czyli
    110 minut — bo ta z 15:00 jeszcze trwa. Próg dwóch świec (120 min) leżałby
    dokładnie na górnej granicy normy i migałby kilka razy dziennie.

    Czujka, która miga, jest tak samo bezużyteczna jak fałszywa: obie uczą
    odbiorcę, że można je pomijać.
    """
    from services.engine.dozor import ocen_swiezosc

    def wiek(minut):
        return [{"co": "market.candle", "minut_swiecy": 60, "wiek_min": minut}]

    assert ocen_swiezosc(wiek(110)) == []      # zmierzona norma
    assert ocen_swiezosc(wiek(140)) == []      # norma + pelny odstep pobierania
    assert len(ocen_swiezosc(wiek(200))) == 1  # ponad trzy swiece — potok stanal


# ---------------------------------------------------- zdrowie egzekucji

def _stan_egz(**kw):
    baza = dict(przyjete=0, odrzucone=0, nieznane=0, id_nieznanych=[],
                powody=[], zawieszone_plany=0)
    return {**baza, **kw}


def test_JEDNO_odrzucenie_nie_budzi_nikogo():
    """Zlecenie poniżej minimalnej wartości albo chwilowy limit konta zdarzają
    się. Alarm przy każdym z nich uczyłby odbiorcę, że można je pomijać."""
    from services.engine.dozor import ocen_egzekucje

    assert ocen_egzekucje(_stan_egz(przyjete=10, odrzucone=1, powody=["x"])) == []


def test_gdy_NIC_NIE_PRZECHODZI_alarm_jest_natychmiast():
    """System stoi, a wygląda dokładnie jak system bez sygnałów: silnik bije,
    dane świeże, kill-switch milczy."""
    from services.engine.dozor import ocen_egzekucje

    (kod, tytul, tresc), = ocen_egzekucje(
        _stan_egz(przyjete=0, odrzucone=3, powody=["za mała wartość"]))
    assert kod == "order.rejected"
    assert "ANI JEDNEGO" in tresc
    assert "za mała wartość" in tresc          # powód, nie tylko liczba


def test_prog_to_STOSUNEK_a_nie_liczba_odrzucen():
    """Sto odrzuceń przy tysiącu przyjęć to inny stan niż sto przy dwóch."""
    from services.engine.dozor import ocen_egzekucje

    assert ocen_egzekucje(_stan_egz(przyjete=1000, odrzucone=100, powody=["x"])) == []
    assert len(ocen_egzekucje(_stan_egz(przyjete=2, odrzucone=100, powody=["x"]))) == 1


def test_zlecenia_o_NIEZNANYM_stanie_alarmuja_zawsze():
    """Odrzucone na pewno nie weszło. „Nieznane" znaczy, że odpowiedź nie
    dotarła — a zlecenie MOGŁO wejść. To kandydat na pozycję-widmo."""
    from services.engine.dozor import ocen_egzekucje

    powody = ocen_egzekucje(_stan_egz(przyjete=100, nieznane=1,
                                      id_nieznanych=["tai-p7"]))
    assert len(powody) == 1
    assert "tai-p7" in powody[0][2]            # identyfikator, nie sama liczba
    assert "pozycję-widmo" in powody[0][2]


def test_zawieszone_plany_zglaszane_jako_awaria_silnika():
    """Nierozstrzygnięty plan wejścia blokuje kolejną decyzję dla tego
    instrumentu — to awaria pętli, nie problem z giełdą."""
    from services.engine.dozor import ocen_egzekucje

    (kod, _, _), = ocen_egzekucje(_stan_egz(przyjete=5, zawieszone_plany=3))
    assert kod == "engine.down"


# ------------------------------------------------- czy alarm ma jak opuscic panel
def _wiersz_kanalu(kod, kanaly, login="wlasciciel", sev="critical", uid=1):
    return {"event_code": kod, "severity": sev, "user_id": uid,
            "login": login, "kanaly": kanaly}


def test_sam_inapp_to_brak_drogi_dla_alarmu():
    """ZMIERZONE 2026-09-24: `notify.delivery` mialo 89 dostarczen, WSZYSTKIE
    `inapp`, przy 39 powiadomieniach `critical` w dobie. Kill-switch dociera
    wtedy do czlowieka dopiero, gdy ten sam otworzy dashboard."""
    from services.engine.dozor import ocen_kanaly

    braki = ocen_kanaly([_wiersz_kanalu("killswitch.triggered", ["inapp"])])
    assert len(braki) == 1
    assert "wylacznie panel" in braki[0][1]


def test_brak_preferencji_tez_jest_brakiem_drogi():
    """Pusta lista kanalow znaczy to samo co sam panel: alarm nie wychodzi."""
    from services.engine.dozor import ocen_kanaly

    assert len(ocen_kanaly([_wiersz_kanalu("engine.down", [])])) == 1
    assert len(ocen_kanaly([_wiersz_kanalu("engine.down", None)])) == 1


def test_kanal_wychodzacy_zamyka_sprawe():
    from services.engine.dozor import ocen_kanaly

    assert ocen_kanaly([_wiersz_kanalu("engine.down", ["inapp", "whatsapp"])]) == []
    assert ocen_kanaly([_wiersz_kanalu("engine.down", ["webhook"])]) == []


def test_brak_liczy_sie_PER_ODBIORCA():
    """Jeden uzytkownik z WhatsAppem nie zalatwia drugiego, ktory ma sam panel —
    preferencje sa per odbiorca, wiec i brak drogi jest per odbiorca."""
    from services.engine.dozor import ocen_kanaly

    braki = ocen_kanaly([
        _wiersz_kanalu("engine.down", ["whatsapp"], login="a", uid=1),
        _wiersz_kanalu("engine.down", ["inapp"], login="b", uid=2),
    ])
    assert [k for k, _ in braki] == ["engine.down:2"]


def _stend_kanalow(*, luk: int = 5, juz_w_bazie: bool = False,
                   tryby: tuple[str, ...] = ("live",)):
    """Stend, ktory MODELUJE COOLDOWN KATALOGOWY `notify()`.

    Poprzednia wersja tej atrapy oddawala te sama odpowiedz na kazde pytanie
    o tlumienie i przyjmowala kazde `notify()`. Przez to NIE ZLAPALA wady, ktora
    produkcja pokazala przy pierwszym przebiegu (zadanie 29837520): `notify()`
    ma wlasny cooldown per (odbiorca, kod zdarzenia), wiec z pieciu wywolan
    zapisywal JEDNO, a czujka liczyla pozostale cztery jako nowe przy kazdym
    przebiegu — Job swiecil na czerwono bez konca.

    Dlatego ta atrapa odrzuca drugie powiadomienie tego samego typu dla tego
    samego odbiorcy, a zapytanie o `notify.notification` odpowiada Z TEGO, CO
    NAPRAWDE POWSTALO. Test moze wtedy sprawdzic ZBIEZNOSC do zielonego Joba,
    a nie tylko pierwszy przebieg.
    """
    baza = [("notify.no_channel", 1)] if juz_w_bazie else []
    wolania = []

    async def fetch_all(sql, params=None):
        p = " ".join(sql.split())
        if "risk.engine_heartbeat" in p:
            return [{"mode": m} for m in tryby]
        # KOLEJNOSC GALEZI JEST ISTOTNA: zapytanie o kanaly TEZ zawiera
        # `app."user"` (CROSS JOIN), wiec rozroznia je dopiero `notify.event_type`.
        # Atrapa, ktora tego nie rozroznia, oddaje liste odbiorcow na pytanie
        # o kanaly — i test pada na `KeyError`, zamiast sprawdzic cokolwiek.
        if "notify.event_type" in p:
            return [_wiersz_kanalu(f"zdarzenie{i}.krytyczne", ["inapp"], uid=1)
                    for i in range(luk)]
        if "notify.notification" in p:
            return [{"?column?": 1}] if baza else []
        if 'app."user"' in p:
            return [{"id": 1}]
        raise AssertionError(f"atrapa nie zna tego zapytania: {p[:80]}")

    async def notify(user_id, event_code, tytul, tresc, **kw):
        wolania.append((event_code, tytul))
        # COOLDOWN KATALOGOWY - tak jak `services/api/notifications.py`.
        if any(e == event_code and u == user_id for e, u in baza):
            return None
        baza.append((event_code, user_id))
        return len(baza)

    return fetch_all, notify, wolania


def test_czujka_kanalow_ZGLASZA_luke_pierwszy_raz():
    """Pierwsze zgloszenie musi dac czerwony Job — drugi, niezalezny od kanalu slad."""
    import asyncio

    from services.engine.dozor import sprawdz_kanaly

    fetch_all, notify, wolania = _stend_kanalow(luk=5)
    assert asyncio.run(sprawdz_kanaly(fetch_all, notify)) == 5
    assert len(wolania) == 1, (
        "jedno powiadomienie na przebieg, nie jedno na luke — kolejne i tak "
        "odrzuci cooldown katalogowy, a czujka policzy je jako niezgloszone")


def test_czujka_kanalow_ZBIEGA_do_zielonego_Joba():
    """WADA ZMIERZONA NA PRODUKCJI 2026-09-24 (zadanie 29837520).

    Stan trwaly ma dac czerwony Job RAZ. Jesli kolejny przebieg nadal jest
    czerwony, czerwony przestaje cokolwiek znaczyc — a `trader-zywotnosc`
    uruchamia w tym samym zadaniu osiem innych sprawdzen.
    """
    import asyncio

    from services.engine.dozor import sprawdz_kanaly

    fetch_all, notify, wolania = _stend_kanalow(luk=5)
    pierwszy = asyncio.run(sprawdz_kanaly(fetch_all, notify))
    drugi = asyncio.run(sprawdz_kanaly(fetch_all, notify))
    trzeci = asyncio.run(sprawdz_kanaly(fetch_all, notify))
    assert pierwszy == 5
    assert (drugi, trzeci) == (0, 0), (
        f"czujka nie zbiega do zera ({drugi}, {trzeci}) — Job bedzie czerwony "
        f"przy kazdym przebiegu, mimo ze stan zostal juz zgloszony")
    assert len(wolania) == 1, "powtorzenia nie maja prawa wysylac nic wiecej"


def test_tlumienie_nie_zalezy_od_LICZBY_luk():
    """Wada z produkcji brala sie z tego, ze tlumienie liczylo per luke,
    a `notify()` per odbiorce i typ. Dwa warunki mierzace co innego zawsze
    sie rozjada — ten test pilnuje, ze mierza to samo."""
    import asyncio

    from services.engine.dozor import sprawdz_kanaly

    for luk in (1, 3, 12):
        fetch_all, notify, _ = _stend_kanalow(luk=luk)
        assert asyncio.run(sprawdz_kanaly(fetch_all, notify)) == luk
        assert asyncio.run(sprawdz_kanaly(fetch_all, notify)) == 0, (
            f"przy {luk} lukach czujka nie zbiega do zera")


def test_tryb_suchy_nie_wysyla_ale_liczy():
    import asyncio

    from services.engine.dozor import sprawdz_kanaly

    fetch_all, notify, wolania = _stend_kanalow(luk=3)
    assert asyncio.run(sprawdz_kanaly(fetch_all, notify, sucho=True)) == 3
    assert wolania == []


def test_czujka_kanalow_jest_wpieta_w_zadanie_cykliczne():
    """Czujka niewolana przez nic jest dokumentacja, nie nadzorem."""
    from pathlib import Path

    cron = (Path(__file__).resolve().parents[1]
            / "charts/trader/templates/zywotnosc-cron.yaml").read_text(encoding="utf-8")
    assert "--co kanaly" in cron
    assert "|| RC=$?" in cron.split("--co kanaly")[1].split("\n")[0], \
        "kod wyjscia czujki kanalow nie idzie do RC — Job byl by zielony"


def test_czujka_kanalow_MILCZY_w_demo():
    """Decyzja wlasciciela 2026-09-24: „to jest demo, nie musze miec alarmow od
    demo, zostawiaj na stronie".

    Panel jako jedyny kanal jest wtedy WYBOREM, nie luka. Czujka zglaszajaca
    swiadomy wybor raz na dobe uczy przewijac powiadomienia — czyli psuje
    dokladnie ten kanal, ktorego pilnuje.
    """
    import asyncio

    from services.engine.dozor import sprawdz_kanaly

    fetch_all, notify, wolania = _stend_kanalow(luk=5, tryby=("demo",))
    assert asyncio.run(sprawdz_kanaly(fetch_all, notify)) == 0
    assert wolania == []


def test_czujka_kanalow_WLACZA_SIE_SAMA_na_zywo():
    """Wyciszenie, ktore trzeba PAMIETAC zdjac, jest wylaczone na stale.

    Warunek czyta `risk.engine_heartbeat.mode`, ktory kazdy silnik zapisuje przy
    kazdym cyklu — wiec pierwszy silnik przelaczony na `live` wlacza czujke bez
    niczyjej pamieci i bez zmiany w charcie.
    """
    import asyncio

    from services.engine.dozor import sprawdz_kanaly

    # Jeden silnik na zywo obok demo wystarczy: alarm dotyczy calego systemu.
    fetch_all, notify, wolania = _stend_kanalow(luk=5, tryby=("demo", "live"))
    assert asyncio.run(sprawdz_kanaly(fetch_all, notify)) == 5
    assert len(wolania) == 1


def test_brak_pulsu_silnikow_nie_wycisza_czujki():
    """Pusta tabela pulsow to nie jest dowod, ze system chodzi w demo.

    Cisza nie moze byc czytana jako zgoda — przy braku wpisow czujka pracuje
    normalnie, bo „nie wiem" nigdy nie znaczy tu „bezpiecznie".
    """
    import asyncio

    from services.engine.dozor import sprawdz_kanaly

    fetch_all, notify, _ = _stend_kanalow(luk=2, tryby=())
    assert asyncio.run(sprawdz_kanaly(fetch_all, notify)) == 2
