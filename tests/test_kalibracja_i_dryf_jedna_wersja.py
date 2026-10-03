"""KALIBRACJA I DRYF OPISUJĄ JEDEN MODEL — ten, którym silnik handluje.

CO BYŁO ZŁE

`/learning/calibration` i `/learning/drift` filtrowały wyłącznie po
`m.engine`, a korelowane podzapytanie przypinało `evaluated_at` / `event_time`
do KAŻDEJ wersji z osobna — czyli do każdej po jej własnej ostatniej ocenie.
Tymczasem `train.py:642` zakłada NOWĄ `learn.model_version` przy każdym
treningu, a od migracji 0025 świeca 4h i świeca 1h trenują się RÓWNOLEGLE,
jako osobne modele tego samego silnika.

Skutki, każdy inny:

  * `/calibration` — `podsumowanie.transakcji` jest sumą po wszystkich
    wersjach, więc zawyżoną krotnie, a werdykt („Pewność różnicuje
    trafność…") liczy się z mieszanki modeli, w tym horyzontu, którym silnik
    nie handluje. `Learning.jsx:223` drukuje tę liczbę jako jeden pomiar.
  * `/drift` — `ORDER BY d.psi DESC LIMIT %s` wybiera największe dryfy
    Z DOWOLNEJ wersji. Czerwony pasek na dashboardzie może więc opisywać
    model, którego nie ma na produkcji, a prawdziwy dryf modelu handlującego
    wypada poza `LIMIT`.

To ten sam błąd, który `/walk-forward` ma opisany i naprawiony w docstringu:
wybór idzie przez `learn.learning_state.model_version_id` dla `DEFAULT_BAR`.

CZEGO PILNUJE ATRAPA

`BazaUczenia` nie oddaje gotowej odpowiedzi. Trzyma kubełki i dryfy DWÓCH
wersji i stosuje do nich to, co zapytanie faktycznie mówi — filtr po wersji
albo po silniku, korelowany wybór najnowszej oceny, sortowanie i limit. Stare
zapytanie dostaje więc dokładnie to, co dostawało od Postgresa, i test na nim
pada z tego samego powodu, z którego dashboard kłamał.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from services.api.routers import learning as modul
from services.engine.horizons import DEFAULT_BAR
from tests.test_dane_bez_zer import podlacz, sprawdz_kolumny

# Wersja, którą `learn.learning_state` wskazuje dla horyzontu domyślnego —
# jedyna, o której wolno mówić na tej zakładce.
MV_HANDLOWA = 77
# Drugi model TEGO SAMEGO silnika: tor porównawczy 1h. Jego liczby są celowo
# rażąco inne, żeby domieszka była widoczna w wyniku, a nie tylko w SQL-u.
MV_POROWNAWCZA = 78

OCENA_STARA = datetime(2026, 9, 20, 4, 40, tzinfo=timezone.utc)
OCENA_NOWA = datetime(2026, 9, 23, 4, 40, tzinfo=timezone.utc)


def kubelek(lo, hi, predicted, observed, n, at):
    return {"bin_lo": lo, "bin_hi": hi, "predicted_mean": predicted,
            "observed_rate": observed, "n_samples": n, "evaluated_at": at}


# Model handlujący: 200 rozliczonych transakcji, trafność wyraźnie zależna
# od deklarowanej pewności.
KALIBRACJA = {
    MV_HANDLOWA: [
        kubelek(0.5, 0.7, 0.60, 0.50, 100, OCENA_STARA),
        kubelek(0.7, 0.9, 0.80, 0.90, 100, OCENA_STARA),
        # Ocena nowsza tej samej wersji — te dwa kubełki mają wygrać.
        kubelek(0.5, 0.7, 0.60, 0.52, 100, OCENA_NOWA),
        kubelek(0.7, 0.9, 0.80, 0.88, 100, OCENA_NOWA),
    ],
    # Tor 1h: dziesięć razy więcej próbek i trafność rozjechana na oba krańce.
    # Domieszany do wyniku przesuwa WSZYSTKIE trzy liczby podsumowania —
    # liczbę transakcji, rozpiętość i werdykt z niej policzony.
    MV_POROWNAWCZA: [
        kubelek(0.5, 0.7, 0.60, 0.15, 2000, OCENA_NOWA),
        kubelek(0.7, 0.9, 0.80, 0.99, 2000, OCENA_NOWA),
    ],
}

DRYF = {
    MV_HANDLOWA: [
        {"feature": "rsi_14@1", "psi": 0.12, "ks_stat": 0.05, "event_time": OCENA_NOWA},
        {"feature": "atr_norm@1", "psi": 0.08, "ks_stat": 0.03, "event_time": OCENA_NOWA},
    ],
    MV_POROWNAWCZA: [
        {"feature": "cecha_toru_1h@1", "psi": 9.90, "ks_stat": 0.80, "event_time": OCENA_NOWA},
    ],
}

# Obie wersje należą do tego samego silnika — na tym polega cała wada.
SILNIK_WERSJI = {MV_HANDLOWA: "crypto", MV_POROWNAWCZA: "crypto"}


class BazaUczenia:
    """Atrapa trzech zapytań zakładki nauki. Stosuje klauzule, nie zgaduje wyniku."""

    def __init__(self, *, stan: int | None = MV_HANDLOWA):
        self.stan = stan
        self.zapytania: list[tuple[str, object]] = []

    async def fetch_one(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.zapytania.append((plaski, params))
        if "learn.learning_state" in plaski:
            engine, bar = params[0], params[1]
            if engine != "crypto" or bar != DEFAULT_BAR or self.stan is None:
                return None
            wiersz = {"model_version_id": self.stan}
            sprawdz_kolumny(plaski, [wiersz])
            return wiersz
        if "learn.training_run" in plaski:
            # `walk-forward` pyta o przebieg. Ten test porównuje WYBÓR WERSJI
            # trzech endpointów, więc dalej już nic nie musi się wydarzyć.
            return None
        raise AssertionError("zapytanie bez podłożonej odpowiedzi:\n" + plaski)

    async def fetch_all(self, sql, params=None):
        plaski = " ".join(sql.split())
        self.zapytania.append((plaski, params))
        if "learn.calibration_bin" in plaski:
            return self._kalibracja(plaski, params)
        if "learn.drift_metric" in plaski:
            return self._dryf(plaski, params)
        raise AssertionError("zapytanie bez podłożonej odpowiedzi:\n" + plaski)

    # ------------------------------------------------------------- klauzule
    def _wersje(self, sql: str, params) -> list[int]:
        """Które wersje przepuszcza WHERE tego zapytania."""
        if "model_version_id = %s" in sql:
            return [params[0]]
        if "m.engine::text = %s" in sql:
            # Stare zapytanie: cały silnik, czyli OBA modele.
            return [w for w, e in SILNIK_WERSJI.items() if e == params[0]]
        raise AssertionError(
            "zapytanie nie zawęża ani po wersji modelu, ani po silniku — atrapa "
            "nie zgaduje, co miało znaczyć:\n" + sql)

    def _kalibracja(self, sql, params):
        out = []
        for w in self._wersje(sql, params):
            wszystkie = KALIBRACJA.get(w, [])
            if not wszystkie:
                continue
            # Korelowane `max(evaluated_at)` — obie wersje zapytania mają je
            # PER WERSJA, i właśnie w tym rzecz: wybiera najnowszą ocenę
            # każdego modelu z osobna, a nie jedną na wykres.
            naj = max(k["evaluated_at"] for k in wszystkie)
            out += [k for k in wszystkie if k["evaluated_at"] == naj]
        out.sort(key=lambda k: k["bin_lo"])
        wiersze = [{"bin_lo": k["bin_lo"], "bin_hi": k["bin_hi"],
                    "predicted_mean": k["predicted_mean"],
                    "observed_rate": k["observed_rate"], "n_samples": k["n_samples"]}
                   for k in out]
        if wiersze:
            sprawdz_kolumny(sql, wiersze[:1])
        return wiersze

    def _dryf(self, sql, params):
        out = []
        for w in self._wersje(sql, params):
            wszystkie = DRYF.get(w, [])
            if not wszystkie:
                continue
            naj = max(d["event_time"] for d in wszystkie)
            out += [d for d in wszystkie if d["event_time"] == naj]
        out.sort(key=lambda d: d["psi"], reverse=True)
        limit = params[-1]
        wiersze = [dict(d) for d in out[:limit]]
        if wiersze:
            sprawdz_kolumny(sql, wiersze[:1])
        return wiersze


@pytest.fixture
def baza(monkeypatch):
    def zbuduj(**kw):
        return podlacz(monkeypatch, modul, BazaUczenia(**kw))

    return zbuduj


# ============================================================== kalibracja

def test_kalibracja_liczy_TYLKO_wersje_ktora_handluje(baza):
    """`transakcji` to liczba, którą właściciel czyta jako „na tylu decyzjach
    to zmierzono". Zsumowana po wszystkich wersjach jest po prostu nieprawdziwa."""
    baza()

    wynik = asyncio.run(modul.calibration("crypto"))

    assert wynik["podsumowanie"]["transakcji"] == 200, (
        f"podsumowanie stoi na {wynik['podsumowanie']['transakcji']} rozliczeniach "
        f"zamiast na 200 — do wyniku weszły kubełki innej wersji modelu")
    assert len(wynik["kubelki"]) == 2, (
        f"{len(wynik['kubelki'])} kubełków zamiast dwóch — wykres rysuje kilka "
        f"modeli jako jedną krzywą kalibracji")


