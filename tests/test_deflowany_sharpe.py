"""Deflowany Sharpe (DSR) i bramka promocji, ktora go NIE MIALA.

CO BYLO ZLE — I NIE OBJAWIALO SIE NICZYM

Bramka promocji modelu (`train.py`) przepuszczala na `status='validated'`, gdy

    pct >= 70 and median > 0.05 and bije_odniesienie

gdzie `median` to mediana Sharpe'ow okien. Ten warunek nie uwzglednial ANI
liczby przetestowanych konfiguracji, ANI rozkladu zwrotow. Licznik konfiguracji
istnial od migracji 0006 wylacznie po to, zeby deflowany Sharpe dalo sie
policzyc — komentarz przy tabeli mowi to wprost — i nie zostal policzony ani
razu; szukanie slowa "dsr" albo "deflated" w kodzie znajdowalo same komentarze
o tym, ze go brakuje.

Skutek jest arytmetyczny, nie hipotetyczny. Przy zlaczonej probie 1432
transakcji i Sharpe +0,0952:

    prob   poprzeczka selekcji   DSR
       1              +0,0000   0,9998   (nie bylo z czego wybierac)
      17              +0,0483   0,9616   przechodzi prog 0,95
     131              +0,0694   0,8352   NIE przechodzi
   8 700              +0,1012   0,4111   NIE przechodzi
  11 460              +0,1029   0,3852   NIE przechodzi

Liczba prob to nie szczegol rachunkowy: to rozdzial miedzy „wynik" a „najlepszy
z jedenastu tysiecy losowan". 11 460 to liczba, ktora ten projekt zanotowal
w `docs/03-wynik-walk-forward.md` (8 700 to liczba z sekcji „piata iteracja"
tego samego dokumentu).

CZEGO TE TESTY PILNUJA

  1. wzoru — na wartosciach policzalnych na piechote, wypisanych w docstringach;
  2. KIERUNKU kazdej poprawki: wiecej prob, ujemna skosnosc i grubszy ogon MUSZA
     obnizac DSR, nigdy podnosic;
  3. fail-closed w bramce: `dsr = None` (nieobliczalny) NIE MOZE dawac
     `validated`. Brak wiedzy wstrzymuje promocje, a nie przepuszcza jej.
"""
from __future__ import annotations

import math
import random
import re
from pathlib import Path

import pytest

from services.engine.statystyka import (
    PROG_DSR, deflowany_sharpe, kurtoza, oczekiwany_maksymalny_sharpe, skosnosc,
)
from services.engine.koszty import karta_rundy
from services.engine.train import (
    MIN_WINDOWS_FOR_VALIDATION, bramka_dsr, n_konfiguracji, przewaga_dsr,
    status_walidacji, werdykt_dsr,
)


def _zwroty_o_sharpe(cel: float, n: int, seed: int = 20260926) -> list[float]:
    """Proba o ZADANYM Sharpe na transakcje — zeby testy mowily o liczbie,
    ktora chca sprawdzic, a nie o przypadkowym ziarnie generatora."""
    rng = random.Random(seed)
    surowe = [rng.gauss(0.0, 1.0) for _ in range(n)]
    srednia = sum(surowe) / n
    wysrodkowane = [x - srednia for x in surowe]
    sd = math.sqrt(sum(x * x for x in wysrodkowane) / (n - 1))
    return [x / sd + cel for x in wysrodkowane]


# ======================================================== momenty na piechote

def test_skosnosc_proby_symetrycznej_to_ZERO():
    """[1,2,3,4,5]: odchylenia -2,-1,0,1,2, wiec m3 = 0 i skosnosc = 0."""
    assert skosnosc([1, 2, 3, 4, 5]) == 0.0


def test_kurtoza_NIE_JEST_NADWYZKOWA_i_da_sie_ja_policzyc_recznie():
    """[1,2,3,4,5]: m2 = 10/5 = 2, m4 = 34/5 = 6,8, wiec kurtoza = 6,8/4 = 1,7.

    Gdyby funkcja zwracala NADWYZKE nad rozkladem normalnym (jak scipy), byloby
    tu 1,7 - 3 = -1,3. Roznica nie jest kosmetyczna: czlon `(g4-1)/4` we wzorze
    na PSR zmienia wtedy znak i poprawka zaczyna dzialac na KORZYSC strategii.
    """
    assert kurtoza([1, 2, 3, 4, 5]) == pytest.approx(1.7)


def test_skosnosc_i_kurtoza_proby_skosnej_maja_wartosci_zamkniete():
    """[0,0,0,4]: srednia 1, odchylenia -1,-1,-1,3.

        m2 = (1+1+1+9)/4 = 3
        m3 = (-1-1-1+27)/4 = 6      -> skosnosc = 6 / 3^1,5 = 2/sqrt(3)
        m4 = (1+1+1+81)/4 = 21      -> kurtoza  = 21 / 9 = 7/3
    """
    assert skosnosc([0, 0, 0, 4]) == pytest.approx(2 / math.sqrt(3))
    assert kurtoza([0, 0, 0, 4]) == pytest.approx(7 / 3)


