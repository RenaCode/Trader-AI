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


def test_czujka_konczy_sie_kodem_niezerowym_przy_awarii():
    """Drugi, niezależny sygnał: nieudane zadanie widać w `kubectl get jobs`
    nawet wtedy, gdy kanał powiadomień zawiódł."""
    from pathlib import Path

    ogon = _cialo("sprawdz_zywotnosc")
    assert ogon.rstrip().endswith("return 1"), \
        "czujka kończy się zerem mimo wykrytej awarii"


# --- czujka kill-switcha ----------------------------------------------------
def test_czujka_ryzyka_pomija_zdarzenia_zwolnione():
    """Kill-switch zwolniony ręcznie to problem OBSŁUŻONY — powtarzanie alarmu
    o nim nauczyłoby ignorować alarm o nowym."""
    assert "WHERE k.released_at IS NULL" in _cialo("sprawdz_ryzyko")


def test_czujka_ryzyka_tlumi_powtorne_zgloszenie():
    cialo = _cialo("sprawdz_ryzyko")
    assert "event_code = 'killswitch.triggered'" in cialo
    assert "created_at > %s" in cialo


def test_czujka_ryzyka_konczy_sie_kodem_niezerowym_PRZEZ_CALY_CZAS_BLOKADY():
    """Kod wyjścia opisuje STAN, a nie zdarzenie.

    Poprzednia wersja (`return 1 if nowe else 0`) zwracała zero, gdy tłumienie
    powiadomień akurat milczało — więc zatrzymany handel meldował `Complete`.
    Argument „stan znany nie może malować `kubectl get jobs` na czerwono" jest
    słuszny przy dryfie, gdzie HANDEL IDZIE DALEJ; przy kill-switchu czerwień
    jest jedynym prawdziwym opisem systemu i gaśnie sama w chwili zwolnienia.

    Reszta tej czujki — rytm przypominania i cała ścieżka wysyłki — siedzi
    w `test_kill_switch_widocznosc.py`.
    """
    ogon = _cialo("sprawdz_ryzyko")
    assert "return 1 if nowe else 0" not in ogon
    assert ogon.rstrip().endswith("return 1"), \
        "czujka kończy się zerem mimo aktywnego kill-switcha"


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

    (tytul_brak, _), = ocen_swiezosc(
        [{"co": "feat.feature_value", "minut_swiecy": 240, "wiek_min": None}])
    (tytul_stare, _), = ocen_swiezosc(
        [{"co": "feat.feature_value", "minut_swiecy": 240, "wiek_min": 9999}])
    assert "Brak danych" in tytul_brak
    assert "nieświeże" in tytul_stare


def test_swiezosc_mowi_WPROST_ze_petla_tego_nie_zglosi():
    """To jest cały powód istnienia tej czujki: silnik bije dalej i handluje
    na tym, co ma. Stare cechy nie wyglądają jak awaria."""
    from services.engine.dozor import ocen_swiezosc

    (_, tresc), = ocen_swiezosc(
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
    assert "feat.feature_value" in powody[0][0]


def test_swiezosc_pyta_o_timeframe_ZRODLOWY_a_nie_o_swiece_docelowa():
    """Świeca 4h nie istnieje w `market.candle` — jest składana ze świec 1h.

    Pierwsza wersja pytała o timeframe „4h" i dostała „brak danych" przy
    w pełni zdrowym potoku. Fałszywy alarm psuje czujkę skuteczniej niż jej
    brak: uczy odbiorcę, że można ją pomijać.
    """
    import asyncio

    zapytania = []

    async def fetch_all(sql, params=None):
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
