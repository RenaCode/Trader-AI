"""„DSR 0,9524 przy progu 0,95" to nie fakt o strategii, a rzut monetą o oknie.

DLACZEGO TO ISTNIEJE

Zmierzone 2026-09-27 na produkcji, tor 4h, pięć przesunięć okna o **jeden
dzień** (`--days` 1400…1396, `--sucho`, poza tym identyczne nastawy):

    --days  status      DSR    Sharpe   okien dodatnich
      1400  validated  0,9524  +0,1016   27/38 (71%)
      1399  validated  0,9732  +0,1078   28/38 (74%)
      1398  learning   0,9342  +0,0975   31/40 (78%)
      1397  learning   0,9362  +0,0978   28/39 (72%)
      1396  validated  0,9790  +0,1095   29/39 (74%)

Trzy razy `validated`, dwa razy `learning`. Poprzeczka selekcji jest we
wszystkich pięciu **identyczna** (+0,0611), więc nie chodzi o liczbę prób — cała
zmiana siedzi w Sharpe'ie, który chodzi w przedziale 0,0120, czyli sd 0,0056.
To **0,22 błędu standardowego** samego Sharpe'a (1/√1500 = 0,0258). Bramka
rozstrzyga więc różnice pięć razy drobniejsze od szumu własnego pomiaru,
a jej wyjście jest binarne i czyta się jak ustalenie. Przy 1398 wychodzi wręcz
odwrotność: **najlepszy** odsetek okien (78%) i brak promocji.

CZEGO TA ZMIANA NIE ROBI: nie rusza progu i nie zmienia werdyktu. Dokłada
wyłącznie zdanie o tym, ile ten werdykt znaczy — w błędach standardowych, bo
w DSR nikt nie umie ocenić „blisko".

MIARA JEST ZANIŻONA I TAK MA BYĆ CZYTANA: 1/√T to błąd standardowy Sharpe'a
przy zwrotach NIEZALEŻNYCH, a te nie są niezależne (nakładające się pozycje, te
same świece). Prawdziwy błąd jest większy, czyli werdykt jest MNIEJ odporny,
niż mówi ta liczba.
"""
from __future__ import annotations

import random
import statistics
from pathlib import Path

from services.engine.statystyka import (
    PROG_DSR, deflowany_sharpe, sharpe_dla_dsr,
)
from services.engine.train import (
    bramka_dsr, opis_odpornosci_werdyktu, przewaga_dsr, werdykt_dsr,
)

KORZEN = Path(__file__).resolve().parents[1]
TRAIN = (KORZEN / "services" / "engine" / "train.py").read_text(encoding="utf-8")

N_PROB = 53
SD_PROB = 0.0266


def _zwroty(cel: float, n: int = 1500, seed: int = 11) -> list[float]:
    """Próba o ZADANYM Sharpe/transakcję — żeby test mówił o liczbie, którą
    sprawdza, a nie o ziarnie generatora."""
    rng = random.Random(seed)
    surowe = [rng.gauss(0.0, 1.0) for _ in range(n)]
    sr = statistics.fmean(surowe)
    sd = statistics.stdev(surowe)
    return [(x - sr) / sd + cel for x in surowe]


# ============================================ ODWROTNOŚĆ DSR

def test_sharpe_dla_dsr_JEST_odwrotnoscia_deflowanego_sharpe():
    """Sprawdzane PRZEZ PODSTAWIENIE, nie przez drugi rachunek: Sharpe oddany
    przez tę funkcję, wstawiony z powrotem, musi dać DSR równy celowi."""
    z = _zwroty(0.1016)
    cel = sharpe_dla_dsr(z, N_PROB, PROG_DSR, SD_PROB)
    assert cel is not None
    sd = statistics.stdev(z)
    teraz = deflowany_sharpe(z, N_PROB, SD_PROB)["sharpe"]
    przesunieta = [x + (cel - teraz) * sd for x in z]
    wynik = deflowany_sharpe(przesunieta, N_PROB, SD_PROB)["dsr"]
    assert abs(wynik - PROG_DSR) < 1e-4, (
        f"podstawienie oddanego Sharpe'a {cel:.4f} daje DSR {wynik:.6f}, "
        f"a miało dać {PROG_DSR}")


