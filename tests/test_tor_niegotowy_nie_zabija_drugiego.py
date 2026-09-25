"""Bramka jednego toru nie ma prawa zabić wyniku toru, który się udał.

CO ZMIERZONO NA PRODUKCJI 2026-09-25

Job `trader-trainer` liczy dwa tory w JEDNYM skrypcie powłoki
(`charts/trader/templates/trainer-cron.yaml`). Tor crypto kończył się wynikiem
(„Stan: learning, 26/38 okien"), a tor equity zaraz po nim przerywał na pustym
uniwersum decyzyjnym — `tradeable = false` na wszystkich 21 akcjach, ustawione
ŚWIADOMIE migracją 0047 z adnotacją, że włączenie to osobna migracja PO pierwszym
walk-forwardzie. Kod wyjścia 2 szedł do wspólnego `RC`, więc:

  * cały Job był czerwony,
  * ArgoCD stało trwale `Degraded`,
  * to samo padnięte zadanie wracało każdej nocy.

Czerwony powtarzany codziennie o stanie, o którym ktoś już zdecydował, jest
dokładnie tym wzorcem, który `services/engine/dozor.py` opisuje jako zmierzony
2026-09-12: kill-switch aktywny półtorej doby → 144 padnięte Joby na dobę →
„uczy odklikiwać całą kategorię". Zabierał przy tym jedyny sygnał, jakim tor
crypto w ogóle dysponuje.

CO PILNUJĄ TE TESTY — I DLACZEGO AŻ TRZY POZIOMY

  1. `train.py` oddaje INNY kod dla „nie wolno było zacząć" niż dla „miałem
     policzyć i nie policzyłem". Bez tego rozróżnienia wołający nie ma z czego
     wnioskować, więc żadna poprawka w skrypcie nie jest możliwa.
  2. Bramka DALEJ ODMAWIA i dalej jest głośna. Kod niezerowy, komunikat
     PRZERWANO, polecenie sprawdzające — kanarek na „poprawkę", która po prostu
     wycisza gate i zwraca zero.
  3. Skrypt zadania jest URUCHAMIANY, a nie czytany. Sprawdzenie przez `grep`
     po treści szablonu przechodziłoby dla skryptu, który zbiera kody poprawnie
     w jednym miejscu i gubi je w drugim. Zamiast tego podstawiamy atrapę
     `python` na PATH, każemy jej zwrócić zadany kod i patrzymy na kod wyjścia
     CAŁEGO skryptu — czyli na to samo, co widzi Kubernetes.

NIC TU NIE DOTYKA BAZY ANI GIEŁDY: kursor jest atrapą, a atrapa `python`
w teście skryptu nie uruchamia żadnego modułu tego repozytorium.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from services.engine.horizons import H1H
from services.engine.train import (
    KOD_NIC_NIE_POLICZONO, KOD_TOR_NIEGOTOWY, train,
)

KORZEN = Path(__file__).resolve().parents[1]
CHART = Path(os.environ.get("CHART_TRADER", KORZEN / "charts/trader"))
SZABLON_TRENERA = CHART / "templates/trainer-cron.yaml"

_KOMENTARZ_HELMA = re.compile(r"\{\{-?/\*.*?\*/-?\}\}", re.S)
_AKCJA_HELMA = re.compile(r"\{\{-?.*?-?\}\}", re.S)


# --------------------------------------------------------------- atrapa bazy

class Kursor:
    """Kursor, który oddaje z góry ustawione odpowiedzi w kolejności zapytań.

    Świadomie NIE interpretuje SQL-a: tu mierzymy reakcję `train()` na PUSTY
    zbiór, a nie to, jak brzmi zapytanie (tym zajmuje się
    `test_uniwersum_diagnostyki.py`).
    """

    def __init__(self, odpowiedzi: list):
        self._kolejka = list(odpowiedzi)
        self.zapytania: list[str] = []
        self.parametry: list[tuple] = []
        self._ostatnia = None

    def execute(self, sql, params=None):
        self.zapytania.append(" ".join(sql.split()))
        self.parametry.append(params)
        self._ostatnia = self._kolejka.pop(0) if self._kolejka else []

    def fetchall(self):
        return self._ostatnia if isinstance(self._ostatnia, list) else []

    def fetchone(self):
        if isinstance(self._ostatnia, list):
            return self._ostatnia[0] if self._ostatnia else None
        return self._ostatnia


class Polaczenie:
    def __init__(self, kursor):
        self._kursor = kursor
        self.commity = 0

    def cursor(self):
        return self._kursor

    def commit(self):
        self.commity += 1


def _uruchom(odpowiedzi: list, engine: str = "equity", kind: str = "stock"):
    kursor = Kursor(odpowiedzi)
    conn = Polaczenie(kursor)
    with pytest.raises(SystemExit) as wyjatek:
        train(conn, engine, 1400, 60, H1H, kind=kind)
    return wyjatek.value.code, conn


# ------------------------------------------------- 1. kod wyjścia rozróżnia

def test_puste_uniwersum_ma_WLASNY_kod_wyjscia():
    """Sedno poprawki: „nie wolno było zacząć" ≠ „miałem policzyć i nie policzyłem".

    Dopóki oba stany mają kod 2, skrypt zadania nie ma z czego wnioskować —
    a bez wnioskowania każdy wariant rozwiązania jest zgadywaniem.
    """
    kod, _ = _uruchom([[]])            # uniwersum decyzyjne: zero wierszy
    assert kod == KOD_TOR_NIEGOTOWY, (
        f"puste uniwersum kończy się kodem {kod}; przy {KOD_NIC_NIE_POLICZONO} "
        f"skrypt zadania nie odróżni bramki od awarii i zapali cały Job")
    assert KOD_TOR_NIEGOTOWY != KOD_NIC_NIE_POLICZONO, (
        "dwa różne stany nie mogą mieć jednego kodu — to była cała wada")


def test_bramka_pustego_uniwersum_NADAL_ODMAWIA(capsys):
    """Kanarek na „poprawkę" polegającą na wyciszeniu bramki.

    Zwrócenie zera i policzenie zera okien byłoby najgorszym możliwym wynikiem:
    Job zielony, w panelu nie przybywa wersji modelu, pętla decyzyjna odmawia
    handlu z powodu „brak zwalidowanego modelu" — trzy objawy, z których żaden
    nie wskazuje przyczyny.
    """
    kod, conn = _uruchom([[]])
    assert kod != 0, "bramka przestała odmawiać"
    wypis = capsys.readouterr().out
    assert "PRZERWANO" in wypis, "bramka przestała być głośna"
    assert "puste uniwersum decyzyjne" in wypis
    assert "SELECT count(*)" in wypis, (
        "zniknęło polecenie sprawdzające — człowiek musi wiedzieć, CO zobaczyć "
        "w bazie, żeby zdjąć bramkę")
    assert conn.commity == 0, "bramka zapisała coś do bazy zanim odmówiła"


def test_niepuste_uniwersum_NIE_zatrzymuje_sie_na_tej_bramce():
    """Kanarek odwrotny: bramka odrzucająca wszystko przechodziłaby oba testy wyżej.

    Dla silnika `crypto` (bez bramki podziałów akcji) przebieg z niepustym
    uniwersum musi ZAJŚĆ DALEJ — tu zatrzyma go dopiero atrapa, której skończą
    się odpowiedzi, więc kod wyjścia nie będzie już kodem bramki.
    """
    wiersze = [{"id": 1, "symbol": "BTCUSDC", "quote_ccy": "USDC"}]
    kursor = Kursor([wiersze])
    conn = Polaczenie(kursor)
    with pytest.raises(Exception) as wyjatek:
        train(conn, "crypto", 1400, 60, H1H, kind="spot_eu")
    assert not (isinstance(wyjatek.value, SystemExit)
                and wyjatek.value.code == KOD_TOR_NIEGOTOWY), (
        "bramka zapaliła się przy NIEPUSTYM uniwersum — mierzyłaby wtedy "
        "cokolwiek innego niż pustka")
    assert any("tradeable" in z for z in kursor.zapytania), (
        "trener przestał pytać o `tradeable`, czyli o uniwersum decyzyjne")


# ------------------------------------- 2. bramka danych equity (podziały akcji)

def test_equity_z_uniwersum_ale_bez_znacznika_podzialow_tez_jest_NIEGOTOWY(capsys):
    """Druga bramka, ten sam kod wyjścia — bo to ten sam rodzaj stanu.

    Podział 10:1 to w szeregu −90% zwrotu, `ret_long` sięga 168 świec, a bufor
    EMA 288: jedno zdarzenie zatruwa ~670 świec i wchodzi do kalibracji jako
    obserwacja kilkunastosigmowa. Zdjęcie tej bramki wymaga decyzji człowieka
    (`adjustment=split` i POWTÓRZENIE backfillu), tak samo jak zdjęcie bramki
    uniwersum wymaga migracji — więc tak samo nie może malować Joba na czerwono
    co noc.
    """
    wiersze = [{"id": 7, "symbol": "NVDA", "quote_ccy": "USD"}]
    kod, conn = _uruchom([wiersze, {"bez": 1}])
    assert kod == KOD_TOR_NIEGOTOWY
    wypis = capsys.readouterr().out
    assert "PRZERWANO" in wypis
    assert "podziały akcji" in wypis
    assert conn.commity == 0, "bramka danych zapisała coś do bazy"


def test_bramka_podzialow_pyta_o_znacznik_Z_ADAPTERA_a_nie_o_wlasny_literal():
    """Dwie kopie nazwy klucza rozjechałyby się CICHO.

    Bramka pytająca o klucz, którego nikt nie ustawia, milczy albo krzyczy
    ZAWSZE — w obu przypadkach niezależnie od danych, czyli nie jest bramką.
    Nazwa stoi w module, który decyduje o `adjustment`.
    """
    from services.exchanges.alpaca import ZNACZNIK_KOREKTY_PODZIALOW

    wiersze = [{"id": 7, "symbol": "NVDA", "quote_ccy": "USD"}]
    kursor = Kursor([wiersze, {"bez": 1}])
    with pytest.raises(SystemExit):
        train(Polaczenie(kursor), "equity", 1400, 60, H1H, kind="stock")
    # Nazwa klucza idzie PARAMETREM, nie w treści SQL-a — więc szukamy jej
    # w parametrach, a nie w napisie zapytania.
    wszystkie = [str(x) for p in kursor.parametry for x in (p or ())]
    assert any(ZNACZNIK_KOREKTY_PODZIALOW in x for x in wszystkie), (
        f"bramka nie pyta o znacznik zdefiniowany w adapterze Alpaki; "
        f"parametry zapytań: {kursor.parametry}")


def test_bramka_podzialow_NIE_dotyczy_toru_crypto():
    """Bybit nie dzieli akcji. Bramka rozciągnięta na crypto zatrzymałaby tor,
    który dziś jako jedyny oddaje wynik — czyli zrobiłaby dokładnie to, co ta
    zmiana ma usunąć."""
    wiersze = [{"id": 1, "symbol": "BTCUSDC", "quote_ccy": "USDC"}]
    kursor = Kursor([wiersze])
    with pytest.raises(Exception):
        train(Polaczenie(kursor), "crypto", 1400, 60, H1H, kind="spot_eu")
    from services.exchanges.alpaca import ZNACZNIK_KOREKTY_PODZIALOW
    wszystkie = [str(x) for p in kursor.parametry for x in (p or ())]
    assert not any(ZNACZNIK_KOREKTY_PODZIALOW in x for x in wszystkie), (
        "bramka podziałów akcji pyta o crypto")


# ------------------------------------------- 3. skrypt zadania, URUCHAMIANY

def _skrypt_trenera() -> str:
    """Treść skryptu powłoki z szablonu — bez binarki `helm`.

    Akcje Helma będące całym wierszem (`{{- range }}`, `{{- end }}`, `{{- if }}`)
    znikają, więc pętla po horyzontach zostaje rozwinięta do JEDNEJ iteracji na
    tor. Do tego, co tu mierzymy — składania kodów wyjścia — jedna iteracja
    wystarcza, a druga nie dodałaby nowej ścieżki.

    Akcje wewnątrz wiersza zamieniamy na wartości z `values.yaml` tam, gdzie
    mają znaczenie (nazwy plików w wywołaniach), a w pozostałych na napis.
    """
    tekst = _KOMENTARZ_HELMA.sub("", SZABLON_TRENERA.read_text(encoding="utf-8"))
    wiersze = []
    for wiersz in tekst.splitlines():
        goly = wiersz.strip()
        if goly.startswith("{{") and goly.endswith("}}") and _AKCJA_HELMA.fullmatch(goly):
            continue
        wiersze.append(_AKCJA_HELMA.sub("SZABLON", wiersz))
    manifesty = [d for d in yaml.safe_load_all("\n".join(wiersze)) if d]
    kron = [d for d in manifesty if d.get("kind") == "CronJob"]
    assert len(kron) == 1
    polecenie = kron[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"]
    assert polecenie[:2] == ["sh", "-c"]
    return polecenie[2]


def _przebieg(tmp_path: Path, kod_crypto: int, kod_equity: int) -> subprocess.CompletedProcess:
    """Uruchomienie skryptu zadania z atrapą `python` na PATH.

    Atrapa rozpoznaje tor po argumentach (`--engine equity`) i zwraca zadany
    kod; dozór zwraca zawsze zero, bo tu mierzymy skladanie kodów TRENERA.
    Nic z tego repozytorium nie jest uruchamiane, więc test nie dotyka ani bazy,
    ani giełdy.
    """
    atrapa = tmp_path / "python"
    atrapa.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *train.py*--engine\\ equity*) echo "ATRAPA equity"; exit '
        f"{kod_equity} ;;\n"
        '  *train.py*) echo "ATRAPA crypto"; exit ' f"{kod_crypto} ;;\n"
        '  *) echo "ATRAPA dozor"; exit 0 ;;\n'
        "esac\n")
    atrapa.chmod(atrapa.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    skrypt = tmp_path / "zadanie.sh"
    skrypt.write_text(_skrypt_trenera())
    return subprocess.run(
        ["sh", str(skrypt)], capture_output=True, text=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path)})


def test_skrypt_jest_poprawna_powloka(tmp_path):
    """Bez tego cały blok niżej mógłby mierzyć błąd składni zamiast logiki."""
    skrypt = tmp_path / "zadanie.sh"
    skrypt.write_text(_skrypt_trenera())
    sprawdzenie = subprocess.run(["sh", "-n", str(skrypt)],
                                 capture_output=True, text=True)
    assert sprawdzenie.returncode == 0, sprawdzenie.stderr


def test_NIEGOTOWY_tor_equity_nie_zapala_Joba_gdy_crypto_sie_udal(tmp_path):
    """DOKŁADNIE stan z produkcji 2026-09-25: crypto 0, equity 3."""
    wynik = _przebieg(tmp_path, kod_crypto=0, kod_equity=KOD_TOR_NIEGOTOWY)
    assert wynik.returncode == 0, (
        f"Job czerwony (kod {wynik.returncode}), choć tor crypto policzył wynik, "
        f"a tor equity był tylko NIEURUCHOMIONY. To jest ta wada: sygnał "
        f"zawsze czerwony nic nie znaczy.\n{wynik.stdout}\n{wynik.stderr}")


def test_bramka_jest_WIDOCZNA_w_logu_mimo_zielonego_Joba(tmp_path):
    """Kanarek do poprzedniego, i ważniejszy od niego.

    Poprawka polegająca na samym przemilczeniu kodu 3 byłaby zamianą fałszywego
    alarmu na ciszę — czyli na gorszy stan. Bramka musi zostać głośna w kanale,
    który nie traci wiarygodności od powtarzania: w logu przebiegu.
    """
    wynik = _przebieg(tmp_path, kod_crypto=0, kod_equity=KOD_TOR_NIEGOTOWY)
    assert "TORY NIEURUCHOMIONE" in wynik.stdout, (
        f"zielony Job i ani słowa o nieuruchomionym torze:\n{wynik.stdout}")
    assert "equity/" in wynik.stdout, "podsumowanie nie mówi, KTÓRY tor"


def test_AWARIA_toru_equity_nadal_zapala_Joba(tmp_path):
    """Kanarek na zbyt szerokie zwolnienie: `|| true` po torze equity przechodzi
    test wyżej i kasuje CAŁY sygnał o awarii tego toru."""
    for kod in (1, KOD_NIC_NIE_POLICZONO):
        wynik = _przebieg(tmp_path, kod_crypto=0, kod_equity=kod)
        assert wynik.returncode == kod, (
            f"awaria toru equity z kodem {kod} nie zapaliła Joba "
            f"(kod {wynik.returncode}) — zwolnienie objęło za dużo")


def test_AWARIA_toru_crypto_nadal_zapala_Joba(tmp_path):
    """Tor produkcyjny nie zmienia zachowania ani na jotę."""
    wynik = _przebieg(tmp_path, kod_crypto=KOD_NIC_NIE_POLICZONO, kod_equity=0)
    assert wynik.returncode == KOD_NIC_NIE_POLICZONO, (
        f"awaria toru crypto nie zapaliła Joba (kod {wynik.returncode})")


def test_NIEGOTOWY_tor_nie_zasłania_awarii_drugiego_toru(tmp_path):
    """Oba naraz: equity nieuruchomiony, crypto padnięty. Job musi być czerwony
    KODEM CRYPTO, a log musi nadal wymieniać nieuruchomiony tor."""
    wynik = _przebieg(tmp_path, kod_crypto=KOD_NIC_NIE_POLICZONO,
                      kod_equity=KOD_TOR_NIEGOTOWY)
    assert wynik.returncode == KOD_NIC_NIE_POLICZONO
    assert "TORY NIEURUCHOMIONE" in wynik.stdout, (
        "podsumowanie zniknęło, bo przebieg i tak był czerwony — a to dwie różne "
        "informacje dla dwóch różnych torów")


def test_dozor_equity_dostaje_kod_TRENERA_EQUITY_a_nie_wspolny(tmp_path):
    """Alarm o torze, który się nie wywrócił, jest tym samym fałszywym sygnałem
    co czerwony Job bez powodu.

    Do 2026-09-25 oba dozory dostawały wspólne `$RC`, więc awaria crypto kazała
    dozorowi equity ogłosić „Trener zawiódł (equity)".
    """
    linie = [w.strip() for w in _skrypt_trenera().splitlines() if w.strip()]
    equity = [w for w in linie if "dozor.py" in w and "--engine equity" in w]
    crypto = [w for w in linie
              if "dozor.py" in w and "--engine" not in w and "--kod-trenera" in w]
    assert equity and crypto, "zniknęło wywołanie dozoru dla jednego z torów"
    assert "$RC_EQUITY" in equity[0], f"dozór equity dostaje {equity[0]!r}"
    assert "$RC_CRYPTO" in crypto[0], f"dozór crypto dostaje {crypto[0]!r}"
