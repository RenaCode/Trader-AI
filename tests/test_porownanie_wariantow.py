"""Porównanie wariantów: niepełne parsowanie MUSI przerwać, a nie ostrzec.

DLACZEGO TO ISTNIEJE

2026-09-27 porównywałem `--progi wspolny` z `--progi per-rezim` na tych samych
oknach. Wzorzec `okno (\\d+)` zgubił okna 0–9, bo numery jednocyfrowe są
wyrównane DWIEMA spacjami (`okno  0`). Na 29 z 38 okien średnia różnica wyszła
**−0,0101**, a na wszystkich 38 — **+0,0051**. Zgubienie jednej czwartej próby
odwróciło znak wyniku, a wynik wyglądał dokładnie tak samo: liczba, sd, t,
wszystko na miejscu.

Wykryła to niezgodność liczby sparsowanych okien z liczbą, którą podał sam
trener („Dodatnie 24/38 okien"), a nie ogląd wzorca. Dlatego skrypt tę
niezgodność **przerywa**, a nie wypisuje jako uwagę: liczba podana dalej
i ostrzeżenie wyżej to dwie rzeczy, z których czytana jest pierwsza.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "porownaj_warianty", KORZEN / "scripts" / "porownaj_warianty.py")
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)


def _log(pary, dodatnie=None, total=None) -> str:
    """Log w formacie trenera. Numery jednocyfrowe wyrównane DWIEMA spacjami —
    tak, jak je wypisuje `train.py` (`{idx:>2d}`)."""
    linie = []
    for i, (okres, sharpe, n, rezim) in enumerate(pary):
        linie.append(f"  okno {i:>2d}  {okres}  Sharpe/transakcję={sharpe:+.4f}"
                     f"  transakcje={n}  reżim={rezim}  [strategia +0.10%]")
    if total is None:
        total = len(pary)
    if dodatnie is None:
        dodatnie = sum(1 for _, s, _, _ in pary if s > 0)
    linie.append(f"Stan: learning — Dodatnie {dodatnie}/{total} okien "
                 f"out-of-sample (60%).")
    return "\n".join(linie)


PARY = [(f"{m:02d}-01–{m:02d}-28", 0.1 + m / 100, 40 + m, "range")
        for m in range(1, 13)]


def test_parsuje_okna_JEDNOCYFROWE():
    """Cały powód istnienia tego pliku: `okno  0` z dwiema spacjami."""
    znalezione = pw.okna(_log(PARY))
    assert len(znalezione) == 12, (
        f"wzorzec gubi okna o numerach jednocyfrowych — znalazł "
        f"{len(znalezione)} z 12")
    assert "01-01–01-28" in znalezione


def test_niezgodnosc_z_liczba_trenera_PRZERYWA():
    blok = _log(PARY, total=20)  # trener mówi 20, w logu jest 12
    with pytest.raises(SystemExit) as e:
        pw.sprawdz_kompletnosc("wariant", blok, 12)
    assert "12" in str(e.value) and "20" in str(e.value), (
        "komunikat nie podaje obu liczb, więc nie mówi, co się nie zgadza")


def test_zgodnosc_z_liczba_trenera_PRZECHODZI():
    pw.sprawdz_kompletnosc("wariant", _log(PARY), 12)


def test_okna_POMINIETE_W_OCENIE_nie_wchodza():
    """Trener sam ich nie liczy do „Dodatnie N/M", więc i tu nie mogą wejść —
    inaczej sprawdzenie kompletności padałoby na poprawnym logu."""
    blok = _log(PARY) + ("\n  okno 12  12-15–01-12  Sharpe/transakcję=+0.0000"
                         "  transakcje=5  reżim=range  (poniżej 12 transakcji"
                         " — pominięte w ocenie)")
    assert len(pw.okna(blok)) == 12


def test_kluczem_jest_OKRES_a_nie_numer_okna():
    """Dwa przebiegi o różnej liczbie okien mają inne numery dla tego samego
    kawałka kalendarza — porównanie po numerach zestawiałoby różne okresy."""
    a = pw.okna(_log(PARY))
    b = pw.okna(_log(PARY[1:]))  # to samo, bez pierwszego okna
    assert set(a) - set(b) == {"01-01–01-28"}, (
        "przesunięcie o jedno okno rozjechało WSZYSTKIE pary — klucz to numer, "
        "nie okres")


def test_porownanie_nie_myli_KIERUNKU_roznicy(capsys):
    """`porownaj(a, b)` liczy b − a. Odwrócony znak zamieniłby poprawę
    w pogorszenie, a to jedyna rzecz, o którą się tu pyta."""
    a = pw.okna(_log([(o, s, n, r) for o, s, n, r in PARY]))
    lepsze = pw.okna(_log([(o, s + 0.05, n, r) for o, s, n, r in PARY]))
    pw.porownaj("gorszy", a, "lepszy", lepsze)
    wyjscie = capsys.readouterr().out
    assert "+0.0500" in wyjscie, f"różnica policzona nie w tę stronę: {wyjscie}"


def test_przebieg_BEZ_OKIEN_wypada_a_nie_przerywa(capsys, tmp_path):
    """Log pomiaru często zawiera tor NIEURUCHOMIONY (kod 3 — np. equity bez
    potwierdzonej korekty o podziały). Postawiony między dwoma wariantami
    przerywał cały skrypt na „za malo wspolnych okien"."""
    sciezka = tmp_path / "log.txt"
    sciezka.write_text(
        "===== A: tor nieuruchomiony =====\n"
        "PRZERWANO: puste uniwersum decyzyjne\n"
        "===== B: wspolny =====\n" + _log(PARY) + "\n"
        "===== C: per-rezim =====\n"
        + _log([(o, s + 0.02, n, r) for o, s, n, r in PARY]) + "\n",
        encoding="utf-8")
    assert pw.main([str(sciezka)]) == 0
    wyjscie = capsys.readouterr().out
    assert "pomijam" in wyjscie
    assert "+0.0200" in wyjscie, (
        f"po pominięciu pustego toru nie doszło do porównania B z C: {wyjscie}")