def test_proba_stala_i_jednoelementowa_oddaje_BRAK_a_nie_zero():
    """Ta sama zasada, co w `sharpe`: brak pomiaru musi wygladac inaczej niz pomiar."""
    for xs in ([], [1.0], [2.0, 2.0, 2.0]):
        assert skosnosc(xs) is None and kurtoza(xs) is None


# ============================================ poprzeczka na wielokrotne proby

def test_jedna_proba_to_ZERO_poprzeczki_bo_nie_bylo_z_czego_wybierac():
    """Wzor sam by tu nie przeszedl: F^-1(1 - 1/1) = F^-1(0) nie istnieje."""
    assert oczekiwany_maksymalny_sharpe(1, 0.25) == 0.0


def test_poprzeczka_dla_dwoch_prob_wychodzi_ze_wzoru():
    """N=2: F^-1(1 - 1/2) = F^-1(0,5) = 0, wiec zostaje sam drugi czlon:

        E[max] = sd * g * F^-1(1 - 1/(2e)) = 1 * 0,5772157 * 0,9004526
               = 0,5197553
    """
    assert oczekiwany_maksymalny_sharpe(2, 1.0) == pytest.approx(0.5197553, abs=1e-7)


def test_poprzeczka_ROSNIE_z_liczba_prob():
    """To jest cala tresc poprawki: najlepszy z wiekszej liczby losowan wychodzi
    wyzej sam z siebie, wiec poprzeczka musi rosnac razem z N."""
    wartosci = [oczekiwany_maksymalny_sharpe(n, 0.1)
                for n in (2, 17, 131, 1000, 11460)]
    assert all(a < b for a, b in zip(wartosci, wartosci[1:])), wartosci


def test_poprzeczka_jest_LINIOWA_w_rozrzucie_prob():
    assert (oczekiwany_maksymalny_sharpe(131, 0.2)
            == pytest.approx(2 * oczekiwany_maksymalny_sharpe(131, 0.1)))


def test_brak_rozrzutu_prob_to_BRAK_poprzeczki_a_nie_zerowa_poprzeczka():
    """Zero podstawione w to miejsce zdejmuje CALA poprawke na wielokrotne
    testowanie i robi to po cichu — czyli w strone latwiejszej bramki."""
    assert oczekiwany_maksymalny_sharpe(131, 0.0) is None
    assert oczekiwany_maksymalny_sharpe(131, -1.0) is None
    assert oczekiwany_maksymalny_sharpe(0, 0.1) is None


# ================================================= DSR policzony na piechote

def test_DSR_na_probie_policzalnej_recznie():
    """zwroty = [-1, 0, 1, 2, 3], JEDNA proba (brak poprawki na selekcje).

        srednia = 1,  odchylenie PROBKOWE = sqrt(10/4) = 1,5811388
        SR      = 1 / 1,5811388 = 0,6324555,   SR^2 = 0,4 dokladnie
        skosnosc = 0 (proba symetryczna),  kurtoza = 1,7
        mianownik = sqrt(1 - 0*SR + (1,7-1)/4 * 0,4) = sqrt(1,07) = 1,0344080
        z = (0,6324555 - 0) * sqrt(5-1) / 1,0344080 = 1,2228357
        DSR = F(1,2228357) = 0,8893041
    """
    w = deflowany_sharpe([-1.0, 0.0, 1.0, 2.0, 3.0], 1)
    assert w["sharpe"] == pytest.approx(0.6324555, abs=1e-7)
    assert w["skosnosc"] == 0.0
    assert w["kurtoza"] == pytest.approx(1.7)
    assert w["prog"] == 0.0
    assert w["dsr"] == pytest.approx(0.8893041, abs=1e-7)
    assert w["powod"] == ""


def test_DSR_SPADA_gdy_prob_jest_wiecej_przy_TYCH_SAMYCH_zwrotach():
    """Sedno miary. Ten sam wynik przy 17 i przy 11 460 probach to dwa rozne
    wyniki, a stara bramka nie odrozniala ich niczym."""
    zwroty = _zwroty_o_sharpe(0.0952, n=1432)
    dsry = [deflowany_sharpe(zwroty, n)["dsr"] for n in (1, 17, 131, 11460)]
    assert all(a > b for a, b in zip(dsry, dsry[1:])), dsry


def test_UJEMNA_skosnosc_obniza_DSR_a_dodatnia_podnosi():
    """Kierunek, nie wielkosc. Strategia z krotkim ogonem zyskow i dlugim strat
    ma skosnosc ujemna — i to musi kosztowac, a nie byc niewidoczne."""
    baza = _zwroty_o_sharpe(0.12, n=800)
    ujemna = baza[:-1] + [baza[-1] - 8 * abs(baza[-1]) - 0.5]
    dodatnia = baza[:-1] + [baza[-1] + 8 * abs(baza[-1]) + 0.5]
    assert skosnosc(ujemna) < 0 < skosnosc(dodatnia)
    d_u = deflowany_sharpe(ujemna, 131)
    d_d = deflowany_sharpe(dodatnia, 131)
    assert d_u["dsr"] < d_d["dsr"], (d_u, d_d)


