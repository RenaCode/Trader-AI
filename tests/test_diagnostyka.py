"""Diagnostyka IC: pominięcie jest wynikiem, a błąd standardowy uwzględnia nakładanie.

DWA PROBLEMY, OBA Z TEJ SAMEJ RODZINY CO RESZTA DNIA

1. Przy zbyt krótkim `--days` odpadały wszystkie instrumenty (próg 500 świec
   panelu), skrypt wypisywał pustą tabelę i kończył się zerem. „Żaden sygnał nic
   nie przewiduje" i „nie było czego liczyć" wyglądały identycznie.

2. Błąd standardowy IC liczony jako 1/√n zakłada niezależne obserwacje. Zwrot
   z k świec liczony przy każdej świecy nakłada się na k−1 sąsiednich pomiarów,
   więc naiwny błąd zaniża się tym mocniej, im dłuższy horyzont — czyli tam,
   gdzie IC zwykle wygląda najlepiej. Pierwszy pomiar mikrostruktury dał przez to
   „+0,1532, trzy i pół raza więcej niż najlepszy sygnał"; po poprawce wyszło
   1,6 błędu standardowego.
"""
import re
from pathlib import Path

ZRODLO = Path("services/engine/diagnose_signals.py").read_text()


def test_brak_danych_konczy_sie_kodem_niezerowym():
    assert "return 2" in ZRODLO, "pusty wynik kończy się sukcesem — nie do odróżnienia od zerowego IC"
    i = ZRODLO.index("if not zebrane:")
    assert "PRZERWANO" in ZRODLO[i:i + 300]


def test_pominiete_instrumenty_sa_wypisywane():
    """Cicha pętla `continue` ukrywała, ile instrumentów w ogóle weszło do pomiaru."""
    assert "pominiete.append" in ZRODLO
    assert "pominięte (panel <" in ZRODLO


def test_blad_standardowy_uwzglednia_nakladanie_obserwacji():
    """Efektywna liczba obserwacji to n/k, nie n."""
    assert re.search(r"n_ef\s*=\s*len\(xs\)\s*/\s*k", ZRODLO), \
        "błąd standardowy nie dzieli liczby obserwacji przez horyzont"


def test_cechy_bez_sygnalu_sa_mierzone_osobno():
    """Mierzenie to nie budowanie: IC cech, których żaden sygnał nie używa,
    odpowiada na pytanie „czy warto ten sygnał w ogóle zbudować"."""
    assert "CECHY_BEZ_SYGNALU" in ZRODLO
    assert "book_imbalance" in ZRODLO and "book_spread_bps" in ZRODLO


def test_prog_panelu_jest_stala_a_nie_liczba_w_warunku():
    """Liczba 500 wpisana w warunek nie da się wyjaśnić w komunikacie błędu."""
    assert "MIN_PANEL = 500" in ZRODLO
    assert "len(panel) < MIN_PANEL" in ZRODLO


# --- test permutacyjny czasu wejscia ----------------------------------------
TIMING = Path("services/engine/diagnose_timing.py").read_text()


def test_test_permutacyjny_zachowuje_wszystko_poza_czasem():
    """Jeśli losowanie zmieniłoby liczbę transakcji, ich długość albo instrument,
    różnica nie pochodziłaby już z momentu wejścia — a to jest całe pytanie."""
    # Wzorzec niesie instrument, swiece wejscia i dlugosc; tryb `czas` losuje
    # WYLACZNIE swiece wejscia (dlugosc i instrument z wzorca).
    assert "wzorce.append((t[\"symbol\"], i0, max(1, i1 - i0)))" in TIMING, \
        "wzorce transakcji nie zachowują instrumentu, wejścia i długości"
    assert "rng.randrange(0, len(seria) - dlugosc - 2)" in TIMING, \
        "losowany jest nie tylko moment wejścia"


def test_test_permutacyjny_liczy_istotnosc_sam():
    """Liczba w rodzaju „43,3" zachęca do wniosku „gorzej niż losowo", który przy
    trzydziestu kilku oknach zwykle nie jest uprawniony.

    Przy braku trafności percentyl okna ma rozkład jednostajny: średnia 50,
    odchylenie 100/√12 ≈ 28,9.
    """
    assert "28.87" in TIMING, "brak odchylenia rozkładu jednostajnego"
    assert "se_sredniej" in TIMING and "odchylenie_licznika" in TIMING
    assert "WERDYKT" in TIMING


