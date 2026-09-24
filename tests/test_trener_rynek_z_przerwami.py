"""Progi trenera liczą się z DANYCH, nie z kalendarza.

Dwa strażniki w `train.py` były napisane przy milczącym założeniu, że rynek jest
notowany bez przerwy. Oba przepuszczały krypto i oba zatrzymywały giełdę — każdy
inaczej i żaden przez awarię:

  * świeżość cech mierzona zegarem (`now - najnowsza_cecha`). Ostatnia świeca
    giełdowa zamyka się o 20:00 UTC, następna przychodzi nazajutrz o 13:00, więc
    w pełni aktualna cecha ma nad ranem 800 minut przy progu 180. Zadanie
    cykliczne trenera chodzi o 02:40 UTC, czyli ZAWSZE przy zamkniętej giełdzie:
    każdy nocny trening equity przerywałby się, meldując awarię działającego
    potoku.

  * próg pokrycia okna treningowego liczony jako ćwierć świec, które zmieściłyby
    się w oknie na rynku ciągłym (540 dla świecy 1h i okna 90 dni). Giełda daje
    w tym samym oknie 521-524 świece, więc próg przechodził ZERO instrumentów,
    każde okno odpadało na MIN_INSTRUMENTS, a walk-forward kończył się z zerem
    okien i komunikatem „za mało informatywnych okien" — brzmiącym jak brak
    danych, a nie jak próg policzony dla innego rynku.

Drugi przypadek przegrywał o niecałe cztery procent, więc odrobinę gęstsze dane
sprawiłyby, że trener „zacząłby działać" bez naprawienia czegokolwiek.
"""
from datetime import datetime, timedelta, timezone

from services.engine.horizons import H1H, H4H
from services.engine.train import MIN_SWIEC_OKNA, cechy_nadazaja, prog_pokrycia

# Czwartek nad ranem — pora, o której chodzi cykliczny trener (02:40 UTC).
NAD_RANEM = datetime(2026, 9, 24, 2, 40, tzinfo=timezone.utc)
# Ostatnia świeca giełdowa przed tą chwilą: środa 19:00 UTC.
OSTATNIA_GIELDOWA = datetime(2026, 9, 23, 19, 0, tzinfo=timezone.utc)


# ====================================================== świeżość cech

def test_cechy_gieldy_sa_swieze_nad_ranem_przy_zamknietym_rynku():
    """Sedno pierwszej wady. Zegarem wychodziło tu 800 minut przy progu 180."""
    # Cecha świecy 18:00 powstaje po jej zamknięciu, czyli o 19:00 — jedną
    # świecę za ostatnią, którą potok w ogóle mógł policzyć.
    cecha = OSTATNIA_GIELDOWA - timedelta(hours=1)
    assert cechy_nadazaja(cecha, OSTATNIA_GIELDOWA, H1H.max_staleness_min), (
        "trener uznał zdrowy potok cech za zatrzymany tylko dlatego, że rynek "
        "jest o tej porze zamknięty — nocny trening equity nie ruszy ani razu")


def test_zegar_UZNALBY_te_same_cechy_za_przeterminowane():
    """Pin pod samą przyczynę, żeby test wyżej nie przechodził przypadkiem."""
    cecha = OSTATNIA_GIELDOWA - timedelta(hours=1)
    wiek_zegarowy_min = (NAD_RANEM - cecha).total_seconds() / 60
    assert wiek_zegarowy_min > H1H.max_staleness_min, (
        f"wiek zegarowy {wiek_zegarowy_min:.0f} min nie przekracza progu "
        f"{H1H.max_staleness_min} min — ten test przestał odtwarzać przerwę "
        f"nocną i nie dowodzi już niczego")


def test_po_weekendzie_tez():
    """Przerwa weekendowa to ponad 3900 minut — dwudziestokrotność progu."""
    piatek = datetime(2026, 9, 18, 19, 0, tzinfo=timezone.utc)
    assert cechy_nadazaja(piatek - timedelta(hours=1), piatek,
                          H1H.max_staleness_min)