def test_pominiecie_kurtozy_i_skosnosci_NIE_jest_neutralne():
    """Gdyby wzor zakladal normalnosc, mianownik bylby sqrt(1 + SR^2/2). Test
    pokazuje, ze na realnym rozkladzie wychodzi INNA liczba — czyli ze te dwa
    momenty faktycznie doszly do miary, a nie sa ozdoba w docstringu."""
    zwroty = _zwroty_o_sharpe(0.12, n=600)
    zwroty = zwroty[:-1] + [zwroty[-1] - 12.0]          # gruby ogon strat
    w = deflowany_sharpe(zwroty, 40)
    sr = w["sharpe"]
    mian_normalny = math.sqrt(1 + sr * sr / 2)
    mian_realny = math.sqrt(1 - w["skosnosc"] * sr + (w["kurtoza"] - 1) / 4 * sr * sr)
    assert mian_realny > mian_normalny * 1.01, (mian_realny, mian_normalny)


def test_za_krotka_proba_i_zerowy_licznik_prob_oddaja_BRAK_z_POWODEM():
    for zwroty, n_prob, fragment in (
            ([0.01], 40, "mniej niż dwie"),
            ([0.01, 0.01, 0.01], 40, "zerowy rozrzut"),
            ([0.01, -0.02, 0.03], 0, "licznik"),
    ):
        w = deflowany_sharpe(zwroty, n_prob)
        assert w["dsr"] is None and fragment in w["powod"], (zwroty, n_prob, w)


def test_DSR_jest_prawdopodobienstwem_czyli_miesci_sie_w_zero_jeden():
    for sr in (-0.5, -0.05, 0.0, 0.05, 0.5):
        for n in (1, 40, 11460):
            d = deflowany_sharpe(_zwroty_o_sharpe(sr, n=500), n)["dsr"]
            assert 0.0 <= d <= 1.0, (sr, n, d)


# ================================================ bramka: FAIL-CLOSED i opis

def test_bramka_WSTRZYMUJE_promocje_gdy_DSR_jest_NIEOBLICZALNY():
    """Jedyny kierunek, w ktorym brak wiedzy wolno interpretowac."""
    for zwroty, n_prob in (([], 40), ([0.01], 40), ([0.01, -0.01], 0)):
        ok, opis = bramka_dsr(przewaga_dsr(zwroty, n_prob))
        assert ok is False, (zwroty, n_prob, opis)
        assert "NIEOBLICZALNY" in opis and "wstrzymana" in opis


def test_bramka_przepuszcza_wynik_MOCNY_przy_MALEJ_liczbie_prob():
    """Kanarek na druga strone: bramka, ktorej nie da sie przejsc nigdy, nie
    jest pomiarem, tylko wylacznikiem."""
    ok, opis = bramka_dsr(przewaga_dsr(_zwroty_o_sharpe(0.20, n=1500), 40))
    assert ok is True, opis


def test_ten_sam_wynik_przy_DUZEJ_liczbie_prob_JUZ_nie_przechodzi():
    """Sharpe +0,12 na 1500 transakcjach: DSR 0,9929 przy 40 probach i 0,7734
    przy 11 460. Te same transakcje, ten sam Sharpe, przeciwne werdykty — i to
    jest cala roznica, ktorej stara bramka nie widziala."""
    zwroty = _zwroty_o_sharpe(0.12, n=1500)
    assert bramka_dsr(przewaga_dsr(zwroty, 40))[0] is True
    assert bramka_dsr(przewaga_dsr(zwroty, 131))[0] is True
    assert bramka_dsr(przewaga_dsr(zwroty, 11460))[0] is False


def test_opis_podaje_DSR_RAZEM_z_N_i_liczba_prob():
    """Sam DSR jest nieczytelny — ta sama liczba przy 17 i przy 11 460 probach
    opisuje dwa rozne wyniki. To ta sama zasada, ktora kaze podawac przedzial
    ufnosci razem z odsetkiem dodatnich okien."""
    _, opis = bramka_dsr(przewaga_dsr(_zwroty_o_sharpe(0.12, n=1432), 131))
    assert "1432 transakcji" in opis
    assert "131 przetestowanych konfiguracji" in opis
    assert "poprzeczka selekcji" in opis
    assert "skośność" in opis and "kurtoza" in opis
    assert f"{PROG_DSR:.2f}" in opis


# =============================================== bramka promocji jako calosc

DOBRE = dict(total=40, positive=30, median=0.117, bije_odniesienie=True, udzial_b2=0.7)


def test_wszystkie_warunki_spelnione_daje_validated():
    assert status_walidacji(**DOBRE, dsr_ok=True) == "validated"


def test_SAM_DSR_odbiera_validated_gdy_reszta_jest_spelniona():
    """Sedno zmiany. Przed nia ten przypadek dawal `validated` — czyli zielone
    swiatlo dla wyniku, o ktorym nie wiadomo, z ilu prob jest maksimum."""
    assert status_walidacji(**DOBRE, dsr_ok=False) == "learning"


def test_bramka_moze_byc_tylko_TRUDNIEJSZA_kazdy_warunek_osobno():
    """Zaden z pozostalych warunkow nie moze byc przez DSR zniesiony."""
    assert status_walidacji(total=40, positive=27, median=0.117,
                            bije_odniesienie=True, dsr_ok=True) == "learning"
    assert status_walidacji(total=40, positive=30, median=0.04,
                            bije_odniesienie=True, dsr_ok=True) == "learning"
    assert status_walidacji(total=40, positive=30, median=0.117,
                            bije_odniesienie=False, dsr_ok=True) == "learning"
    assert status_walidacji(total=MIN_WINDOWS_FOR_VALIDATION - 1, positive=19,
                            median=0.117, bije_odniesienie=True,
                            dsr_ok=True) == "learning"


