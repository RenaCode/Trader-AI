-- 0001: rozszerzenia, role, dane referencyjne
-- Bitemporalność: każda tabela faktów ma event_time (kiedy zaszło) i ingested_at (kiedy wiemy).

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS news;
CREATE SCHEMA IF NOT EXISTS feat;
CREATE SCHEMA IF NOT EXISTS sig;
CREATE SCHEMA IF NOT EXISTS journal;
CREATE SCHEMA IF NOT EXISTS exec;
CREATE SCHEMA IF NOT EXISTS learn;
CREATE SCHEMA IF NOT EXISTS risk;
CREATE SCHEMA IF NOT EXISTS app;

-- Silnik = jedna z dwóch niezależnych maszyn decyzyjnych (dwie zakładki w UI).
CREATE TYPE ref.engine     AS ENUM ('crypto', 'equity');
CREATE TYPE ref.trade_mode AS ENUM ('demo', 'live');

-- ---------------------------------------------------------------- venues
CREATE TABLE ref.venue (
    id           smallserial PRIMARY KEY,
    code         text        NOT NULL UNIQUE,          -- 'bybit', 'ibkr'
    name         text        NOT NULL,
    engine       ref.engine  NOT NULL,
    timezone     text        NOT NULL DEFAULT 'UTC',
    is_24_7      boolean     NOT NULL DEFAULT false,
    demo_host    text,
    live_host    text,
    meta         jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------- instruments
CREATE TABLE ref.instrument (
    id            serial      PRIMARY KEY,
    venue_id      smallint    NOT NULL REFERENCES ref.venue(id),
    symbol        text        NOT NULL,                -- 'BTCUSDT', 'AAPL'
    engine        ref.engine  NOT NULL,
    base_ccy      text,
    quote_ccy     text        NOT NULL,
    kind          text        NOT NULL,                -- 'spot','perp','stock','etf'
    tick_size     numeric(28,12),
    lot_size      numeric(28,12),
    min_notional  numeric(28,12),
    active        boolean     NOT NULL DEFAULT true,
    meta          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (venue_id, symbol)
);

CREATE INDEX instrument_engine_active_idx ON ref.instrument (engine) WHERE active;

-- ---------------------------------------------------------------- kalendarz sesji
-- Tylko dla equity. Silnik crypto ignoruje tę tabelę (is_24_7 = true).
CREATE TABLE ref.session_calendar (
    venue_id    smallint    NOT NULL REFERENCES ref.venue(id),
    session_day date        NOT NULL,
    open_at     timestamptz,
    close_at    timestamptz,
    is_holiday  boolean     NOT NULL DEFAULT false,
    note        text,
    PRIMARY KEY (venue_id, session_day)
);

-- ---------------------------------------------------------------- akcje korporacyjne
-- Split/dywidenda niepoprawnie zaksięgowana potrafi zniszczyć cały szereg cen
-- i wygenerować fałszywy sygnał. Trzymamy je jawnie, żeby dało się je odjąć.
CREATE TABLE ref.corporate_action (
    id            bigserial   PRIMARY KEY,
    instrument_id integer     NOT NULL REFERENCES ref.instrument(id),
    kind          text        NOT NULL,                -- 'split','dividend','merger'
    ex_date       date        NOT NULL,
    ratio         numeric(20,10),
    amount        numeric(28,12),
    currency      text,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instrument_id, kind, ex_date)
);