def _kod(zrodlo: str) -> str:
    """Same instrukcje: bez komentarzy i bez docstringu modułu.

    Pierwsza wersja testu niżej szukała słowa „hypothesis_trial" w całym pliku
    i trafiała na komentarz wyjaśniający, że tej tabeli NIE dotykamy — czyli
    zgłaszała problem tam, gdzie było jego wyjaśnienie.
    """
    bez_docstringu = zrodlo.split('"""', 2)[-1]
    return "\n".join(l for l in bez_docstringu.splitlines()
                      if not l.strip().startswith("#"))


def test_test_permutacyjny_nie_zapisuje_hipotez():
    """Model zerowy nie jest kandydatem do wyboru — nie może podnosić licznika
    hipotez, bo nic nie optymalizuje."""
    kod = _kod(TIMING)
    for zapis in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert zapis not in kod, f"narzędzie pomiarowe zapisuje do bazy: {zapis}"


def test_kierunek_dlugi_jest_zapisany_wprost():
    """Wyliczanie kierunku ze znaku zwrotu wyglądałoby na obsługę shortów,
    a zwracałoby jedynkę zawsze — na spocie krótkich pozycji nie ma."""
    assert "kierunek" not in _kod(TIMING), \
        "kierunek pozycji nadal jest wyliczany zamiast zapisany wprost"


def test_test_permutacyjny_umie_mierzyc_STRATEGIE_PRODUKCYJNA():
    """22.09.2026: pomiar na DEFAULT_WEIGHTS (wariant bez przewagi) dał percentyl
    ~50 i został odczytany jako „brak timingu" — mówił tylko, że strategia bez
    przewagi nie ma timingu. `--wagi ic` ma iść DOKŁADNIE ścieżką trenera:
    martwe sygnały, bramki z wag bazowych po wyzerowaniu martwych, handel
    wagami ic z okna treningowego. Od 2026-10-03 (audyt Fable F-2.1) bramki
    licza sie na wagach, ktorymi sie handluje, jak w `train.walk_forward`."""
    assert '"--wagi"' in TIMING and '"ic"' in TIMING
    assert "martwe = martwe_sygnaly(cache_tr)" in TIMING
    assert "bazowe = _bez_martwych(DEFAULT_WEIGHTS, martwe)" in TIMING
    assert "ic_weights(trening, cache_tr, martwe, h.ic_horizon_bars)" in TIMING
    # Bramki z wag HANDLOWANYCH i PER REZIM - dokladnie jak w trenerze. Od
    # 2026-09-27 prog liczy sie osobno dla kazdego rezimu (`bramki_okna`);
    # `score_threshold` liczylo jeden prog dla wszystkich i gdyby diagnostyka
    # zostala przy nim, mierzylaby strategie, ktorej produkcja nie wykonuje.
    assert "bramki_okna(" in TIMING and "wagi, prog, h," in TIMING, (
        "bramki z wag handlowanych i per reżim jak w trenerze")
    assert "score_threshold" not in TIMING, (
        "diagnostyka wróciła do jednego progu wspólnego dla reżimów")
    assert "simulate_portfolio(test, wagi," in TIMING


def test_permutacja_DLUGOSCI_zostawia_wejscia_na_miejscu():
    """H2 stratega: przewaga w regule wyjścia, nie w wejściu. Tasowanie
    długości MIĘDZY transakcjami okna przy wejściu w faktycznej świecy —
    ten sam rozkład długości, inny przydział."""
    assert '"--losuj"' in TIMING and '"dlugosc"' in TIMING
    assert "rng.shuffle(dlugosci)" in TIMING
    # Od 2026-10-02 `zwrot_losowej` dostaje swiece DECYZJI i sam wybiera swiece
    # wejscia z trybu egzekucji - patrz tests/test_zwrot_losowej_jak_symulacja.py.
    assert "zwrot_losowej(test[sym], i0, dl, koszt, limit=args.limit)" in TIMING, (
        "wejście ma zostać w swojej świecy (i0 = świeca decyzji, jak `opened_at`)")