def test_degraded_zostaje_degraded_niezaleznie_od_DSR():
    for dsr_ok in (False, True):
        assert status_walidacji(total=40, positive=10, median=-0.05,
                               bije_odniesienie=False, dsr_ok=dsr_ok) == "degraded"


def test_brak_mediany_nie_jest_zerem_i_nie_promuje():
    assert status_walidacji(total=40, positive=30, median=None,
                            bije_odniesienie=True, dsr_ok=True) == "learning"


# ============================== CO NAPRAWDE LICZY learn.hypothesis_trial
#
# DSR zalezy od liczby prob przez `F^-1(1 - 1/N)`, wiec roznica miedzy „jedna
# proba na konfiguracje" a „jedna na przebieg" nie jest niuansem nazewniczym:
# przy 1432 transakcjach i Sharpe +0,0952 przejscie z N=17 na N=131 zmienia DSR
# z 0,9616 (przechodzi) na 0,8352 (nie przechodzi). Dlatego te testy pilnuja
# SEMANTYKI licznika w zrodle, a nie tylko jego wartosci.

ZRODLO_TRENERA = (Path(__file__).resolve().parents[1]
                  / "services/engine/train.py").read_text(encoding="utf-8")
MIGRACJA_0006 = (Path(__file__).resolve().parents[1]
                 / "db/migrations/0006_learning.sql").read_text(encoding="utf-8")
ZRODLO_PURGE = (Path(__file__).resolve().parents[1]
                / "services/ingest/purge_demo.py").read_text(encoding="utf-8")


def test_licznik_jest_liczba_ROZNYCH_konfiguracji_a_nie_przebiegow():
    """Dowod z definicji tabeli, nie z domyslu.

    `UNIQUE (model_id, config_hash)` + INSERT `ON CONFLICT DO NOTHING` znaczy,
    ze `count(*)` liczy ROZNE wektory wag. Dwa przebiegi TEJ SAMEJ konfiguracji
    dokladaja tylko te wektory, ktorych jeszcze nie bylo — wiec licznik NIE
    jest liczba przebiegow i nie jest liczba prob u dostawcy statystyki.
    """
    assert re.search(r"CREATE TABLE learn\.hypothesis_trial.*?UNIQUE \(model_id, config_hash\)",
                     MIGRACJA_0006, re.S), (
        "tabela nie ma UNIQUE (model_id, config_hash) — licznik znaczy wtedy coś "
        "innego niż to, co opisuje `przewaga_dsr`, a DSR zależy od tego liniowo "
        "przez log(N)")
    assert "ON CONFLICT (model_id, config_hash) DO NOTHING" in ZRODLO_TRENERA


def test_tabela_NIE_WIE_z_ktorego_przebiegu_jest_wiersz():
    """Dlaczego „124 przebiegi to 1 czy 124 proby" nie da sie rozstrzygnac z tej
    tabeli: nie ma w niej `training_run_id`. Najblizsze przyblizenie to
    `created_at`, a to jest data, nie identyfikator przebiegu."""
    ddl = re.search(r"CREATE TABLE learn\.hypothesis_trial \((.*?)\);",
                    MIGRACJA_0006, re.S).group(1)
    assert "training_run_id" not in ddl, (
        "jeśli kolumna doszła, `przewaga_dsr` może liczyć próby per przebieg — "
        "i wtedy ten test ma zniknąć razem z tamtym komentarzem")
    assert "created_at" in ddl


def test_kolumna_oos_sharpe_jest_MARTWA_i_dlatego_V_nie_jest_mierzalne():
    """To nie jest czepianie sie nieuzywanej kolumny.

    `deflowany_sharpe` potrzebuje ROZRZUTU Sharpe'ow miedzy probami (V[{SR_n}]
    u Baileya). Jedynym miejscem, z ktorego dalo by sie go odczytac, jest ta
    kolumna — i nikt jej nie wypelnia, wiec funkcja podstawia wariancje samego
    estymatora, co jest zalozeniem dzialajacym NA KORZYSC strategii. Ten test
    pilnuje, zeby to zdanie w docstringu pozostalo prawdziwe: gdy ktos zacznie
    `oos_sharpe` zapisywac, test padnie i kaze przejsc na pomiar.
    """
    assert "oos_sharpe" in MIGRACJA_0006
    wstawki = re.findall(r"INSERT INTO learn\.hypothesis_trial[^\"']*", ZRODLO_TRENERA)
    assert wstawki, "nie znaleziono INSERT-a do learn.hypothesis_trial"
    for w in wstawki:
        assert "oos_sharpe" not in w, (
            "`oos_sharpe` jest już zapisywane — `deflowany_sharpe` może wtedy "
            "dostać ZMIERZONE `odchylenie_prob` zamiast przybliżenia, a docstring "
            "o „drodze optymistycznej” trzeba poprawić")


