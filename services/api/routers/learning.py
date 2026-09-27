"""Zakladka STOPIEN NAUKI.

Kazdy endpoint tutaj zwraca metryki OUT-OF-SAMPLE. Nie ma tu miejsca, w ktore
mozna by wpisac "wynik backtestu" - to celowe ograniczenie, bo swietny backtest
jest domyslnie sygnalem przeuczenia, nie alfy.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from services.engine.horizons import DEFAULT_BAR
from ..db import fetch_all, fetch_one

# Patrz overview.py: wartosc zamknieta tutaj, zeby nieznany silnik konczyl sie
# odmowa 422, a nie bledem rzutowania enuma w Postgresie (500).
Silnik = Literal["crypto", "equity"]

router = APIRouter(prefix="/api/engines/{engine}/learning", tags=["learning"])


async def _wersja_modelu(engine: str) -> int | None:
    """Wersja modelu, ktorym silnik FAKTYCZNIE handluje - jedna regula na caly modul.

    DLACZEGO TA REGULA, A NIE "najnowsza wersja" CZY "wersja aktywna"

    `train.py:642` zaklada NOWA `learn.model_version` przy kazdym treningu, a od
    migracji 0025 swiece 4h i 1h trenuja sie ROWNOLEGLE, jako osobne modele tego
    samego silnika. "Wszystkie wersje tego silnika" to wiec po tygodniu
    kilkanascie roznych modeli, z czego handluje jeden.

    Wskazuje go `learn.learning_state` dla horyzontu domyslnego - dokladnie ten
    sam wiersz, z ktorego `/state` bierze liczby na kafle. Kazda inna regula
    rozjechalaby wykres z kaflami nad nim, czyli dwie liczby opisujace ten sam
    model pochodzilyby z dwoch roznych.

    Brak stanu albo stan bez wersji to BRAK DANYCH, nie dane czyjes.
    """
    stan = await fetch_one(
        """
        SELECT ls.model_version_id
        FROM learn.learning_state ls
        WHERE ls.engine::text = %s AND ls.bar = %s
        """,
        (engine, DEFAULT_BAR),
    )
    return stan["model_version_id"] if stan else None


@router.get("/state")
async def state(engine: Silnik):
    row = await fetch_one(
        """
        SELECT ls.status, ls.windows_total, ls.windows_positive, ls.median_oos_sharpe,
               ls.positive_ci_low, ls.positive_ci_high,
               ls.max_drawdown, ls.calibration_error, ls.max_psi, ls.trials_run,
               ls.reason, ls.updated_at,
               m.name AS model_name, mv.version AS model_version, mv.trained_from, mv.trained_to
        FROM learn.learning_state ls
        LEFT JOIN learn.model_version mv ON mv.id = ls.model_version_id
        LEFT JOIN learn.model         m  ON m.id  = mv.model_id
        -- Jawny filtr po horyzoncie: od migracji 0025 jeden silnik ma wiele
        -- stanow nauki, po jednym na trenowana swiece. Zakladka pokazuje ten,
        -- ktorym silnik faktycznie handluje - pozostale sa torami porownawczymi.
        WHERE ls.engine::text = %s AND ls.bar = %s
        """,
        (engine, DEFAULT_BAR),
    )
    if not row:
        # KOMPLET KLUCZY Z `null`, a nie obiekt o dwoch polach. W JS brakujacy
        # klucz i klucz o wartosci `undefined` sa nieodroznialne, wiec front
        # wypisywal doslownie "undefined/undefined" - czyli brak pomiaru
        # w przebraniu pomiaru, ktorego nie dalo sie sformatowac.
        return {
            "status": "warmup",
            "reason": "Brak stanu dla tego silnika",
            "updated_at": None,
            "model": None,
            "trained_from": None,
            "trained_to": None,
            "windows_total": None,
            "windows_positive": None,
            "windows_positive_pct": None,
            "windows_positive_ci": None,
            "median_oos_sharpe": None,
            "max_drawdown": None,
            "calibration_error": None,
            "max_psi": None,
            "trials_run": None,
            "n_konfiguracji": None,
        }

    total = row["windows_total"]
    positive = row["windows_positive"]
    # N BRAMKUJACE liczymy TU, czyli PO wczesnym wyjsciu dla silnika bez stanu.
    # Przed nim byloby to zapytanie wykonywane takze wtedy, gdy nie ma czego
    # opisywac - i wywracalo dwie atrapy, ktore (slusznie) odmawiaja zgadywania
    # odpowiedzi na zapytanie, ktorego nikt im nie podlozyl.
    konf = await fetch_one(
        "SELECT count(DISTINCT v.params) AS n FROM learn.model_version v"
        " JOIN learn.model m ON m.id = v.model_id WHERE m.name = %s",
        (row["model_name"],)) if row.get("model_name") else None
    n_konf = konf["n"] if konf else None
    return {
        "status": row["status"],
        "reason": row["reason"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "model": (f'{row["model_name"]} v{row["model_version"]}' if row["model_name"] else None),
        "trained_from": row["trained_from"].isoformat() if row["trained_from"] else None,
        "trained_to": row["trained_to"].isoformat() if row["trained_to"] else None,
        "windows_total": total,
        "windows_positive": positive,
        # To jest glowna liczba "jak dobrze sie uczy": odsetek okien walk-forward
        # z dodatnim wynikiem OUT-OF-SAMPLE. Prog wyjscia z demo: >= 70%.
        # Zero okien daje `null`, a nie 0.0: odsetek z pustego mianownika nie
        # istnieje, a wypisany jako "0,0%" wyglada jak zmierzona porazka.
        "windows_positive_pct": (positive / total * 100) if total else None,
        # Przedzial ufnosci 95% podawany ZAWSZE razem z odsetkiem. Przy 40 oknach
        # ma szerokosc okolo 30 punktow procentowych, wiec sam odsetek zacheca
        # do czytania roznicy kilku punktow jako sygnalu - a to szum.
        "windows_positive_ci": (
            [row["positive_ci_low"] * 100, row["positive_ci_high"] * 100]
            if row["positive_ci_low"] is not None else None),
        "median_oos_sharpe": row["median_oos_sharpe"],
        "max_drawdown": row["max_drawdown"],
        "calibration_error": row["calibration_error"],
        "max_psi": row["max_psi"],
        # Licznik wektorow wag. NIE jest N deflowanego Sharpe'a, choc komentarz
        # przy migracji 0006 tak go zapowiadal: jego kandydaci byli oceniani
        # in-sample, a walk-forward juz za nich zaplacil, liczac wynik poza
        # oknem wyboru. N bierze sie z `count(DISTINCT params)` po
        # `learn.model_version` - patrz `train.n_konfiguracji`.
        #
        # CZEGO TA LICZBA NIE JEST, a co trzeba wiedziec, zeby ja czytac:
        # `learn.hypothesis_trial` ma `UNIQUE (model_id, config_hash)`, a jedyny
        # INSERT (train.py) konczy sie `ON CONFLICT DO NOTHING`. Liczy wiec
        # ROZNE WEKTORY WAG kiedykolwiek zapisane dla tego modelu, a nie proby:
        # dwa przebiegi tej samej konfiguracji dokladaja tylko te wektory,
        # ktore sie zmienily, a `purge_demo.py` czysci tabele w calosci.
        "trials_run": row["trials_run"],
        # N, KTORE FAKTYCZNIE BRAMKUJE - liczba ROZROZNIALNYCH konfiguracji
        # tego modelu. Bez niej panel pokazywal `trials_run` jako miare
        # uczciwosci pomiaru i konczyl zdaniem "bez tej liczby deflated Sharpe
        # jest nieobliczalny", co od 2026-09-27 jest FALSZEM: DSR jest liczony
        # i jawnie NIE z tego licznika. Czytelnik panelu dowiadywal sie wiec
        # odwrotnosci tego, co robi system.
        #
        # Liczone tu, a nie brane z `learning_state`, bo tam takiej kolumny nie
        # ma, a dopisanie jej wymagaloby migracji i zmiany trenera - podczas gdy
        # to jest jedno `count(DISTINCT params)`, zawsze aktualne. Ta sama
        # wielkosc co `train.n_konfiguracji`, wiec panel i bramka nie moga sie
        # rozjechac.
        #
        # DOLNA GRANICA: `params` nie zapisywalo `kind` ani `limit` do
        # 2026-09-26, wiec przebiegi rozniace sie samym cennikiem zlewaly sie
        # w jedna konfiguracje. Panel musi to powiedziec, nie ukryc.
        "n_konfiguracji": n_konf,
    }


@router.get("/weights")
async def weights(engine: Silnik, bar: str = Query("4h")):
    """Wagi sygnalow: wyliczone przez trening kontra zalozenia poczatkowe.

    To jest najbardziej konkretna odpowiedz na pytanie "czy algorytm sie uczy".
    Metryki mowia, czy wynik jest lepszy; ta tabela mowi, CO konkretnie model
    zmienil w swoim rozumowaniu - ktoremu sygnalowi w ktorym rezimie zaczal
    ufac bardziej, a ktoremu mniej.

    Gdy nie ma aktywnej wersji modelu, zwracamy same zalozenia poczatkowe
    i mowimy o tym wprost. Silnik pracuje wtedy wlasnie na nich i uczciwie
    jest to pokazac, zamiast wyswietlac pusta tabele.
    """
    from services.engine.signals import DEFAULT_WEIGHTS, SIGNALS

    row = await fetch_one(
        """
        SELECT mv.version, mv.params, mv.promoted_at, mv.trained_from, mv.trained_to
        FROM learn.model_version mv JOIN learn.model m ON m.id = mv.model_id
        WHERE m.name = %s AND mv.status = 'active'
        ORDER BY mv.version DESC LIMIT 1
        """,
        (f"{engine}_signal_weighting_{bar}",),
    )
    wyuczone = (row["params"] or {}).get("weights") if row else None

    etykiety = {n: SIGNALS[n][2] for n in SIGNALS}
    rodziny = {n: SIGNALS[n][1] for n in SIGNALS}
    rezimy = []
    for rezim, bazowe in DEFAULT_WEIGHTS.items():
        pozycje = []
        for nazwa in SIGNALS:
            baza = float(bazowe.get(nazwa, 0.0))
            teraz = float((wyuczone or {}).get(rezim, {}).get(nazwa, baza))
            pozycje.append({
                "sygnal": nazwa,
                "etykieta": etykiety[nazwa],
                "rodzina": rodziny[nazwa],
                "zalozenie": round(baza, 4),
                "wyuczona": round(teraz, 4),
                "zmiana": round(teraz - baza, 4),
            })
        pozycje.sort(key=lambda x: -x["wyuczona"])
        rezimy.append({"rezim": rezim, "sygnaly": pozycje})

    return {
        "swieca": bar,
        "wersja_modelu": row["version"] if row else None,
        "wyuczone": wyuczone is not None,
        "promowany": row["promoted_at"].isoformat() if row and row["promoted_at"] else None,
        # JEDNA LICZBA ZOSTAJE, ale od 2026-09-27 nie jest tym, czym silnik
        # wchodzi: prog liczy sie per rezim (`services/engine/bramki.py`).
        # Pokazywanie samej zbiorczej wartosci znaczyloby, ze zakladka mowi
        # o innej wielkosci niz ta, ktora rozstrzyga decyzje.
        "prog_wejscia": (row["params"] or {}).get("enter_threshold") if row else None,
        "prog_wejscia_per_rezim": (row["params"] or {}).get(
            "enter_threshold_per_regime") if row else None,
        # Ile OKIEN walk-forward stoi za mediana progu w kazdym rezimie. Prog
        # z dwoch okien i prog z czterdziestu wygladaja identycznie i nie sa
        # tym samym pomiarem.
        "prog_wejscia_okien": (row["params"] or {}).get(
            "enter_threshold_windows") if row else None,
        # Rezimy, w ktorych silnik NIE WCHODZI, bo progu nie udalo sie
        # skalibrowac. Bez tej listy brak wejsc w rezimie wyglada na zakladce
        # tak samo jak spokojny rynek.
        "rezimy_bez_progu": sorted(
            r for r in DEFAULT_WEIGHTS
            if r not in ((row["params"] or {}).get("enter_threshold_per_regime") or {})
        ) if row and (row["params"] or {}).get("enter_threshold_per_regime") else [],
        "trenowany_od": row["trained_from"].isoformat() if row and row["trained_from"] else None,
        "trenowany_do": row["trained_to"].isoformat() if row and row["trained_to"] else None,
        "rezimy": rezimy,
    }


@router.get("/walk-forward")
async def walk_forward(engine: Silnik, limit: int = Query(40, le=200)):
    """Metryki per okno walk-forward. Jedyny dopuszczalny protokol oceny.

    JEDEN PRZEBIEG, JEDEN HORYZONT - i dlatego dwa zapytania przed wlasciwym.

    `learn.wf_window` numeruje `idx` od zera W KAZDYM przebiegu treningowym
    (UNIQUE (training_run_id, idx), migracja 0006), a `train.py` zaklada NOWY
    przebieg przy kazdym treningu. Do tego od migracji 0025 swiece 4h i 1h
    trenuja sie ROWNOLEGLE, jako osobne modele tego samego silnika.

    Filtr po samym `m.engine` zbieral wiec okna ze WSZYSTKICH przebiegow obu
    horyzontow naraz. Po czterech nocnych treningach wykres pokazywal 40
    slupkow bedacych dziesiecioma ostatnimi indeksami razy cztery przebiegi -
    cztery slupki podpisane "okno 39" z roznymi datami, w kolejnosci, ktora nie
    byla chronologiczna. Gorzej: `ORDER BY w.idx DESC LIMIT 40` przy oknach 1h
    siegajacych `idx` 87 zwracal WYLACZNIE okna horyzontu, ktorym silnik nie
    handluje - pod naglowkiem opisanym metrykami z 4h.

    Wybor jest wiec DOKLADNIE TEN SAM co w /state: wiersz `learn.learning_state`
    dla horyzontu domyslnego wskazuje wersje modelu z ostatniego przebiegu, a my
    bierzemy okna tego przebiegu. Druga regula ("aktywna wersja", "najnowszy
    model") rozjechalaby sie z liczbami na kaflach nad tym wykresem - czyli
    wykres opisywalby inny przebieg niz odsetek dodatnich okien obok niego.

    Sortowanie idzie po `test_from`, nie po `idx`: to okno testowe jest osia
    czasu tego wykresu, a nie numer porzadkowy.
    """
    # Brak stanu albo stan bez wersji modelu to BRAK OKIEN, a nie okna czyjes.
    # Poprzednio ta sciezka oddawala okna dowolnego przebiegu, jaki byl w bazie.
    wersja = await _wersja_modelu(engine)
    if wersja is None:
        return []

    przebieg = await fetch_one(
        """
        SELECT tr.id
        FROM learn.training_run tr
        WHERE tr.model_version_id = %s
        ORDER BY tr.started_at DESC, tr.id DESC
        LIMIT 1
        """,
        (wersja,),
    )
    if not przebieg:
        return []

    rows = await fetch_all(
        """
        SELECT w.idx, w.train_from, w.train_to, w.test_from, w.test_to,
               w.n_trades, w.dominant_regime,
               -- FILTER, bo `jsonb_object_agg` z kluczem NULL konczy sie bledem
               -- bazy ("field name must not be null"). Okno bez ani jednej
               -- metryki jest stanem NORMALNYM (front liczy takie osobno),
               -- a wywracalo cale zapytanie, czyli caly wykres.
               jsonb_object_agg(mt.metric, mt.value)
                   FILTER (WHERE mt.metric IS NOT NULL) AS metrics
        FROM learn.wf_window w
        LEFT JOIN learn.wf_metric mt ON mt.wf_window_id = w.id
        WHERE w.training_run_id = %s
        GROUP BY w.id, w.idx, w.train_from, w.train_to, w.test_from, w.test_to,
                 w.n_trades, w.dominant_regime
        ORDER BY w.test_from DESC
        LIMIT %s
        """,
        (przebieg["id"], limit),
    )
    return [
        {
            "idx": r["idx"],
            "test_from": r["test_from"].isoformat(),
            "test_to": r["test_to"].isoformat(),
            "n_trades": r["n_trades"],
            "regime": r["dominant_regime"],
            "metrics": r["metrics"] or {},
        }
        for r in reversed(rows)
    ]


@router.get("/calibration")
async def calibration(engine: Silnik):
    """Czy pewnosc 70% oznacza trafnosc ~70%. Rozjazd = model klamie o swojej pewnosci.

    Zwracamy tez PODSUMOWANIE, bo sam wykres latwo przeczytac zyczeniowo.
    Kluczowe pytanie nie brzmi "czy punkty leza blisko przekatnej", tylko
    "czy wyzsza deklarowana pewnosc oznacza wyzsza trafnosc". Miara przy nim to
    rozpietosc trafnosci miedzy kubelkami: gdy trafnosc jest plaska niezaleznie
    od pewnosci, liczba nie niesie informacji o jakosci sytuacji - i wtedy prog
    wejscia oparty na pewnosci filtruje przypadkowo.
    """
    # JEDNA WERSJA MODELU, TEN SAM WYBOR CO W /state I /walk-forward.
    #
    # Filtr szedl po samym `m.engine`, a podzapytanie przypinalo `evaluated_at`
    # do KAZDEJ wersji z osobna - czyli do kazdej po jej wlasnej ostatniej
    # ocenie. Po czterech nocnych treningach wynikiem bylo ~10 kubelkow razy
    # liczba wersji: `podsumowanie.transakcji` zawyzone krotnie, a werdykt
    # ("Pewnosc roznicuje trafnosc...") liczony z mieszanki modeli, w tym
    # horyzontu, ktorym silnik nie handluje. `Learning.jsx` drukuje te liczbe
    # jako jeden pomiar.
    wersja = await _wersja_modelu(engine)
    if wersja is None:
        return {"kubelki": [], "podsumowanie": None}

    rows = await fetch_all(
        """
        SELECT cb.bin_lo, cb.bin_hi, cb.predicted_mean, cb.observed_rate, cb.n_samples
        FROM learn.calibration_bin cb
        WHERE cb.model_version_id = %s
          AND cb.evaluated_at = (
              SELECT max(evaluated_at) FROM learn.calibration_bin
              WHERE model_version_id = %s)
        ORDER BY cb.bin_lo
        """,
        (wersja, wersja),
    )
    kubelki = [
        {"bin_lo": r["bin_lo"], "bin_hi": r["bin_hi"], "predicted": r["predicted_mean"],
         "observed": r["observed_rate"], "n": r["n_samples"]}
        for r in rows
    ]
    if not kubelki:
        return {"kubelki": [], "podsumowanie": None}

    n = sum(k["n"] for k in kubelki)
    trafnosc = sum(k["observed"] * k["n"] for k in kubelki) / n
    ece = sum(abs(k["observed"] - k["predicted"]) * k["n"] for k in kubelki) / n
    # Rozpietosc liczona tylko na kubelkach o sensownej liczebnosci - inaczej
    # jeden kubelek z czternastoma transakcjami wyznaczalby wniosek.
    istotne = [k for k in kubelki if k["n"] >= 50]
    rozpietosc = (max(k["observed"] for k in istotne) - min(k["observed"] for k in istotne)
                  if len(istotne) >= 2 else None)

    if rozpietosc is None:
        werdykt = "Za mało danych, żeby ocenić."
    elif rozpietosc < 0.10:
        werdykt = (f"Trafność jest praktycznie płaska ({trafnosc:.0%}) niezależnie od "
                   f"deklarowanej pewności. Ta liczba NIE porządkuje sytuacji według "
                   f"jakości — próg wejścia oparty na niej filtruje przypadkowo.")
    elif rozpietosc < 0.20:
        werdykt = (f"Pewność słabo różnicuje trafność (rozpiętość {rozpietosc:.0%} "
                   f"przy średniej {trafnosc:.0%}). Sygnał jest, ale wątły.")
    else:
        werdykt = (f"Pewność różnicuje trafność (rozpiętość {rozpietosc:.0%} przy "
                   f"średniej {trafnosc:.0%}).")

    return {
        "kubelki": kubelki,
        "podsumowanie": {
            "transakcji": n,
            "trafnosc": round(trafnosc, 4),
            "ece": round(ece, 4),
            "rozpietosc_trafnosci": round(rozpietosc, 4) if rozpietosc is not None else None,
            "kubelkow_istotnych": len(istotne),
            "werdykt": werdykt,
        },
    }


@router.get("/drift")
async def drift(engine: Silnik, limit: int = Query(12, le=60)):
    """Dryf rozkladu cech. UWAGA: kolumna nazywa sie `psi`, ale to NIE JEST PSI.

    `train.py` liczy standaryzowane przesuniecie sredniej (d Cohena) powiekszone
    o wzgledna zmiane rozrzutu:

        |srednia_nowa - srednia_stara| / sd_stare + |sd_nowe / sd_stare - 1|

    PSI to suma po kubelkach (p-q)*ln(p/q) - inna konstrukcja i inna skala. Prog
    0,25 z literatury Siddiqiego, ktory stal w tym docstringu i na dashboardzie,
    NIE ZNACZY WIEC NIC dla tej miary, a wygladal na uzasadniony. Wlasciciel
    widzial czerwien przy stanie, ktory czujka uznaje za normalny.

    Porownywane okna to tez swiezsza historia wobec starszej, a NIE rozklad
    treningowy wobec biezacego - mimo nazwy kolumny.

    Progi oddajemy WPROST z modulu dozoru, zeby interfejs nie mial wlasnej kopii
    tych liczb. Poprzednio mial i wlasnie stad wzial sie ten rozjazd.
    """
    from services.engine.dozor import DRYF_TRWALY_PROG, PSI_PROG

    # JEDNA WERSJA MODELU - ta sama regula co w /state, /walk-forward
    # i /calibration. Tu skutek pomylki byl najgorszy z calej zakladki:
    # `ORDER BY d.psi DESC LIMIT %s` po WSZYSTKICH wersjach silnika wybieral
    # najwieksze dryfy z dowolnego modelu, wiec czerwony pasek na dashboardzie
    # mogl opisywac model, ktorego nie ma na produkcji - i odwrotnie, prawdziwy
    # dryf modelu handlujacego wypadal poza `LIMIT`.
    wersja = await _wersja_modelu(engine)
    rows = []
    if wersja is not None:
        rows = await fetch_all(
            """
            SELECT fd.name || '@' || fd.version AS feature, d.psi, d.ks_stat, d.event_time
            FROM learn.drift_metric d
            JOIN feat.feature_def fd ON fd.id = d.feature_def_id
            WHERE d.model_version_id = %s
              AND d.event_time = (SELECT max(event_time) FROM learn.drift_metric
                                  WHERE model_version_id = %s)
            ORDER BY d.psi DESC NULLS LAST
            LIMIT %s
            """,
            (wersja, wersja, limit),
        )
    return {
        "miara": "wielkość efektu (d Cohena + względna zmiana rozrzutu)",
        "prog_alarmu": PSI_PROG,
        "prog_trwaly": DRYF_TRWALY_PROG,
        "pozycje": [
            {"feature": r["feature"], "psi": r["psi"], "ks": r["ks_stat"],
             "at": r["event_time"].isoformat()}
            for r in rows
        ],
    }


@router.get("/signal-performance")
async def signal_performance(engine: Silnik):
    """Skutecznosc per sygnal - ktora hipoteza faktycznie cos wnosi.

    TRAFNOSC LICZY SIE WYLACZNIE PO ZAMKNIETYCH POZYCJACH.

    Poprzednia wersja liczyla `avg(CASE WHEN p.realized_pnl > 0 THEN 1 ELSE 0)`
    po `LEFT JOIN exec.position`. Decyzja, ktora nie ma jeszcze dopasowanej
    zamknietej pozycji - bo pozycja wisi otwarta, bo plan zostal odrzucony, bo
    decyzja brzmiala `hold` - dawala w tym CASE wartosc `ELSE 0.0`, czyli
    liczyla sie jako NIETRAFIENIE. Kolumna "Trafnosc" byla przez to
    systematycznie zanizona, i to tym mocniej, im mlodszy sygnal: swiezo dodana
    hipoteza ma najwiecej decyzji jeszcze nierozliczonych.

    Teraz nierozliczone decyzje wypadaja z MIANOWNIKA (FILTER), a ich liczba
    jest widoczna osobno (`unsettled_count`) - bo "trafnosc 60% z 5 rozliczen
    przy 95 nierozliczonych" i "trafnosc 60% ze 100 rozliczen" to dwa rozne
    zdania, a bez tej liczby wygladaja identycznie.

    POZYCJE ZWIJA LATERAL, a nie plaski JOIN. `exec.position.open_decision_id`
    nie ma UNIQUE, wiec jedna decyzja moze miec kilka zamknietych pozycji.
    Plaski JOIN zwielokrotnial przez to wiersz decyzji: `used_count` rosl
    ponad liczbe uzyc sygnalu, a `avg(dc.contribution)` wazyl decyzje liczba
    pozycji, ktore po niej powstaly. Sumujemy je w jeden wynik na decyzje -
    trafieniem jest decyzja zamknieta na plusie LACZNIE, nie kazda noga osobno.
    """
    rows = await fetch_all(
        """
        SELECT sd.name, sd.family,
               count(*)                       AS used_count,
               avg(dc.contribution)           AS avg_contribution,
               count(z.pnl)                   AS closed_count,
               avg(CASE WHEN z.pnl > 0 THEN 1.0 ELSE 0.0 END)
                   FILTER (WHERE z.pnl IS NOT NULL) AS hit_rate,
               sum(z.pnl)                     AS total_pnl
        FROM journal.decision_contribution dc
        JOIN journal.decision d ON d.id = dc.decision_id
        JOIN sig.signal_def   sd ON sd.id = dc.ref_id AND dc.kind = 'signal'
        LEFT JOIN LATERAL (
            SELECT sum(p.realized_pnl) AS pnl
            FROM exec.position p
            WHERE p.open_decision_id = d.id AND p.closed_at IS NOT NULL
        ) z ON true
        WHERE d.engine::text = %s
        GROUP BY sd.name, sd.family
        ORDER BY count(*) DESC
        """,
        (engine,),
    )
    return [
        {
            "name": r["name"], "family": r["family"], "used_count": r["used_count"],
            "avg_contribution": float(r["avg_contribution"]) if r["avg_contribution"] is not None else None,
            "hit_rate": float(r["hit_rate"]) if r["hit_rate"] is not None else None,
            # Rozliczone i nierozliczone PODAJEMY WPROST. Bez nich `hit_rate`
            # jest liczba bez mianownika, a front nie ma jak odroznic sygnalu
            # slabego od sygnalu jeszcze niezmierzonego.
            "closed_count": r["closed_count"],
            "unsettled_count": r["used_count"] - r["closed_count"],
            # Brak zamknietej pozycji to BRAK WYNIKU, nie wynik zerowy.
            # Zero w tej kolumnie znaczylo "sygnal nic nie zarobil", a znaczylo
            # "nie ma z czego policzyc".
            "total_pnl": float(r["total_pnl"]) if r["total_pnl"] is not None else None,
        }
        for r in rows
    ]


@router.get("/postmortems")
async def postmortems(engine: Silnik, limit: int = Query(5, le=50)):
    """Dziennik jakosciowy generowany przez Claude z zamknietych transakcji."""
    rows = await fetch_all(
        """
        SELECT id, period_from, period_to, model, summary, findings, created_at
        FROM learn.postmortem WHERE engine::text = %s
        ORDER BY created_at DESC LIMIT %s
        """,
        (engine, limit),
    )
    return [
        {
            "id": r["id"], "period_from": r["period_from"].isoformat(),
            "period_to": r["period_to"].isoformat(), "model": r["model"],
            "summary": r["summary"], "findings": r["findings"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]
