"""Czujka dryfu cech: stan trwale zły ma się odezwać, ale nie migać.

DLACZEGO TEN PLIK ISTNIEJE OSOBNO OD `test_dozor.py`

`test_dozor.py` pilnuje reguły „zgłaszaj zmianę, nie stan" i pilnuje jej dobrze.
Ten plik pilnuje wyjątku od niej — jedynego, jaki ten moduł ma — więc miesza się
z tamtym równie źle, jak mieszałyby się same czujki.

CO ZMIERZONO NA PRODUKCJI, ZANIM TO POWSTAŁO

Dryf 0,695 utrzymywał się dniami, a `notify.notification` nie miał ANI JEDNEGO
wiersza z `event_code='learning.drift_high'`. Złożyły się na to trzy niezależne
przyczyny, z których KAŻDA W POJEDYNKĘ wystarczała do ciszy:

  1. żadna linia kodu nie wysyłała `learning.drift_high` — typ zdarzenia istniał
     w katalogu od migracji 0011 wyłącznie jako wiersz w tabeli,
  2. jedyny tor zgłoszenia dryfu (`zmiany`) wymagał WZROSTU, a stan, który stoi,
     nie rośnie,
  3. próg w tym torze to 1,0, a nie 0,25 obiecane w katalogu i na dashboardzie —
     bo mierzona wielkość nie jest PSI, tylko standaryzowanym przesunięciem
     rozkładu, do którego skala PSI się nie stosuje.

Każdy test poniżej trzyma jedną z tych przyczyn zamkniętą.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.engine.dozor import (
    DRYF_CECH_W_TRESCI,
    DRYF_PRZYPOMNIENIE_DNI,
    DRYF_TRWALY_GODZIN,
    DRYF_TRWALY_PROG,
    DRYF_TRWALY_PRZEBIEGOW,
    PSI_PROG,
    ocen_dryf_trwaly,
    podsumuj_dryf_cech,
    zmiany,
)

TERAZ = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)


def historia(*wartosci: float, co_godzin: int = 24,
             od: datetime = TERAZ) -> list[dict]:
    """Dziennik stanu nauki, najnowszy pierwszy — tak jak zwraca zapytanie."""
    return [{"max_psi": w, "run_at": od - timedelta(hours=co_godzin * i)}
            for i, w in enumerate(wartosci)]


# --- 1. przyczyna pierwsza: zdarzenie, którego nikt nie wysyłał --------------
def test_zdarzenie_drift_high_ma_nadawce_w_kodzie():
    """Kanarek na przyczynę, przez którą licznik powiadomień wynosił zero.

    `learning.drift_high` był zadeklarowany w katalogu zdarzeń, włączony
    domyślnie u wszystkich odbiorców i nieużywany przez ŻADNĄ linię kodu.
    Zapytanie `count(*) ... WHERE event_code='learning.drift_high'` musiało dać
    zero niezależnie od tego, jak wysoki był dryf — i dawało.

    Sam katalog nie jest tu dowodem: typ zdarzenia bez nadawcy to obietnica,
    której nic nie spełnia.
    """
    zrodlo = Path("services/engine/dozor.py").read_text()
    assert '"learning.drift_high"' in zrodlo, \
        "żadna czujka nie wysyła learning.drift_high — zdarzenie jest martwe"


def test_dryf_nie_dokleja_sie_do_zdarzenia_o_zmianie_stanu():
    """Alarm pod cudzą nazwą uczy czytać alarmy nieuważnie.

    Ten sam powód, dla którego czujka kopii zapasowych ma własny
    `backup.missing`, a nie dokleja się do `engine.down`. Dodatkowo: preferencja
    odbiorcy dotycząca dryfu nie miałaby wtedy czego włączać ani wyłączać.
    """
    zrodlo = Path("services/engine/dozor.py").read_text()
    cialo = zrodlo[zrodlo.index("async def sprawdz_dryf"):]
    cialo = cialo[:cialo.index("\nasync def ")]
    assert '"learning.drift_high"' in cialo
    assert '"learning.state_changed"' not in cialo


# --- 2. przyczyna druga: stan trwały nie jest zmianą ------------------------
def test_stan_trwale_zly_odzywa_sie_mimo_ze_NIE_ROSNIE():
    """Najważniejszy test w tym pliku — dokładnie sytuacja z produkcji.

    Trzy noce po 0,695: nic nie rośnie, więc `zmiany` milczy i ma rację, bo
    zmiany nie ma. Ale rozkład cech odjechał i model nadal na nim handluje.
    """
    w = ocen_dryf_trwaly(historia(0.695, 0.695, 0.695), teraz=TERAZ)
    assert w is not None, "stan trwale zły nadal jest niewidoczny"
    assert w["dryf"] == 0.695 and w["pierwsze"] is True


def test_ta_sama_sytuacja_jest_cisza_dla_czujki_zmiany():
    """Kontrola negatywna: bez niej powyższy test nie dowodzi niczego nowego.

    Gdyby `zmiany` też się tu odzywało, druga czujka byłaby duplikatem, a nie
    uzupełnieniem — i produkowałaby dwa powiadomienia o jednej rzeczy.
    """
    baza = {"status": "learning", "windows_total": 40, "windows_positive": 28,
            "positive_ci_low": 0.546, "positive_ci_high": 0.819,
            "median_oos_sharpe": 0.094, "max_psi": 0.695}
    assert zmiany(baza, dict(baza)) == []


def test_dryf_ktory_opadl_ponizej_progu_rozbraja_czujke_natychmiast():
    """Rozbrajać ma być łatwo. Czujka nadająca długo po naprawie uczy, że
    powiadomienie o dryfie nic nie znaczy."""
    assert ocen_dryf_trwaly(historia(0.30, 0.80, 0.80), teraz=TERAZ) is None


def test_pojedynczy_wyskok_nie_uzbraja_czujki():
    """Uzbrajać ma być trudno. Jeden odczyt ponad progiem to artefakt okna
    porównawczego, a nie zmiana reżimu — a czujka, która miga, jest tak samo
    bezużyteczna jak fałszywa."""
    assert ocen_dryf_trwaly(historia(0.90, 0.20, 0.20), teraz=TERAZ) is None


def test_za_krotka_historia_to_cisza_a_nie_alarm():
    """Tuż po wdrożeniu dziennik jest krótki. Alarm z braku danych byłby
    pierwszym, jaki zobaczy użytkownik — i fałszywym."""
    assert ocen_dryf_trwaly(historia(0.9, 0.9), teraz=TERAZ) is None


# --- 3. przyczyna trzecia: próg z niewłaściwej literatury -------------------
def test_prog_trwalosci_jest_nizszy_niz_prog_wzrostu():
    """Dwa progi, bo dwa różne dowody.

    Czujka zmiany odzywa się po JEDNYM odczycie, więc musi żądać efektu dużego.
    Czujka trwałości ma drugi warunek — utrzymywanie się — który sam w sobie
    jest dowodem, więc może żądać efektu średniego. Odwrotna relacja progów
    znaczyłaby, że stan trwały jest trudniejszy do zgłoszenia niż chwilowy.
    """
    assert DRYF_TRWALY_PROG < PSI_PROG


def test_progi_pochodza_ze_skali_wielkosci_efektu_a_nie_z_PSI():
    """Kanarek na pomyłkę, która kosztowała ten projekt cztery razy zawyżony
    próg alarmowy.

    Mierzona wielkość to |Δśrednia|/sd + |Δsd|/sd, czyli d Cohena plus człon
    rozrzutu — a nie Population Stability Index. Progi Siddiqiego (0,10 / 0,25)
    do niej NIE PASUJĄ; pasują progi Cohena (0,2 / 0,5 / 0,8). Próg 0,25
    przeniesiony z PSI dawałby alarm przy przesunięciu o ćwierć odchylenia,
    czyli przy efekcie poniżej „małego" — na szeregu finansowym co tydzień.
    """
    assert DRYF_TRWALY_PROG == 0.5, "próg trwałości ≠ efekt średni u Cohena"
    assert PSI_PROG >= 0.8, "próg wzrostu poniżej efektu dużego u Cohena"
    assert DRYF_TRWALY_PROG > 0.25, \
        "próg z literatury o PSI zastosowany do miary, która PSI nie jest"


def test_tresc_powiadomienia_prostuje_nazwe_miary():
    """Odbiorca porównujący liczbę z progiem 0,25 obiecanym w katalogu zdarzeń
    i na dashboardzie wyciągnie zły wniosek. Powiadomienie musi powiedzieć, co
    ta liczba naprawdę mierzy — inaczej naprawiamy ciszę, a zostawiamy pomyłkę.
    """
    zrodlo = Path("services/engine/dozor.py").read_text()
    cialo = zrodlo[zrodlo.index("async def sprawdz_dryf"):]
    cialo = cialo[:cialo.index("\nasync def ")]
    assert "NIE PSI" in cialo


# --- rytm przypominania -----------------------------------------------------
def test_pierwsze_utrwalenie_odzywa_sie_od_razu():
    w = ocen_dryf_trwaly(historia(0.7, 0.7, 0.7), teraz=TERAZ)
    assert w is not None and w["pierwsze"] is True


def test_kolejny_przebieg_tego_samego_dnia_juz_nie_krzyczy():
    """Powtórka przy każdym przebiegu to powiadomienie codzienne, a takie
    przestaje cokolwiek znaczyć dokładnie tak samo jak cisza."""
    w = ocen_dryf_trwaly(historia(0.7, 0.7, 0.7),
                         ostatnie_zgloszenie=TERAZ - timedelta(days=1),
                         zgloszony_dryf=0.7, teraz=TERAZ)
    assert w is None


def test_po_tygodniu_bez_poprawy_przypomina():
    """Ani cisza, ani powtórka. Stan, który trwa tydzień, to nie ten sam
    komunikat — to informacja, że nikt na niego nie zareagował."""
    w = ocen_dryf_trwaly(historia(0.7, 0.7, 0.7),
                         ostatnie_zgloszenie=TERAZ - timedelta(
                             days=DRYF_PRZYPOMNIENIE_DNI, hours=1),
                         zgloszony_dryf=0.7, teraz=TERAZ)
    assert w is not None and w["pierwsze"] is False and w["eskalacja"] is False
    assert "NADAL" in w["powod"]


def test_pogorszenie_pasma_lamie_rytm_tygodniowy():
    """Przejście ze średniego dryfu w duży to NOWA informacja, nie to samo
    przypomnienie. Trzymanie jej w kolejce przez tydzień byłoby ciszą dokładnie
    wtedy, gdy coś się dzieje."""
    w = ocen_dryf_trwaly(historia(1.4, 1.4, 1.4),
                         ostatnie_zgloszenie=TERAZ - timedelta(hours=6),
                         zgloszony_dryf=0.7, teraz=TERAZ)
    assert w is not None and w["eskalacja"] is True and w["pasmo"] == "duzy"


def test_pogorszenie_w_obrebie_tego_samego_pasma_NIE_lamie_rytmu():
    """Kanarek na eskalację zbyt czułą. 0,60 → 0,75 to ten sam stan mierzony
    dzień później; gdyby to łamało rytm, „przypomnienie tygodniowe" wracałoby do
    bycia powiadomieniem codziennym tylnymi drzwiami."""
    w = ocen_dryf_trwaly(historia(0.75, 0.75, 0.75),
                         ostatnie_zgloszenie=TERAZ - timedelta(hours=6),
                         zgloszony_dryf=0.60, teraz=TERAZ)
    assert w is None


def test_rytm_jest_dluzszy_niz_odstep_miedzy_przebiegami_trenera():
    """Trener chodzi raz na dobę. Rytm przypominania równy dobie — a taki jest
    domyślny cooldown zdarzenia w katalogu (86400 s) — przepuszczałby KAŻDY
    przebieg, czyli nie byłby rytmem, tylko jego pozorem."""
    assert DRYF_PRZYPOMNIENIE_DNI > 1


# --- dwa przebiegi tego samego dnia -----------------------------------------
def test_trzy_przebiegi_z_jednej_nocy_to_nie_jest_stan_trwaly():
    """Trener bywa uruchamiany powtórnie tego samego dnia — po awarii, przy
    nadrabianiu zaległości, ręcznie. Bez warunku na rozpiętość w czasie trzy
    takie wiersze udawałyby trzy noce i „trwały" znaczyłoby „chwilowy"."""
    assert ocen_dryf_trwaly(historia(0.7, 0.7, 0.7, co_godzin=1),
                            teraz=TERAZ) is None


def test_rozpietosc_dokladnie_na_progu_juz_wystarcza():
    """Granica musi być po stronie działania, nie ciszy: trzy kolejne nocne
    przebiegi dają dokładnie 48 godzin i to jest przypadek NORMALNY, a nie
    brzegowy. Próg wykluczający go wstrzymywałby każdy alarm o dobę."""
    okno = historia(0.7, 0.7, 0.7, co_godzin=DRYF_TRWALY_GODZIN // 2)
    assert ocen_dryf_trwaly(okno, teraz=TERAZ) is not None


def test_czujka_filtruje_dziennik_po_horyzoncie():
    """Świeca 4h i 1h mają osobne przebiegi tej samej nocy (migracja 0025) —
    pomiar z produkcji to 39 okien przy Sharpe +0,101 dla jednej i 88 okien przy
    −0,029 dla drugiej. Bez filtra po `bar` „trzy kolejne przebiegi" mieszałyby
    dwa różne modele, a rytm przypominania jednego uciszałby drugi.
    """
    zrodlo = Path("services/engine/dozor.py").read_text()
    cialo = zrodlo[zrodlo.index("async def sprawdz_dryf"):]
    cialo = cialo[:cialo.index("\nasync def ")]
    assert "WHERE engine = %s AND bar = %s" in cialo, \
        "dziennik stanu czytany bez filtra po horyzoncie"
    assert "data->>'bar' = %s" in cialo, \
        "historia powiadomień czytana bez filtra po horyzoncie"


# --- które cechy odjechały --------------------------------------------------
def test_rozpiska_wskazuje_konkretne_cechy():
    """„Coś się zmieniło" bez wskazania czego nie prowadzi do żadnego działania.
    Odbiorca musi wiedzieć, czy dryf siedzi w jednej rodzinie cech (do naprawy),
    czy jest rozlany po wszystkich (zmiana reżimu rynku)."""
    r = podsumuj_dryf_cech([{"feature": "rsi_14@1", "psi": 1.31},
                            {"feature": "atr_pct@2", "psi": 0.72},
                            {"feature": "vol_z@1", "psi": 0.11}])
    assert r == ["rsi_14@1: 1.31", "atr_pct@2: 0.72"]


def test_rozpiska_pomija_cechy_spokojne():
    """Cecha poniżej progu nie dodaje informacji, a rozciąga komunikat — po
    kilku takich odbiorca przestaje go czytać do miejsca, gdzie stoi ta ważna."""
    r = podsumuj_dryf_cech([{"feature": "a@1", "psi": 0.9},
                            {"feature": "b@1", "psi": 0.1},
                            {"feature": "c@1", "psi": 0.0}])
    assert r == ["a@1: 0.90"]


def test_rozpiska_nie_milknie_gdy_zadna_cecha_nie_przekracza_progu():
    """`learn.drift_metric` jest kluczowana po wersji modelu, a dziennik stanu
    po (engine, bar) — mogą się rozjechać. Czujka, która w tej sytuacji nie
    powie NIC o cechach, milczy o powodzie, dla którego właśnie krzyknęła."""
    r = podsumuj_dryf_cech([{"feature": "a@1", "psi": 0.2},
                            {"feature": "b@1", "psi": 0.1}])
    assert r == ["a@1: 0.20", "b@1: 0.10"]


def test_rozpiska_jest_ucieta_do_czolowki():
    r = podsumuj_dryf_cech([{"feature": f"f{i}@1", "psi": 9.0 - i}
                            for i in range(20)])
    assert len(r) == DRYF_CECH_W_TRESCI


def test_brak_wartosci_psi_nie_wywraca_rozpiski():
    """`learn.drift_metric.psi` jest nullowalna, a `ks_stat` nie jest w ogóle
    wypełniana. Czujka wywrócona przez NULL to czujka, która nie działa."""
    assert podsumuj_dryf_cech([{"feature": "a@1", "psi": None}]) == ["a@1: 0.00"]


def test_brak_wartosci_dryfu_w_dzienniku_rozbraja_a_nie_wywraca():
    """`max_psi` bywa NULL — po `purge_demo`, przy przebiegu bez aktywnych cech.
    Brak pomiaru to nie jest pomiar zerowy ani alarm; to brak podstaw do
    werdyktu."""
    assert ocen_dryf_trwaly(historia(0.9, 0.9, 0.9)[:2]
                            + [{"max_psi": None,
                                "run_at": TERAZ - timedelta(hours=48)}],
                            teraz=TERAZ) is None


# --- czujka nie zmienia zachowania systemu ----------------------------------
def test_czujka_nie_wstrzymuje_handlu():
    """Dodanie czujki to dodanie czujki. Wstrzymanie handlu jest zmianą
    zachowania systemu i wymaga własnego uzasadnienia oraz własnej decyzji —
    a przemycone przy okazji alarmu byłoby najgorszym możliwym trybem: system
    przestaje handlować i nikt nie wie dlaczego.
    """
    zrodlo = Path("services/engine/dozor.py").read_text()
    cialo = zrodlo[zrodlo.index("async def sprawdz_dryf"):]
    cialo = cialo[:cialo.index("\nasync def ")]
    for zakazane in ("killswitch", "kill_switch", "UPDATE ", "INSERT INTO",
                     "halted", "trading_enabled"):
        assert zakazane not in cialo, \
            f"czujka dryfu robi coś więcej niż czujka: {zakazane}"


def test_czujka_jest_wywolywana_po_nocnym_treningu():
    """Czujka, której nic nie uruchamia, jest tak samo cicha jak jej brak —
    a dokładnie tym był `learning.drift_high` przez cały czas swojego istnienia.
    """
    chart = Path("charts/trader/templates/trainer-cron.yaml").read_text()
    # Linia ZAKOMENTOWANA przechodziłaby zwykłe `in` — a zakomentowana czujka
    # jest dokładnie tak samo cicha jak nieistniejąca. Ten kanarek złapano przy
    # pisaniu tego testu, więc warunek pilnuje właśnie tego przypadku.
    wywolania = [w.strip() for w in chart.splitlines()
                 if "--co dryf" in w and not w.strip().startswith("#")]
    assert wywolania, "czujka dryfu nie jest uruchamiana przez żadne zadanie"


@pytest.mark.parametrize("wartosci,oczekiwane_pasmo", [
    ((0.7, 0.7, 0.7), "sredni"),
    ((1.4, 1.4, 1.4), "duzy"),
])
def test_pasmo_odroznia_srednie_od_duzego(wartosci, oczekiwane_pasmo):
    w = ocen_dryf_trwaly(historia(*wartosci), teraz=TERAZ)
    assert w is not None and w["pasmo"] == oczekiwane_pasmo


# --- czujka jako całość, nie sam werdykt ------------------------------------
#
# Testy powyżej sprawdzają czysty werdykt, a kilka z nich czyta źródło. Żaden
# nie dowodzi, że czujka SKŁADA SIĘ w całość: że zapytania trafiają tam, gdzie
# trzeba, że wynik werdyktu dojeżdża do `notify` i że rozpiska cech ląduje
# w treści. Czujka poprawna w kawałkach i rozłączona w środku jest równie cicha
# jak żadna — a dokładnie tym był `learning.drift_high` do tej pory.


class AtrapaBazy:
    """Minimalna atrapa: rozpoznaje zapytania po fragmencie, nie udaje bazy."""

    def __init__(self, dziennik, powiadomienia=(), cechy=()):
        self.dziennik, self.powiadomienia, self.cechy = dziennik, powiadomienia, cechy
        self.zapytania = []

    async def fetch_all(self, sql, params=None):
        self.zapytania.append(sql)
        if "learning_state_log" in sql:
            return list(self.dziennik)
        if "notify.notification" in sql:
            return list(self.powiadomienia)
        if "drift_metric" in sql:
            return list(self.cechy)
        if "app." in sql:
            return [{"id": 1}, {"id": 2}]
        raise AssertionError(f"nieoczekiwane zapytanie: {sql}")


def uruchom(atrapa, **kw):
    """Zwraca (kod wyjścia, lista wysłanych powiadomień)."""
    import asyncio

    from services.engine.dozor import sprawdz_dryf

    wyslane = []

    async def notify(user_id, code, tytul, tresc, **reszta):
        wyslane.append({"user_id": user_id, "code": code, "tytul": tytul,
                        "tresc": tresc, **reszta})

    kod = asyncio.run(sprawdz_dryf(atrapa.fetch_all, notify, teraz=TERAZ, **kw))
    return kod, wyslane


def test_czujka_wysyla_powiadomienie_o_stanie_trwalym():
    """Ścieżka alarmowa przechodzona od zapytania do `notify` — ta sama, którą
    na produkcji nikt nigdy nie przeszedł."""
    a = AtrapaBazy(historia(0.695, 0.70, 0.68),
                   cechy=[{"feature": "rsi_14@1", "psi": 0.9},
                          {"feature": "atr_pct@1", "psi": 0.6}])
    kod, wyslane = uruchom(a)
    assert kod == 1
    assert [w["code"] for w in wyslane] == ["learning.drift_high"] * 2, \
        "powiadomienie nie poszło albo poszło pod złym kodem zdarzenia"
    assert wyslane[0]["data"]["cechy"] == ["rsi_14@1: 0.90", "atr_pct@1: 0.60"], \
        "treść nie mówi, KTÓRE cechy odjechały"
    assert wyslane[0]["data"]["bar"] == "4h", \
        "bez `bar` w danych rytm przypominania nie da się rozróżnić per horyzont"


def test_czujka_milczy_w_oknie_przypomnienia():
    """Powiadomienie sprzed doby ucisza kolejny przebieg — i kod wyjścia też
    jest zerowy, żeby `kubectl get jobs` nie malował się na czerwono co noc
    z powodu stanu, który jest już zgłoszony i czeka na reakcję."""
    a = AtrapaBazy(historia(0.7, 0.7, 0.7),
                   powiadomienia=[{"created_at": TERAZ - timedelta(days=1),
                                   "dryf": "0.7"}])
    kod, wyslane = uruchom(a)
    assert (kod, wyslane) == (0, [])


def test_czujka_nie_pyta_o_cechy_gdy_ma_milczec():
    """Rozpiska kosztuje złączenie czterech tabel. Czujka, która liczy ją przy
    każdym przebiegu, żeby jej nie użyć, płaci za nic co noc."""
    a = AtrapaBazy(historia(0.1, 0.1, 0.1))
    kod, wyslane = uruchom(a)
    assert (kod, wyslane) == (0, [])
    assert not any("drift_metric" in q for q in a.zapytania)


def test_czujka_na_krotkim_dzienniku_nie_pyta_o_nic_wiecej():
    """Tuż po wdrożeniu dziennik jest krótki — to nie jest alarm i nie jest
    powód, żeby odpytywać resztę bazy."""
    a = AtrapaBazy(historia(0.9, 0.9))
    kod, wyslane = uruchom(a)
    assert (kod, wyslane) == (0, [])
    assert len(a.zapytania) == 1


def test_tryb_suchy_nie_wysyla_ale_zglasza_kodem():
    """Bez tego jedyną drogą sprawdzenia, czy ścieżka alarmowa działa, byłoby
    czekanie na prawdziwy dryf na produkcji — czyli nie sprawdziłby jej nikt."""
    a = AtrapaBazy(historia(0.7, 0.7, 0.7), cechy=[{"feature": "a@1", "psi": 0.7}])
    kod, wyslane = uruchom(a, sucho=True)
    assert kod == 1 and wyslane == []


def test_liczba_przebiegow_jest_ta_sama_co_przy_pozostalych_czujkach():
    """Trzy, a nie dwa — ta sama liczba i ten sam powód co przy SWIEC_ZWLOKI,
    CYKLI_ZWLOKI i MARTWY_PO_MIN. Próg dwóch leży na górnej granicy normalnego
    wahania i czujka migałaby."""
    assert DRYF_TRWALY_PRZEBIEGOW == 3