def test_licznik_prob_NIE_POMIJA_zalozen_poczatkowych():
    """Wada znaleziona przy tej zmianie i naprawiona w tym samym commicie.

    W wariancie `argmax` `bazowe` byly OCENIANE (`ocen(bazowe, ...)`) i braly
    udzial w wyborze, ale jako jedyny kandydat NIE trafialy do licznika. Licznik
    ZANIZAL wiec liczbe prob, a zanizony licznik obniza poprzeczke selekcji,
    czyli PODNOSI DSR — bramka stawala sie latwiejsza, bez zadnego objawu.
    """
    galaz = ZRODLO_TRENERA[ZRODLO_TRENERA.index("if wybor == \"bazowe\":"):
                           ZRODLO_TRENERA.index("all_sharpes.append")]
    for kandydat in ("bazowe", "ic_w"):
        assert f"zapisz_hipoteze({kandydat})" in galaz, (
            f"kandydat `{kandydat}` jest oceniany, a nie jest liczony jako próba")
    # Kazde `ocen(bazowe` ma gdzies obok `zapisz_hipoteze(bazowe)`.
    assert galaz.count("ocen(bazowe") <= galaz.count("zapisz_hipoteze(bazowe)")


def test_tryb_suchy_NIE_dokłada_prob_i_to_jest_zamierzone():
    """`--sucho` nie dotyka bazy, wiec porownania wariantow sa probami
    NIEPOLICZONYMI. Licznik jest przez to dolnym oszacowaniem — kierunek
    niekorzystny, wiec musi byc widoczny w kodzie, a nie tylko w glowie."""
    assert re.search(r"def zapisz_hipoteze\(cand\):\s*\n\s*if sucho:\s*\n\s*return",
                     ZRODLO_TRENERA)


def test_czyszczenie_demo_ZERUJE_licznik_prob():
    """Druga przyczyna, dla ktorej licznik jest dolnym oszacowaniem: `purge_demo`
    robi `TRUNCATE` na tej tabeli i ustawia `trials_run=0`. Konfiguracje zostaly
    przetestowane, a DSR po czyszczeniu liczy sie tak, jakby nie byly."""
    assert "learn.hypothesis_trial" in ZRODLO_PURGE
    assert "trials_run=0" in ZRODLO_PURGE


# ======================================= czy DSR faktycznie dochodzi do bramki

def test_bramka_w_trenerze_JEST_podlaczona_do_DSR():
    """Kanarek na rozjazd „funkcja istnieje, ale nikt jej nie wola".

    Tak wygladal caly ten blad: `trials` bylo liczone, zapisywane i wystawiane
    przez API, a do bramki nie wchodzilo w niczym.
    """
    assert "dsr_ok, opis_dsr = werdykt_dsr(" in ZRODLO_TRENERA
    assert "wszystkie_zwroty, doplata=karta.doplata_poza_symulacja" in ZRODLO_TRENERA
    assert ("status = status_walidacji(total, positive, median, "
            "bije_odniesienie, dsr_ok,\n                                  udzial_b2)") in ZRODLO_TRENERA


def test_stary_warunek_bramki_JUZ_NIE_ISTNIEJE_w_zrodle():
    """Gdyby gdziekolwiek zostalo drugie, rownolegle wyliczenie `validated`,
    bramka mialaby obejscie — a ten projekt naprawial rozjazd „trening kontra
    produkcja" juz trzy razy i za kazdym razem byl to drugi egzemplarz tej
    samej reguly."""
    assert '"validated" if pct >= 70' not in ZRODLO_TRENERA
    assert ZRODLO_TRENERA.count('= status_walidacji(') == 1


# ============================ czy KOSZT dochodzi do DSR, a nie tylko do Sharpe
#
# Blad, ktory kosztowal ten projekt tydzien, mial dokladnie ten kształt:
# prowizja byla odejmowana od krzywej kapitalu, a nie od zwrotu z transakcji,
# a Sharpe liczy sie z tego drugiego. Miara nie reagowala na parametr kosztu
# i nikt tego nie widzial, bo nic sie nie wywracalo. DSR stoi na tej samej
# probie, wiec pilnujemy tego samego.

def test_doplata_kosztowa_OBNIZA_DSR_a_nie_zostawia_go_bez_zmian():
    """Liczby z realnego rachunku tego projektu, odtworzone na probie o znanym
    odchyleniu 4,5% na transakcje:

        Sharpe przed dopłatą         = 0,0952
        zmierzony spread             = 5,85 pb  = 0,000585
        spadek Sharpe                = 0,000585 / 0,045 = 0,0130
        Sharpe po dopłacie           = 0,0822

    Przy 1432 transakcjach i 17 probach DSR spada wtedy z 0,9616 do ~0,90,
    czyli PRZESTAJE przechodzic prog 0,95. Sam Sharpe zmienil sie o 1,3 punktu
    trzeciego miejsca po przecinku — werdykt bramki zmienil sie calkowicie.
    """
    odchylenie = 0.045
    zwroty = [x * odchylenie for x in _zwroty_o_sharpe(0.0952, n=1432)]
    doplata = 5.85e-4
    po = [r - doplata for r in zwroty]

    przed = deflowany_sharpe(zwroty, 17)
    potem = deflowany_sharpe(po, 17)

    assert przed["sharpe"] == pytest.approx(0.0952, abs=1e-4)
    assert potem["sharpe"] == pytest.approx(0.0822, abs=1e-3)
    assert potem["dsr"] < przed["dsr"], (
        "dopłata kosztowa nie zmieniła DSR — koszt nie dochodzi do miary")
    assert przed["dsr"] > PROG_DSR > potem["dsr"], (
        f"zmierzony spread miał przestawić werdykt bramki: "
        f"{przed['dsr']:.4f} -> {potem['dsr']:.4f} przy progu {PROG_DSR}")