def test_kalibracja_bierze_NAJNOWSZA_ocene_tej_wersji(baza):
    """Zawężenie do jednej wersji nie może przy okazji zgubić wyboru najnowszej
    oceny — inaczej wykres zamarłby na pierwszym treningu."""
    baza()

    wynik = asyncio.run(modul.calibration("crypto"))

    obserwowane = sorted(k["observed"] for k in wynik["kubelki"])
    assert obserwowane == [0.52, 0.88], (
        f"kubełki {obserwowane} pochodzą ze starszej oceny — wybór najnowszej "
        f"przestał działać")


def test_werdykt_kalibracji_nie_jest_liczony_z_mieszanki_modeli(baza):
    """Werdykt to jedno zdanie, które ktoś przeczyta i na jego podstawie zaufa
    progowi wejścia albo nie. Policzony z domieszki toru porównawczego mówi
    o rozpiętości, której model handlujący nie ma."""
    baza()

    wynik = asyncio.run(modul.calibration("crypto"))

    assert wynik["podsumowanie"]["rozpietosc_trafnosci"] == pytest.approx(0.36), (
        "rozpiętość policzona z innego zbioru kubełków niż kubełki tej wersji")
    assert "różnicuje trafność" in wynik["podsumowanie"]["werdykt"]


def test_kalibracja_bez_stanu_nauki_oddaje_PUSTO_a_nie_cudze_kubelki(baza):
    """Brak wiersza `learn.learning_state` znaczy „nie wiadomo, który model".
    Pokazanie wtedy kubełków dowolnej wersji jest gorsze niż pusty wykres."""
    baza(stan=None)

    wynik = asyncio.run(modul.calibration("crypto"))

    assert wynik == {"kubelki": [], "podsumowanie": None}