def test_przesuniecie_proby_NIE_RUSZA_momentow_centralnych():
    """Cała poprawność tej metody stoi na tym jednym fakcie: przesunięcie
    zmienia średnią i nic więcej, więc poprzeczka selekcji, skośność, kurtoza
    i T zostają te same."""
    z = _zwroty(0.10)
    a = deflowany_sharpe(z, N_PROB, SD_PROB)
    b = deflowany_sharpe([x + 0.05 for x in z], N_PROB, SD_PROB)
    assert abs(a["skosnosc"] - b["skosnosc"]) < 1e-9
    assert abs(a["kurtoza"] - b["kurtoza"]) < 1e-9
    assert a["prog"] == b["prog"]
    assert a["n_obserwacji"] == b["n_obserwacji"]
    assert b["sharpe"] > a["sharpe"]


def test_wiekszy_cel_wymaga_WIEKSZEGO_Sharpe():
    z = _zwroty(0.10)
    niski = sharpe_dla_dsr(z, N_PROB, 0.50, SD_PROB)
    wysoki = sharpe_dla_dsr(z, N_PROB, 0.99, SD_PROB)
    assert niski is not None and wysoki is not None
    assert wysoki > niski


def test_wieksze_N_wymaga_WIEKSZEGO_Sharpe():
    """Poprzeczka selekcji rośnie z liczbą prób, więc próg DSR staje się
    trudniejszy — gdyby wyszło odwrotnie, znaczyłoby to błąd znaku."""
    z = _zwroty(0.10)
    malo = sharpe_dla_dsr(z, 5, PROG_DSR, SD_PROB)
    duzo = sharpe_dla_dsr(z, 5000, PROG_DSR, SD_PROB)
    assert malo is not None and duzo is not None
    assert duzo > malo


def test_proba_bez_DSR_oddaje_None_a_nie_liczbe():
    assert sharpe_dla_dsr([0.01], N_PROB, PROG_DSR, SD_PROB) is None
    assert sharpe_dla_dsr([0.01] * 500, N_PROB, PROG_DSR, SD_PROB) is None


# ============================================ ZDANIE W WERDYKCIE

def test_werdykt_NA_GRANICY_jest_nazwany_NIEODPORNYM():
    z = _zwroty(0.1016)
    _, opis = werdykt_dsr(z, doplata=0.0, n=N_PROB, trials_kumulatywnie=12653,
                          nieznane=(), odchylenie_prob=SD_PROB,
                          konfiguracji_z_oos=28)
    assert "ODPORNOŚĆ WERDYKTU" in opis
    assert "NIE JEST ODPORNY" in opis, (
        f"werdykt 0,07 błędu standardowego od progu nie jest nazwany "
        f"nieodpornym: {opis}")
    assert "błędu standardowego" in opis
    assert "0,9342" in opis and "0,9790" in opis, (
        "brak zmierzonego rozrzutu pieciu przesuniec - bez niego 'nieodporny' "
        "jest przymiotnikiem, nie pomiarem")


def test_werdykt_DALEKO_od_progu_NIE_dostaje_ostrzezenia():
    z = _zwroty(0.20)
    ok, opis = werdykt_dsr(z, doplata=0.0, n=N_PROB, trials_kumulatywnie=12653,
                           nieznane=(), odchylenie_prob=SD_PROB,
                           konfiguracji_z_oos=28)
    assert ok is True
    assert "ODPORNOŚĆ WERDYKTU" in opis
    assert "NIE JEST ODPORNY" not in opis, (
        "ostrzeżenie doklejane do werdyktu odległego od progu przestaje "
        "cokolwiek znaczyc - to ten sam mechanizm, co alarm swiecacy zawsze")
    assert "zostaje" in opis, "przy werdykcie dodatnim zapas ma być NAZWANY zapasem"


def test_slowo_mowi_KTORA_strone_progu():
    blisko_ponizej = _zwroty(0.1016)
    _, a = werdykt_dsr(blisko_ponizej, doplata=0.0, n=N_PROB,
                       trials_kumulatywnie=1, nieznane=(),
                       odchylenie_prob=SD_PROB, konfiguracji_z_oos=28)
    _, b = werdykt_dsr(_zwroty(0.20), doplata=0.0, n=N_PROB,
                       trials_kumulatywnie=1, nieznane=(),
                       odchylenie_prob=SD_PROB, konfiguracji_z_oos=28)
    assert "brakuje" in a and "zostaje" not in a.split("ODPORNOŚĆ")[1][:120]
    assert "zostaje" in b