def test_stala_doplata_NIE_rusza_odchylenia_ani_momentow():
    """Kontrola rachunku, nie strategii: koszt staly przesuwa SREDNIA i zostawia
    rozrzut, skosnosc i kurtoze nietknietymi. Gdyby ruszyl ktorekolwiek z nich,
    bylby to blad rachunku udajacy koszt."""
    zwroty = [x * 0.045 for x in _zwroty_o_sharpe(0.0952, n=600)]
    po = [r - 5.85e-4 for r in zwroty]
    assert skosnosc(po) == pytest.approx(skosnosc(zwroty), abs=1e-12)
    assert kurtoza(po) == pytest.approx(kurtoza(zwroty), abs=1e-12)


def test_zlaczona_proba_DSR_pochodzi_z_tej_samej_symulacji_co_koszt():
    """Zeby `--kind spot` i `--kind spot_eu` (taker 0,100% wobec 0,250%) dawaly
    ROZNY DSR, proba musi pochodzic z `simulate_portfolio(..., kind=kind)`.
    Test pilnuje tego lancucha w zrodle — sama funkcja DSR nie ma jak sprawdzic,
    czym ja nakarmiono."""
    # `oos_brutto`, nie `oos_res` (od 2026-10-02): `oos_res` jest juz po
    # doplacie z karty, a `werdykt_dsr` odejmuje ja SAM - proba DSR musi wiec
    # byc brutto, inaczej doplata zostalaby odjeta dwa razy.
    assert "wszystkie_zwroty.extend(zwroty_okna)" in ZRODLO_TRENERA
    assert "zwroty_okna = [t[\"ret\"] for t in oos_brutto.trades]" in ZRODLO_TRENERA
    oos = ZRODLO_TRENERA[ZRODLO_TRENERA.index("oos_brutto = simulate_portfolio("):]
    assert "kind=kind" in oos[:400], (
        "symulacja OOS nie dostaje `kind`, więc stawka prowizji nie dochodzi "
        "do próby, z której liczy się DSR")


# ===================== SKAD BIERZE SIE N — I DLACZEGO NIE Z hypothesis_trial
#
# Zmierzone na produkcji 2026-09-26 (ssh renacode, psql, tylko SELECT):
#
#   learn.model_version, tor crypto_signal_weighting_4h:
#       143 wersje | 57 roznych `params` | 2026-08-27 .. 2026-09-26
#   learn.model_version, calosc: 180 wersji | 86 roznych `params`
#   learn.hypothesis_trial, tor 4h: 12 613 | `oos_sharpe` wypelnione: 0
#
# Licznik hipotez NIE jest N, i nie dlatego, ze rosnie kalendarzem — 143
# przebiegi po najwyzej 40 okien daja sufit 5 720 wierszy z wariantu `ic`,
# wiec co najmniej 6 893 z 12 613 pochodzi z realnego przeszukiwania `argmax`.
# Powod jest mocniejszy: kandydaci `argmax` byli oceniani IN-SAMPLE
# (`ocen(..., train_slices, ...)`), a raportowany Sharpe pochodzi z okna
# testowego, ktorego selekcja nie widziala. Walk-forward juz za nich zaplacil.
# Doliczenie ich do poprzeczki deflacji liczy te sama kare dwa razy.
#
# N to liczba ROZROZNIALNYCH KONFIGURACJI, ktorych wynik OOS ktos zobaczyl —
# czyli `count(DISTINCT params)` po `learn.model_version`.


class KursorKonfiguracji:
    """Kursor oddajacy podlozona liczbe i zapamietujacy SQL.

    Atrapa oddaje tylko to, o co zapytano — wiec zapytanie o inna tabele niz
    `model_version` nie dostanie tu nic i test padnie, tak jak padlby pomiar.
    """

    def __init__(self, ile: int):
        self.ile, self.sql = ile, ""

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        if "learn.model_version" in self.sql and "DISTINCT params" in self.sql:
            return {"n": self.ile}
        return {}


def test_N_liczy_ROZNE_params_z_model_version():
    kur = KursorKonfiguracji(57)
    assert n_konfiguracji(kur, 1) == 57
    assert "count(DISTINCT params)" in kur.sql, (
        "N liczone bez DISTINCT to liczba PRZEBIEGOW, nie konfiguracji — "
        "tor 4h ma 143 wersje i 57 roznych params")
    assert "learn.model_version" in kur.sql
    assert "hypothesis_trial" not in kur.sql
    # ATRAPA ODDAJE PODLOZONA LICZBE NIEZALEZNIE OD FILTRU, wiec ten test
    # przechodzil przez caly czas, gdy N liczylo sie ze zlej populacji (takze
    # z wersji, ktore zostaly w `training`). Populacje sprawdza na PRAWDZIWEJ
    # bazie `test_n_selekcji_tylko_przebiegi_z_wynikiem`; tutaj zostaje sam
    # wymog, zeby filtr w SQL byl - inaczej nie ma czego sprawdzac.
    assert "status IN ('candidate','active','retired')" in kur.sql, (
        "zapytanie o N nie filtruje statusow — licza sie wersje, ktore nigdy "
        "nie doszly do wyniku")