# ==================================================================== dryf

def test_dryf_pokazuje_cechy_WERSJI_HANDLUJACEJ(baza):
    """`ORDER BY d.psi DESC LIMIT %s` po wszystkich wersjach silnika wypycha
    z listy prawdziwe dryfy modelu produkcyjnego i wstawia w ich miejsce cudze."""
    baza()

    wynik = asyncio.run(modul.drift("crypto", 12))
    cechy = [p["feature"] for p in wynik["pozycje"]]

    assert "cecha_toru_1h@1" not in cechy, (
        f"na liście dryfu stoi cecha modelu, którym silnik nie handluje: {cechy}")
    assert cechy == ["rsi_14@1", "atr_norm@1"], f"lista dryfu to {cechy}"


def test_najwiekszy_dryf_na_liscie_opisuje_model_z_produkcji(baza):
    """Pierwsza pozycja jest tą, którą widać na czerwono. Zanim poprawka weszła,
    była nią cecha toru porównawczego z psi 9,9 — przy modelu handlującym
    spokojnie siedzącym na 0,12."""
    baza()

    wynik = asyncio.run(modul.drift("crypto", 12))

    assert wynik["pozycje"][0]["psi"] == pytest.approx(0.12)


def test_dryf_bez_stanu_nauki_oddaje_PUSTA_LISTE_z_progami(baza):
    """Progi mają zostać: front bierze je stąd i nie ma własnej kopii
    (patrz `test_dryf.py::test_interfejs_NIE_MA_wlasnej_kopii_progow`)."""
    baza(stan=None)

    wynik = asyncio.run(modul.drift("crypto", 12))

    assert wynik["pozycje"] == []
    assert wynik["prog_alarmu"] is not None and wynik["prog_trwaly"] is not None


# ======================================= wybór wersji jest JEDEN dla zakładki

def test_kalibracja_i_dryf_pytaja_o_wersje_tak_samo_jak_walk_forward(baza):
    """Dwie reguły wyboru wersji rozjechałyby kafle z wykresami pod nimi —
    a rozjazd wyglądałby jak sprzeczność w danych, nie jak błąd w zapytaniu."""
    b = baza()
    asyncio.run(modul.calibration("crypto"))
    asyncio.run(modul.drift("crypto", 12))
    asyncio.run(modul.walk_forward("crypto", 40))

    stany = [(sql, p) for sql, p in b.zapytania if "learn.learning_state" in sql]
    assert len(stany) == 3, "nie każdy z trzech endpointów pyta o stan nauki"
    assert len({(sql, p) for sql, p in stany}) == 1, (
        f"endpointy wybierają wersję modelu różnymi zapytaniami: {stany}")
    assert all(p[1] == DEFAULT_BAR for _, p in stany), (
        "wybór nie jest zawężony do horyzontu, którym silnik handluje")