def test_brak_proby_MOWI_o_braku_zamiast_milczec():
    """Wolajacy bez proby pod reka ma dostac odleglosc w DSR i informacje, ze
    przeliczenia nie ma - cisza bylaby nieodroznialna od werdyktu odpornego."""
    wynik = przewaga_dsr(_zwroty(0.1016), N_PROB, SD_PROB)
    opis = opis_odpornosci_werdyktu(wynik)
    assert "ODPORNOŚĆ WERDYKTU" in opis
    assert "NIE MA" in opis, f"brak przeliczenia nie jest nazwany: {opis}"


def test_DSR_nieobliczalny_NIE_dostaje_zdania_o_odpornosci():
    """Odległość od progu przy braku DSR nie istnieje. Zdanie o niej byłoby
    liczbą wziętą z niczego."""
    wynik = przewaga_dsr([0.01], N_PROB, SD_PROB)
    assert wynik["dsr"] is None
    assert opis_odpornosci_werdyktu(wynik, [0.01]) == ""


def test_bramka_dostaje_probe_PO_doplacie_a_nie_przed():
    """Odporność liczona na próbie sprzed dopłaty opisywałaby inny werdykt niż
    ten, który stoi obok — a dopłata to właśnie ta wielkość, która o werdykcie
    rozstrzyga (zmierzone: 5,85 pb zbija Sharpe z +0,0952 na +0,0823)."""
    assert "ok, opis = bramka_dsr(wynik, po)" in TRAIN, (
        "bramka nie dostaje próby po dopłacie — odporność opisuje inną próbę "
        "niż DSR obok niej")


def test_odpornosc_NIE_ZMIENIA_werdyktu():
    """Zdanie jest informacją, nie bramką. Gdyby próg się przesunął, ten test
    pada — i ma paść, bo to byłaby zmiana reguły promocji przemycona w opisie."""
    for cel in (0.05, 0.1016, 0.20):
        z = _zwroty(cel)
        wynik = przewaga_dsr(z, N_PROB, SD_PROB)
        ok_z_opisem, _ = bramka_dsr(wynik, z)
        ok_bez, _ = bramka_dsr(wynik)
        assert ok_z_opisem == ok_bez == (wynik["dsr"] >= PROG_DSR), (
            f"werdykt zależy od tego, czy policzono odporność (Sharpe {cel})")


# ===================================== PIEC PRZESUNIEC -> JEDEN STATUS (T4)
#
# Tabela z naglowka tego pliku: 3 x `validated`, 2 x `learning` przy tych
# samych nastawach. `train.werdykt_stabilny` sklada piec przesuniec w jeden
# status, ktory zmienia sie tylko, gdy przekroczy prog MINIMUM.

def _st(status: str, dsr: float, pct: float = 72.0):
    from services.engine.train import Status
    return Status(status, "", "", 40, int(pct * 0.4), pct, 0.1, 0.5, 0.8, 0.5, 0.7, dsr)


def test_piec_przesuniec_z_tabeli_27_09_daje_JEDEN_status_learning():
    from services.engine.train import werdykt_stabilny
    statusy = {-2: _st("validated", 0.9524), -1: _st("validated", 0.9732),
               0: _st("learning", 0.9342), 1: _st("learning", 0.9362),
               2: _st("validated", 0.9790)}
    status, zdanie = werdykt_stabilny(statusy)
    assert status == "learning"
    assert "validated 3/5" in zdanie and "learning 2/5" in zdanie
    assert "DSR min/śr/max 0.9342/" in zdanie and "/0.9790" in zdanie


def test_validated_tylko_gdy_minimum_przechodzi():
    from services.engine.train import werdykt_stabilny
    assert werdykt_stabilny({p: _st("validated", 0.96) for p in range(-2, 3)})[0] == "validated"
    prawie = {p: _st("validated", 0.99) for p in range(-2, 3)} | {1: _st("learning", 0.949)}
    assert werdykt_stabilny(prawie)[0] == "learning"


def test_degraded_tylko_gdy_degraded_wszedzie():
    from services.engine.train import werdykt_stabilny
    assert werdykt_stabilny({p: _st("degraded", 0.0, 30) for p in range(-2, 3)})[0] == "degraded"
    mieszane = {p: _st("degraded", 0.0, 30) for p in range(-2, 3)} | {0: _st("learning", 0.1, 45)}
    assert werdykt_stabilny(mieszane)[0] == "learning"


def test_jedno_przesuniecie_to_status_bez_zdania():
    from services.engine.train import werdykt_stabilny
    assert werdykt_stabilny({0: _st("validated", 0.97)}) == ("validated", "")