def test_zmierzone_N_57_NIE_PRZEPUSZCZA_wyniku_ktory_przy_17_przechodzil():
    """Sedno pomiaru. Przy N=17 (zla wielkosc: liczba wersji w bazie
    deweloperskiej) DSR przechodzil prog; przy zmierzonych 57 nie przechodzi
    i nie jest to przypadek na granicy."""
    zwroty = _zwroty_o_sharpe(0.0952, n=1462)
    assert przewaga_dsr(zwroty, 17)["dsr"] > PROG_DSR
    assert przewaga_dsr(zwroty, 57)["dsr"] < PROG_DSR
    assert przewaga_dsr(zwroty, 86)["dsr"] < przewaga_dsr(zwroty, 57)["dsr"]


def test_params_zapisuja_kind_i_limit_bo_bez_nich_N_jest_zanizone():
    """Dwa przebiegi roznce sie WYLACZNIE cennikiem (`--kind spot` taker 0,100%
    wobec `spot_eu` 0,250%) mialy identyczne `params` i liczyly sie jako jedna
    konfiguracja. Zanizone N podnosi DSR, czyli ulatwia bramke."""
    wstawka = re.search(r"INSERT INTO learn\.model_version.*?sha_kodu\(\)\)\)",
                        ZRODLO_TRENERA, re.S)
    assert wstawka, "nie znaleziono zapisu wersji modelu"
    # Od 2026-10-03 `params` sklada sie przed INSERT-em (`params_przebiegu`) -
    # ten sam slownik liczy N przebiegu z pliku panelu - i INSERT go zapisuje.
    assert "json.dumps(params_przebiegu)" in wstawka.group(0)
    slownik = re.search(r"params_przebiegu = \{.*?\}\n", ZRODLO_TRENERA, re.S)
    assert slownik, "nie znaleziono `params_przebiegu`"
    for klucz in ('"kind": kind', '"limit": bool('):
        assert klucz in slownik.group(0), (
            f"`params` nie zapisuje {klucz} — dwa różne cenniki zlewają się "
            f"w jedną konfigurację i N jest zaniżone")


# ================================= KANARKI NA NOWE ZACHOWANIE BRAMKI

def _sprawa(sharpe_docelowy: float, n_transakcji: int = 1462,
            odchylenie: float = 0.045) -> list[float]:
    """Proba o zadanym Sharpe i realnej skali zwrotu, zeby doplata w punktach
    bazowych znaczyla to samo co na produkcji."""
    return [x * odchylenie for x in _zwroty_o_sharpe(sharpe_docelowy, n=n_transakcji)]


def test_KANAREK_narosniecie_hypothesis_trial_NIE_RUSZA_werdyktu():
    """Bramka nie czyta tego licznika, wiec jego wzrost nie moze niczego zmienic.

    Gdyby czytala, po roku przy ~27 000 wierszy nie przeszlaby ZADNA przewaga,
    choćby w pelni realna — a bramka, ktorej nie da sie przejsc, jest
    wylacznikiem, nie pomiarem.

    Druga polowa dowodzi, ze test nie jest tautologiczny: poprzednie
    podlaczenie (licznik jako N) na tych samych danych ZMIENIA werdykt.
    """
    zwroty = _sprawa(0.12)
    licznik = (40, 12613, 27000)

    nowe = [werdykt_dsr(zwroty, doplata=0.0, n=17, trials_kumulatywnie=k,
                        nieznane=("spread",)) for k in licznik]
    assert [ok for ok, _ in nowe] == [True, True, True], [ok for ok, _ in nowe]
    dsry = {re.search(r"Deflowany Sharpe (\d\.\d+)", opis).group(1)
            for _, opis in nowe}
    assert len(dsry) == 1, f"sam DSR tez sie rusza z licznikiem: {dsry}"

    poprzednie = [bramka_dsr(przewaga_dsr(zwroty, k))[0] for k in licznik]
    assert poprzednie == [True, False, False], (
        f"poprzednie podlaczenie mialo zmieniac werdykt przy {licznik}, a dalo "
        f"{poprzednie} — jesli nie zmienia, ten test nie pilnuje niczego")


def test_KANAREK_doliczenie_ZMIERZONEGO_KOSZTU_ZMIENIA_werdykt():
    """NAJWAZNIEJSZY kanarek w tym pliku: koszt jest wielkoscia rozstrzygajaca.

    Ta sama proba, to samo N. Rozni sie wylacznie to, czy doliczono zmierzone
    5,85 pb spreadu, ktorych symulacja nie nalicza:

        Sharpe +0,0952 -> DSR ponad progiem
        Sharpe +0,0822 -> DSR pod progiem

    Gdyby bramka patrzyla na zwroty PRZED doplata, mierzylaby tanszy swiat niz
    ten, w ktorym sklada zlecenia — i bylaby o to latwiejsza.
    """
    zwroty = _sprawa(0.0952)
    przed, o_przed = werdykt_dsr(zwroty, doplata=0.0, n=17,
                                 trials_kumulatywnie=12613, nieznane=("spread",))
    po, o_po = werdykt_dsr(zwroty, doplata=5.85e-4, n=17,
                           trials_kumulatywnie=12613, nieznane=())
    assert przed is True and po is False, (o_przed, o_po)
    assert "PO doliczeniu 5.85 pb" in o_po
    assert "dopłacie ZERO" in o_przed


