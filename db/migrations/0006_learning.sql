-- 0006: rejestr modeli, walk-forward, kalibracja, dryf, uczciwe metryki
--
-- Ten schemat jest zaprojektowany tak, zeby NIE DALO SIE latwo pokazac zawyzonego wyniku:
--   - metryki zyja wylacznie w oknach walk-forward (wf_window), nie ma tabeli "wynik backtestu"
--   - kazda przetestowana konfiguracja laduje w hypothesis_trial (licznik do deflated Sharpe)
--   - kalibracja i dryf sa osobnymi, jawnymi wymiarami oceny

CREATE TYPE learn.mv_status    AS ENUM ('training','candidate','active','retired','failed');
CREATE TYPE learn.state_status AS ENUM ('warmup','learning','validated','degraded','halted');

CREATE TABLE learn.model (
    id          serial      PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    engine      ref.engine  NOT NULL,
    kind        text        NOT NULL,               -- 'signal_weighting','regime_classifier'
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE learn.model_version (
    id              serial      PRIMARY KEY,
    model_id        integer     NOT NULL REFERENCES learn.model(id),
    version         integer     NOT NULL,
    status          learn.mv_status NOT NULL DEFAULT 'training',
    params          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- Na jakich WERSJACH cech i sygnalow byl trenowany. Bez tego porownywanie
    -- metryk dwoch wersji modelu jest bez sensu, gdy w miedzyczasie zmienila sie cecha.
    feature_def_ids integer[]   NOT NULL DEFAULT '{}',
    signal_def_ids  integer[]   NOT NULL DEFAULT '{}',
    artifact_uri    text,
    git_sha         text,
    trained_from    timestamptz,
    trained_to      timestamptz,
    promoted_at     timestamptz,
    retired_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_id, version)
);

-- Tylko jedna wersja modelu moze byc aktywna naraz.
CREATE UNIQUE INDEX model_version_one_active_idx
    ON learn.model_version (model_id) WHERE status = 'active';

-- Odroczone klucze obce z 0004/0005 (kolejnosc tworzenia tabel).
ALTER TABLE feat.regime   ADD CONSTRAINT regime_model_version_fk
    FOREIGN KEY (model_version_id) REFERENCES learn.model_version(id);
ALTER TABLE journal.decision  ADD CONSTRAINT decision_model_version_fk
    FOREIGN KEY (model_version_id) REFERENCES learn.model_version(id);

CREATE TABLE learn.training_run (
    id               bigserial   PRIMARY KEY,
    model_version_id integer     NOT NULL REFERENCES learn.model_version(id),
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    status           text        NOT NULL DEFAULT 'running',
    train_from       timestamptz NOT NULL,
    train_to         timestamptz NOT NULL,
    n_windows        integer,
    error            text,
    log              text
);

-- ---------------------------------------------------------------- walk-forward
-- Jedyny dopuszczalny protokol oceny. Zwykla k-fold jest niewazna na szeregach
-- czasowych; strategia musi udowadniac sie wielokrotnie, w roznych rezimach.
CREATE TABLE learn.wf_window (
    id              bigserial   PRIMARY KEY,
    training_run_id bigint      NOT NULL REFERENCES learn.training_run(id) ON DELETE CASCADE,
    idx             integer     NOT NULL,
    train_from      timestamptz NOT NULL,
    train_to        timestamptz NOT NULL,
    test_from       timestamptz NOT NULL,
    test_to         timestamptz NOT NULL,
    n_trades        integer,
    dominant_regime text,
    CHECK (train_to <= test_from),                  -- zero nakladania sie okien
    UNIQUE (training_run_id, idx)
);

-- Metryki WYLACZNIE out-of-sample (z okresu test_from..test_to danego okna).
CREATE TABLE learn.wf_metric (
    wf_window_id bigint NOT NULL REFERENCES learn.wf_window(id) ON DELETE CASCADE,
    metric       text   NOT NULL,                   -- 'sharpe','sortino','max_dd','hit_rate','avg_r','profit_factor'
    value        double precision NOT NULL,
    PRIMARY KEY (wf_window_id, metric)
);

-- ---------------------------------------------------------------- uczciwosc
-- Licznik WSZYSTKICH przetestowanych konfiguracji. Wysokie wyniki backtestu sa
-- osiagalne po przetestowaniu niewielu konfiguracji — bez tego licznika deflated
-- Sharpe jest nieobliczalny i sami siebie oszukamy selekcja.
CREATE TABLE learn.hypothesis_trial (
    id           bigserial   PRIMARY KEY,
    model_id     integer     NOT NULL REFERENCES learn.model(id),
    config_hash  bytea       NOT NULL,
    params       jsonb       NOT NULL,
    oos_sharpe   double precision,
    n_windows    integer,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_id, config_hash)
);

-- Kalibracja: czy pewnosc 70% oznacza trafnosc ~70%.
CREATE TABLE learn.calibration_bin (
    model_version_id integer     NOT NULL REFERENCES learn.model_version(id) ON DELETE CASCADE,
    evaluated_at     timestamptz NOT NULL,
    bin_lo           double precision NOT NULL,
    bin_hi           double precision NOT NULL,
    predicted_mean   double precision NOT NULL,
    observed_rate    double precision NOT NULL,
    n_samples        integer     NOT NULL,
    PRIMARY KEY (model_version_id, evaluated_at, bin_lo)
);

-- Dryf rozkladu cech: trening vs terazniejszosc. Male przesuniecie dynamiki rynku
-- potrafi zniszczyc wydajnosc, wiec mierzymy je jawnie zamiast czekac na strate.
CREATE TABLE learn.drift_metric (
    model_version_id integer     NOT NULL REFERENCES learn.model_version(id) ON DELETE CASCADE,
    feature_def_id   integer     NOT NULL REFERENCES feat.feature_def(id),
    event_time       timestamptz NOT NULL,
    psi              double precision,              -- Population Stability Index
    ks_stat          double precision,
    PRIMARY KEY (model_version_id, feature_def_id, event_time)
);

-- ---------------------------------------------------------------- stan uczenia
-- To jest "stopien, jak sie uczy algorytm" w dashboardzie — jako status + liczby,
-- ktore nie klamia, a nie jako pasek postepu.
CREATE TABLE learn.learning_state (
    engine            ref.engine  PRIMARY KEY,
    status            learn.state_status NOT NULL DEFAULT 'warmup',
    model_version_id  integer     REFERENCES learn.model_version(id),
    windows_total     integer     NOT NULL DEFAULT 0,
    windows_positive  integer     NOT NULL DEFAULT 0,
    median_oos_sharpe double precision,
    max_drawdown      double precision,
    calibration_error double precision,             -- ECE
    max_psi           double precision,
    trials_run        integer     NOT NULL DEFAULT 0,
    reason            text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Post-mortem generowany przez Claude z zamknietych transakcji.
-- Zapisujemy jako dziennik jakosciowy — NIE karmimy tym automatycznie wag modelu,
-- bo to byloby sprzezenie zwrotne na anegdotach.
CREATE TABLE learn.postmortem (
    id          bigserial   PRIMARY KEY,
    engine      ref.engine  NOT NULL,
    period_from timestamptz NOT NULL,
    period_to   timestamptz NOT NULL,
    model       text        NOT NULL,
    summary     text        NOT NULL,
    findings    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    position_ids bigint[]   NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);
