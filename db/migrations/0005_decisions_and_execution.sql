-- 0005: dziennik decyzji (append-only) + egzekucja
--
-- journal.decision jest NIEMUTOWALNA. Zapisujemy ją PRZED poznaniem wyniku, razem
-- z pelnym wektorem cech, ktory system widzial. To jednoczesnie:
--   - zrodlo zakladki "Ruchy" (dlaczego? -> journal.decision_contribution)
--   - dataset treningowy wolny od leakage'u z definicji
--   - slad audytowy i podatkowy

CREATE TYPE journal.action       AS ENUM ('enter_long','enter_short','scale_in','scale_out','exit','hold');
CREATE TYPE journal.risk_verdict AS ENUM ('allow','downsize','block');

CREATE TABLE journal.decision (
    id               bigserial   PRIMARY KEY,
    engine           ref.engine  NOT NULL,
    mode             ref.trade_mode NOT NULL,
    instrument_id    integer     NOT NULL REFERENCES ref.instrument(id),
    decided_at       timestamptz NOT NULL DEFAULT now(),
    action           journal.action  NOT NULL,
    target_qty       numeric(28,12),
    target_price     numeric(28,12),
    confidence       double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    regime           text,
    model_version_id integer,                        -- FK dodany w 0006
    -- Snapshot dokladnie tego, co model widzial. Redundantny wzgledem feat.feature_value
    -- i to jest celowe: gdy definicja cechy sie zmieni albo dane zostana poprawione
    -- wstecznie, ten wiersz nadal mowi prawde o tamtej chwili.
    feature_snapshot jsonb       NOT NULL,
    risk_verdict     journal.risk_verdict NOT NULL,
    risk_reason      text,
    rationale        text,                           -- uzasadnienie czytelne dla czlowieka
    rationale_model  text,
    latency_ms       integer,
    UNIQUE (engine, instrument_id, decided_at)
);

CREATE INDEX decision_engine_time_idx ON journal.decision (engine, decided_at DESC);
CREATE INDEX decision_instrument_idx  ON journal.decision (instrument_id, decided_at DESC);

-- Twarda niemutowalnosc. Bez tego "poprawimy sobie jeden wiersz" konczy sie
-- datasetem, ktoremu nie mozna ufac.
CREATE FUNCTION journal.forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'journal.% jest append-only (proba: %)', TG_TABLE_NAME, TG_OP;
END;
$fn$;

CREATE TRIGGER decision_immutable
    BEFORE UPDATE OR DELETE ON journal.decision
    FOR EACH ROW EXECUTE FUNCTION journal.forbid_mutation();

-- Rozbicie decyzji na wklady: TO odpowiada na "dlaczego" w dashboardzie.
CREATE TABLE journal.decision_contribution (
    decision_id  bigint  NOT NULL REFERENCES journal.decision(id) ON DELETE CASCADE,
    kind         text    NOT NULL CHECK (kind IN ('feature','signal','news')),
    ref_id       bigint  NOT NULL,                  -- feature_def_id | signal_def_id | article_id
    label        text    NOT NULL,
    value        double precision,
    weight       double precision NOT NULL,
    contribution double precision NOT NULL,         -- value * weight, znormalizowane
    PRIMARY KEY (decision_id, kind, ref_id)
);

CREATE TRIGGER decision_contribution_immutable
    BEFORE UPDATE OR DELETE ON journal.decision_contribution
    FOR EACH ROW EXECUTE FUNCTION journal.forbid_mutation();

-- Zlecenia PLANOWANE — zrodlo sekcji "co system planuje" w UI.
CREATE TABLE journal.planned_order (
    id            bigserial   PRIMARY KEY,
    decision_id   bigint      NOT NULL REFERENCES journal.decision(id),
    instrument_id integer     NOT NULL REFERENCES ref.instrument(id),
    side          text        NOT NULL CHECK (side IN ('buy','sell')),
    qty           numeric(28,12) NOT NULL,
    order_type    text        NOT NULL,             -- 'market','limit','stop'
    limit_price   numeric(28,12),
    trigger       jsonb       NOT NULL,             -- warunki wejscia, opisowo dla UI
    valid_until   timestamptz,
    status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','triggered','cancelled','expired')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at   timestamptz
);

CREATE INDEX planned_order_pending_idx ON journal.planned_order (instrument_id)
    WHERE status = 'pending';

-- ---------------------------------------------------------------- egzekucja
CREATE TABLE exec.order (
    id               bigserial   PRIMARY KEY,
    decision_id      bigint      REFERENCES journal.decision(id),
    planned_order_id bigint      REFERENCES journal.planned_order(id),
    engine           ref.engine  NOT NULL,
    mode             ref.trade_mode NOT NULL,
    instrument_id    integer     NOT NULL REFERENCES ref.instrument(id),
    venue_order_id   text,
    client_order_id  text        NOT NULL,          -- nasz idempotency key
    side             text        NOT NULL CHECK (side IN ('buy','sell')),
    order_type       text        NOT NULL,
    qty              numeric(28,12) NOT NULL,
    limit_price      numeric(28,12),
    status           text        NOT NULL,          -- 'new','partial','filled','cancelled','rejected'
    reject_reason    text,
    submitted_at     timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    raw              jsonb,
    UNIQUE (mode, client_order_id)
);

CREATE INDEX order_open_idx ON exec.order (instrument_id, status)
    WHERE status IN ('new','partial');

CREATE TABLE exec.fill (
    id            bigserial   PRIMARY KEY,
    order_id      bigint      NOT NULL REFERENCES exec.order(id),
    venue_fill_id text,
    qty           numeric(28,12) NOT NULL,
    price         numeric(28,12) NOT NULL,
    fee           numeric(28,12) NOT NULL DEFAULT 0,
    fee_ccy       text,
    event_time    timestamptz NOT NULL,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (order_id, venue_fill_id)
);

CREATE TABLE exec.position (
    id             bigserial   PRIMARY KEY,
    engine         ref.engine  NOT NULL,
    mode           ref.trade_mode NOT NULL,
    instrument_id  integer     NOT NULL REFERENCES ref.instrument(id),
    side           text        NOT NULL CHECK (side IN ('long','short')),
    qty            numeric(28,12) NOT NULL,
    avg_price      numeric(28,12) NOT NULL,
    opened_at      timestamptz NOT NULL,
    closed_at      timestamptz,
    realized_pnl   numeric(28,12) NOT NULL DEFAULT 0,
    fees_paid      numeric(28,12) NOT NULL DEFAULT 0,
    open_decision_id  bigint REFERENCES journal.decision(id),
    close_decision_id bigint REFERENCES journal.decision(id)
);

CREATE INDEX position_open_idx ON exec.position (engine, mode, instrument_id)
    WHERE closed_at IS NULL;

-- Krzywa kapitalu — zrodlo wykresu equity w dashboardzie.
CREATE TABLE exec.equity_point (
    engine       ref.engine  NOT NULL,
    mode         ref.trade_mode NOT NULL,
    event_time   timestamptz NOT NULL,
    equity       numeric(28,12) NOT NULL,
    cash         numeric(28,12) NOT NULL,
    unrealized   numeric(28,12) NOT NULL DEFAULT 0,
    realized_cum numeric(28,12) NOT NULL DEFAULT 0,
    exposure     numeric(28,12) NOT NULL DEFAULT 0,
    PRIMARY KEY (engine, mode, event_time)
);
SELECT create_hypertable('exec.equity_point', 'event_time', chunk_time_interval => interval '30 days');