def test_koszt_rozstrzyga_przy_KAZDYM_N_takze_najlagodniejszym():
    """Dlaczego precyzja N przestaje byc ciezarem bramki: po doliczeniu
    zmierzonego spreadu DSR nie przechodzi przy zadnym wiarygodnym N."""
    po_koszcie = [r - 5.85e-4 for r in _sprawa(0.0952)]
    for n in (17, 22, 57, 86, 12613):
        d = przewaga_dsr(po_koszcie, n)["dsr"]
        assert d < PROG_DSR, f"N={n} przepuszcza wynik po koszcie: DSR {d:.4f}"


def test_doplata_bierze_sie_z_KARTY_a_nie_z_liczby_wpisanej_w_trenerze():
    """Karta jest jedynym zrodlem prawdy o koszcie. `doplata_poza_symulacja`
    odejmuje WYLACZNIE to, czego symulacja nie naliczyla — odjecie
    `policzone` liczyloby prowizje dwa razy."""
    bez = karta_rundy("spot")
    ze_spreadem = karta_rundy("spot", spread_bps=5.85)
    assert bez.doplata_poza_symulacja == 0.0
    assert ze_spreadem.doplata_poza_symulacja == pytest.approx(5.85e-4)
    # prowizja + poslizg SA w symulacji, wiec nie wchodza do doplaty
    assert ze_spreadem.policzone > ze_spreadem.doplata_poza_symulacja
    # `pyl` zostaje nieznany, dopoki trener nie poda kroku i ceny (od 2026-10-03).
    assert "spread" in bez.nieznane and "spread" not in ze_spreadem.nieznane
    assert ze_spreadem.nieznane == ("pyl",)


def test_spread_z_OBCEJ_ksiegi_tez_wchodzi_do_doplaty_ale_nie_domyka_karty():
    """Podloga z cudzej ksiegi jest liczba prawdziwa o czyms innym. Wchodzi do
    doplaty, bo to kierunek OSTROZNY, i zostawia karte niepelna, bo pytanie
    o nasz spread zostaje otwarte."""
    k = karta_rundy("spot", spread_bps=5.85, spread_nasza_ksiega=False)
    assert k.doplata_poza_symulacja == pytest.approx(5.85e-4)
    assert "spread" in k.nieznane and not k.pelna


def test_bramka_w_trenerze_liczy_DSR_PO_koszcie_i_z_N_z_konfiguracji():
    """Kanarek na samo podlaczenie, zrodlowo — dwie rzeczy naraz."""
    wywolanie = re.search(r"dsr_ok, opis_dsr = werdykt_dsr\((.*?)\n\n",
                          ZRODLO_TRENERA, re.S)
    assert wywolanie, "train() nie wola `werdykt_dsr` — DSR nie dochodzi do bramki"
    arg = wywolanie.group(1)
    assert "doplata=karta.doplata_poza_symulacja" in arg, (
        "DSR liczony na zwrotach PRZED kosztem, ktorego symulacja nie nalicza")
    # `mv_id` jest tu czescia wymogu, nie szczegolem: wiersz biezacego przebiegu
    # stoi w `training` w chwili liczenia N, wiec bez niego filtr statusu gubi
    # dokladnie te probe, ktora bramka wlasnie ocenia. Osobno pilnuje tego
    # `test_n_selekcji_tylko_przebiegi_z_wynikiem`.
    # Od refaktoru T2 `werdykt` dostaje N z `_mianowniki_dsr`, a `train()`
    # podaje mu dokladnie `n_selekcji` stamtad.
    assert "n=n," in arg and "werdykt(zp, karta=k.karta, n=n_selekcji," in ZRODLO_TRENERA
    assert "n_selekcji = n_konfiguracji(cur, model_id, mv_id)" in ZRODLO_TRENERA, (
        "N nie pochodzi z liczby rozroznialnych konfiguracji ALBO nie dolicza "
        "wlasnej wersji biezacego przebiegu")
    assert "trials_kumulatywnie=trials" in arg
    assert "przewaga_dsr(wszystkie_zwroty, trials)" not in ZRODLO_TRENERA, (
        "licznik hipotez znow jest N deflacji — patrz `n_konfiguracji`")


def test_opis_podaje_ZRODLO_N_i_zastrzezenie_o_dolnej_granicy():
    """Liczba bez pochodzenia czyta sie jak pomiar zamkniety. 57 nim nie jest."""
    _, opis = werdykt_dsr(_sprawa(0.0952), doplata=5.85e-4, n=57,
                          trials_kumulatywnie=12613, nieznane=())
    assert "N = 57 rozróżnialnych konfiguracji z learn.model_version" in opis
    assert "DOLNA GRANICA" in opis
    assert "12613" in opis and "NIE jest tu mianownikiem" in opis
