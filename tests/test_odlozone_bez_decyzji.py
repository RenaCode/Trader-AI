"""Świeca ODŁOŻONA i nierozstrzygnięta — dziura, którą otworzyła własna poprawka.

DLACZEGO TO ISTNIEJE

Do 2026-09-22 pętla handlowała na niepełnym komplecie sygnałów: jedenaście dni
z pokryciem wag 0,02–0,10, `atr_pct` nieobecny w 590 z 646 decyzji reżimu
`range`, wejścia z jednego sygnału `funding_carry` wzmocnionego dwudziestokrotnie
(commit `d8c7759`). Poprawka postawiła bramkę `MIN_POKRYCIE = 0,9`: poniżej niej
decyzja jest **odkładana** do następnego cyklu w oknie świecy, ze śladem
`niepelne_sygnaly` w `journal.pominiecie`.

Bramka jest słuszna i zostaje. Zamieniła jednak awarię **głośną** (system
handluje bzdurą, widać to w dzienniku decyzji) na **cichą**: przy trwałym braku
cech każda świeca zostanie odłożona, nikt nie wejdzie w rynek, a silnik bez wejść
wygląda dokładnie tak samo jak silnik na spokojnym rynku. Puls bije, świece
przychodzą, zadania świecą na zielono.

Żadna z jedenastu czujek `dozor.py` tego nie widziała: `journal.pominiecie` nie
było czytane przez ani jedną z nich (sprawdzone 2026-09-27), a licznik
`odlozone_niepelne` żyje wyłącznie w pamięci pętli.

WARTOŚCIĄ ODNIESIENIA JEST ZERO, I TO JEST ZMIERZONE. Odłożenie samo w sobie nie
jest awarią — cechy pochodne są gotowe od razu po zamknięciu świecy, a cenowe
20–35 min później, więc pierwszy cykl odkłada, a któryś z następnych rozstrzyga.
Zmierzone na produkcji 2026-09-27 na całej historii `journal.pominiecie`: 40–120
odłożeń na dobę i **każde** skończyło się decyzją w swoim oknie, każdego dnia bez
wyjątku. Dlatego czujka nie ma progu tolerancji.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.engine.dozor import (
    POWODY_ODLOZENIA, dlugosc_swiecy_petli, ocen_odlozone_bez_decyzji,
    sprawdz_odlozone_decyzje,
)

KORZEN = Path(__file__).resolve().parents[1]
DOZOR = (KORZEN / "services" / "engine" / "dozor.py").read_text(encoding="utf-8")
MIGRACJA = KORZEN / "db" / "migrations" / "0068_swieca_odlozona_bez_decyzji.sql"

TERAZ = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)


def _wiersz(symbol="ADAUSDC", godzin=8, powod="niepelne_sygnaly") -> dict:
    return {"symbol": symbol, "bar_start": TERAZ - timedelta(hours=godzin),
            "powod": powod}


# ==================================================== CZYSTA FUNKCJA

def test_brak_wierszy_to_BRAK_ALARMU():
    assert ocen_odlozone_bez_decyzji([]) == []


def test_kazda_swieca_ma_WLASNY_stabilny_klucz():
    """Klucz służy do tłumienia, więc nie może zależeć od kolejności ani od
    liczby pozostałych wierszy."""
    a = ocen_odlozone_bez_decyzji([_wiersz("ADAUSDC"), _wiersz("BTCUSDC")])
    b = ocen_odlozone_bez_decyzji([_wiersz("BTCUSDC"), _wiersz("ADAUSDC")])
    assert {k for k, _ in a} == {k for k, _ in b}
    assert len({k for k, _ in a}) == 2


def test_opis_PODAJE_swiece_i_powod():
    """Bez świecy nie da się tego sprawdzić w bazie, a bez powodu nie wiadomo,
    czy zabrakło cech, czy sygnałów."""
    (_, opis), = ocen_odlozone_bez_decyzji([_wiersz("SOLUSDC", 8, "za_malo_cech")])
    assert "SOLUSDC" in opis
    assert "za_malo_cech" in opis
    assert "09-27 08:00" in opis


# ==================================================== CZUJKA

class _Stend:
    """Atrapa oddaje tylko to, o co zapytano — zapytanie o coś innego dostanie
    pustkę i test padnie, tak jak padłby pomiar."""

    def __init__(self, braki: list[dict], poza_sesja: bool = False,
                 juz_zgloszono: bool = False, swiece_min: int = 240,
                 ile_swiec: int = 6):
        self.braki, self.poza_sesja_ = braki, poza_sesja
        self.juz = juz_zgloszono
        # ZNACZNIKI SWIEC, z ktorych czujka odczytuje dlugosc swiecy petli.
        # Atrapa oddaje je osobno od brakow, bo produkcja tez pyta o nie osobnym
        # zapytaniem - i to wlasnie ten odczyt naprawia wade z 190 falszywymi
        # alarmami.
        self.znaczniki = [{"bar_start": TERAZ - timedelta(minutes=swiece_min * i)}
                          for i in range(ile_swiec, 0, -1)]
        self.wolania: list[tuple] = []
        self.zapytania: list[str] = []

    async def fetch_all(self, sql, params=None):
        self.zapytania.append(" ".join(sql.split()))
        if "engine_heartbeat" in sql or "sesja" in sql or "rynek_zamkniety" in sql:
            return [{"poza": self.poza_sesja_}] if self.poza_sesja_ else []
        if "SELECT DISTINCT bar_start FROM journal.pominiecie" in sql:
            return list(self.znaczniki)
        if "journal.pominiecie" in sql:
            return list(self.braki)
        if "notify.notification" in sql:
            return [{"?column?": 1}] if self.juz else []
        if 'app."user"' in sql:
            return [{"id": 1}]
        return []

    async def notify(self, user_id, event_code, tytul, tresc, **kw):
        self.wolania.append((event_code, tytul, tresc))
        return len(self.wolania)


def _uruchom(stend, **kw) -> int:
    return asyncio.run(sprawdz_odlozone_decyzje(
        stend.fetch_all, stend.notify, **kw))


def test_zdrowy_stan_to_ZERO_i_ZADNEGO_powiadomienia():
    stend = _Stend([])
    assert _uruchom(stend) == 0
    assert stend.wolania == []


def test_swieca_bez_decyzji_ZGLASZA_i_daje_czerwony_Job():
    stend = _Stend([_wiersz("ADAUSDC"), _wiersz("BTCUSDC")])
    kod = _uruchom(stend)
    assert kod != 0, "stan nierozstrzygniętych świec nie zapala zadania"
    assert len(stend.wolania) == 1, (
        "jedno powiadomienie na przebieg, nie jedno na świecę — kolejne i tak "
        "odrzuci cooldown katalogowy, a czujka policzyłaby je jako niezgłoszone")
    kod_zdarzenia, tytul, tresc = stend.wolania[0]
    assert kod_zdarzenia == "engine.decyzje_odlozone"
    assert "ADAUSDC" in tresc and "BTCUSDC" in tresc
    assert "ZERO" in tresc, (
        "treść nie podaje wartości odniesienia — bez niej czytelnik nie wie, "
        "czy dwie świece to dużo")


def test_ZBIEGA_do_zielonego_Joba_gdy_stan_jest_ZGLOSZONY():
    """Ta sama wada, którą zmierzono na produkcji 2026-09-24 przy czujce
    kanałów: stan trwały ma dać czerwony Job RAZ. Inaczej `trader-zywotnosc`
    świeci czerwono bez końca i przestaje cokolwiek znaczyć dla ośmiu
    pozostałych sprawdzeń w tym samym zadaniu."""
    braki = [_wiersz("ADAUSDC")]
    pierwszy = _uruchom(_Stend(braki))
    drugi = _uruchom(_Stend(braki, juz_zgloszono=True))
    assert pierwszy != 0
    assert drugi == 0, (
        f"czujka nie zbiega do zera ({drugi}) — Job będzie czerwony przy każdym "
        f"przebiegu, mimo że stan został już zgłoszony")


def test_POZA_SESJA_czujka_MILCZY_i_mowi_dlaczego(capsys):
    """Poza sesją świece nie powstają, więc nie ma czego rozstrzygać. Alarm
    przychodzący co wieczór kończy się wyciszeniem kanału — ten sam wniosek, co
    przy czujce świeżości."""
    stend = _Stend([_wiersz("AAPL")], poza_sesja=True)
    assert _uruchom(stend, engine="equity") == 0
    assert stend.wolania == []
    assert "rynek zamkniety" in capsys.readouterr().out


def test_tryb_suchy_NIE_WYSYLA_ale_ZWRACA_liczbe():
    stend = _Stend([_wiersz("ADAUSDC"), _wiersz("ETHUSDC")])
    assert _uruchom(stend, sucho=True) == 2
    assert stend.wolania == []


def test_pyta_o_POWODY_ODLOZENIA_a_nie_o_wszystkie_pominiecia():
    """`journal.pominiecie` trzyma też powody, które znaczą świadomą odmowę
    wejścia (pasmo ceny, sesja, minimum wartości). Tam decyzja ISTNIEJE i jest
    nią `hold` — policzenie ich jako braku decyzji dałoby alarm przy każdym
    przebiegu na zdrowym systemie."""
    stend = _Stend([])
    _uruchom(stend)
    # ZAPYTANIE GLOWNE, nie to o znaczniki swiec: oba czytaja
    # `journal.pominiecie`, ale tylko glowne szuka BRAKU decyzji.
    pominiecia = [q for q in stend.zapytania
                  if "journal.pominiecie" in q and "NOT EXISTS" in q]
    assert pominiecia, "czujka nie pyta o journal.pominiecie z NOT EXISTS"
    assert "p.powod = ANY(%s)" in pominiecia[0], (
        "czujka nie zawęża powodów — liczy jako brak decyzji także świadome "
        "odmowy wejścia, które decyzję `hold` mają")
    assert set(POWODY_ODLOZENIA) == {"niepelne_sygnaly", "za_malo_cech"}


def test_TRWAJACA_swieca_jest_POMIJANA():
    """Świeca w toku jest odłożona ZGODNIE Z ZAMYSŁEM — alarm o niej zapalałby
    się przy każdym przebiegu na w pełni zdrowym systemie."""
    stend = _Stend([])
    _uruchom(stend)
    q = [x for x in stend.zapytania
         if "journal.pominiecie" in x and "NOT EXISTS" in x][0]
    assert "p.bar_start < now() - make_interval(mins => %s)" in q, (
        "czujka nie odcina świecy trwającej ani świeżo zamkniętej")


# ============================== DLUGOSC SWIECY Z DANYCH
#
# WADA ZMIERZONA NA PRODUKCJI 2026-09-27, DWADZIESCIA MINUT PO WDROZENIU.
# `journal.pominiecie` nie ma kolumny z rozmiarem swiecy. Pierwsza wersja brala
# dlugosc okna z `--bar`, wiec uruchomiona z `--bar 1h` czytala te SAME wiersze
# (swiece 4h, bo tylko takie petla przetwarza) i wymagala decyzji w 60 minutach
# od otwarcia swiecy. Cechy cenowe sa gotowe 20-35 min PO zamknieciu swiecy, wiec
# powstalo 190 "swiec bez decyzji" na w pelni zdrowej produkcji - krytyczne
# powiadomienie o stanie, ktorego nie ma.

def test_dlugosc_swiecy_czytana_z_ODSTEPOW():
    swiece = [TERAZ - timedelta(hours=4 * i) for i in range(6)]
    assert dlugosc_swiecy_petli(swiece) == 240


def test_dlugosc_swiecy_ODPORNA_na_luke():
    """Brakujaca swieca (przebieg pominiety, restart) nie moze przestawic
    dlugosci - dlatego NAJCZESTSZY odstep, a nie sredni ani maksymalny."""
    swiece = [TERAZ - timedelta(hours=h) for h in (0, 4, 8, 16, 20, 24)]
    assert dlugosc_swiecy_petli(swiece) == 240


def test_MNIEJ_NIZ_TRZY_swiece_to_BRAK_ODCZYTU_a_nie_zgadywanie():
    assert dlugosc_swiecy_petli([]) is None
    assert dlugosc_swiecy_petli([TERAZ]) is None
    assert dlugosc_swiecy_petli([TERAZ, TERAZ - timedelta(hours=4)]) is None


def test_powtorzone_znaczniki_nie_daja_odstepu_zero():
    """`DISTINCT` jest w zapytaniu, ale funkcja nie moze na nim polegac —
    odstep zero dalby okno zerowe i alarm na kazdej swiecy."""
    swiece = [TERAZ, TERAZ, TERAZ - timedelta(hours=4),
              TERAZ - timedelta(hours=8)]
    assert dlugosc_swiecy_petli(swiece) == 240


def test_czujka_NIE_BIERZE_dlugosci_swiecy_z_parametru():
    """Gdyby brala, ten sam zbior wierszy dawalby inny werdykt przy innym
    `--bar` — a to bylo zrodlo 190 falszywych alarmow."""
    import inspect
    podpis = inspect.signature(sprawdz_odlozone_decyzje)
    assert "bar" not in podpis.parameters, (
        f"czujka nadal przyjmuje `bar`: {podpis} — `journal.pominiecie` nie ma "
        f"kolumny z rozmiarem swiecy, wiec ten parametr moze tylko zmyslic okno")
    assert "get_horizon" not in DOZOR.split(
        "async def sprawdz_odlozone_decyzje")[1][:3000], (
        "czujka czyta horyzont z `horizons`, a ma czytac odstep z danych")


def test_BRAK_ODCZYTU_dlugosci_to_MILCZENIE_a_nie_alarm(capsys):
    """Bez dlugosci okna pytanie „czy wrocil w oknie tej swiecy" nie ma tresci.
    Alarm w tym stanie mowilby o pomiarze, ktorego nie bylo."""
    stend = _Stend([_wiersz("ADAUSDC")], ile_swiec=2)
    assert _uruchom(stend) == 0
    assert stend.wolania == []
    assert "NIE DA SIE odczytac" in capsys.readouterr().out


# ==================================================== MIGRACJA

def test_typ_zdarzenia_ISTNIEJE_w_migracji():
    """`notify()` rzuca `ValueError` na nieznanym kodzie, więc czujka bez wpisu
    w `notify.event_type` wywróciłaby się dopiero na produkcji — przy pierwszej
    prawdziwej awarii, czyli w najgorszym możliwym momencie."""
    tresc = MIGRACJA.read_text(encoding="utf-8")
    assert "'engine.decyzje_odlozone'" in tresc
    assert "notify.event_type" in tresc
    assert "always_on" in tresc


def test_zdarzenie_jest_KRYTYCZNE_i_always_on():
    """Handel, który stoi z powodu braku danych, nie jest informacją — jest
    stanem, o którym trzeba wiedzieć niezależnie od preferencji odbiorcy."""
    tresc = MIGRACJA.read_text(encoding="utf-8")
    wstawka = tresc[tresc.index("INSERT INTO notify.event_type"):]
    assert "'critical'" in wstawka and "true" in wstawka


def test_chart_wola_czujke_RAZ_NA_SILNIK_a_nie_raz_na_horyzont():
    """Petla przetwarza jeden horyzont na silnik, a `journal.pominiecie` nie ma
    kolumny z rozmiarem swiecy. Petla po horyzontach czytala te same wiersze dwa
    razy z roznym oknem — i wezsze okno dalo 190 falszywych alarmow."""
    chart = (KORZEN / "charts" / "trader" / "templates"
             / "zywotnosc-cron.yaml").read_text(encoding="utf-8")
    linie = [l for l in chart.splitlines() if "--co odlozone" in l]
    assert linie, "czujka nie jest wolana w zadaniu zywotnosci"
    for l in linie:
        assert "--bar" not in l, (
            f"wywolanie podaje --bar, ktorego czujka nie przyjmuje: {l.strip()}")
    blok = chart[chart.index("--co odlozone") - 400:chart.index("--co odlozone")]
    assert "$tor.horyzonty" not in blok, (
        "czujka jest wolana w petli po horyzontach — te same wiersze zostana "
        "ocenione dwoma roznymi oknami")


def test_czujka_jest_PODLACZONA_do_wyboru_co():
    assert '"odlozone"' in DOZOR, "`--co odlozone` nie istnieje"
    assert 'if args.co == "odlozone":' in DOZOR, "wybór nie woła czujki"
    assert "okno_h=args.okno_h or 48" in DOZOR, (
        "`--okno-h` ma domyslnie None (dla `--co poslizg` znaczy to 'od "
        "poczatku'), a None wywrocilo by zapytanie")