def test_stojacy_potok_cech_nadal_zatrzymuje_trening():
    """Poprawka nie może rozbroić strażnika — po to on jest.

    Gdy świece idą, a cechy stoją, różnica rośnie i trening MUSI paść.
    """
    cecha = OSTATNIA_GIELDOWA - timedelta(hours=21)
    assert not cechy_nadazaja(cecha, OSTATNIA_GIELDOWA, H1H.max_staleness_min), (
        "cechy spóźnione o 21 godzin wobec świec nie zatrzymały treningu — "
        "dokładnie ta awaria zdarzyła się 2026-09-04")


def test_krypto_dziala_jak_dotad():
    """Na rynku ciągłym ostatnia świeca jest tuż przed teraz, więc próg działa
    tak samo jak przy pomiarze zegarem."""
    teraz = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
    assert cechy_nadazaja(teraz - timedelta(hours=1), teraz,
                          H1H.max_staleness_min)
    assert not cechy_nadazaja(teraz - timedelta(hours=25), teraz,
                              H1H.max_staleness_min)


# ====================================================== próg pokrycia okna

def _stary_prog(h) -> int:
    """Próg sprzed poprawki — ćwierć świec mieszczących się w oknie."""
    return max(100, h.train_days * 1440 // h.bar_minutes // 4)


def test_gielda_przechodzi_prog_pokrycia():
    """Sedno drugiej wady: 521-524 świece przy progu 540."""
    # Zmierzone na produkcji: okno 90 dni, świeca 1h, spółki z uniwersum.
    pokrycie = {"AAPL": 522, "MSFT": 524, "NVDA": 524, "GOOGL": 521,
                "AMZN": 522, "META": 523, "JPM": 521, "XOM": 521}
    prog = prog_pokrycia(pokrycie)
    aktywne = [s for s, n in pokrycie.items() if n >= prog]
    assert len(aktywne) == len(pokrycie), (
        f"próg {prog} przepuścił {len(aktywne)} z {len(pokrycie)} spółek — "
        f"walk-forward equity znów skończy się zerem okien")


def test_stary_prog_NIE_przepuscilby_zadnej_spolki():
    """Pin pod przyczynę — inaczej test wyżej przechodziłby także przed poprawką."""
    pokrycie = {"AAPL": 522, "MSFT": 524, "NVDA": 524, "GOOGL": 521}
    assert all(n < _stary_prog(H1H) for n in pokrycie.values()), (
        f"stary próg {_stary_prog(H1H)} przepuszczał te spółki — liczby "
        f"pokrycia rozjechały się z produkcją i test nic już nie dowodzi")


def test_dla_rynku_ciaglego_prog_zostaje_taki_sam():
    """Poprawka nie może poluzować progu tam, gdzie był poprawny.

    Krypto ma w oknie 90 dni 2 158 świec 1h (zmierzone na produkcji), a w oknie
    180 dni 1 078 świec 4h. Ćwierć z nich to 539 i 269 wobec dawnych 540 i 270 —
    różnica jednej świecy nie przesuwa żadnego instrumentu.
    """
    for h, pokrycie_max in ((H1H, 2158), (H4H, 1078)):
        nowy = prog_pokrycia({f"PARA{i}": pokrycie_max for i in range(10)})
        assert abs(nowy - _stary_prog(h)) <= 1, (
            f"świeca {h.bar}: próg zmienił się z {_stary_prog(h)} na {nowy} — "
            f"poprawka dla giełdy ruszyła kalibrację krypto")


def test_mlody_instrument_dalej_wypada_z_okna():
    """Po to ten próg istnieje: szereg notowany przez ćwierć okna to nie ta
    sama strategia w chudszym okresie."""
    pokrycie = {"AAPL": 522, "MSFT": 524, "DEBIUT": 60}
    prog = prog_pokrycia(pokrycie)
    assert [s for s, n in pokrycie.items() if n >= prog] == ["AAPL", "MSFT"]


def test_podloga_bezwzgledna_zostaje():
    """Okno, w którym NIKT nie ma choć stu świec, nie niesie informacji —
    pokrycie względne byłoby tam równe i przepuściłoby wszystkich."""
    assert prog_pokrycia({"A": 12, "B": 10}) == MIN_SWIEC_OKNA
    assert prog_pokrycia({}) == MIN_SWIEC_OKNA
