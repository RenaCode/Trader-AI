-- 0002: dane rynkowe (hypertables + ciągłe agregaty)

CREATE TABLE market.candle (
    instrument_id integer     NOT NULL REFERENCES ref.instrument(id),
    timeframe     text        NOT NULL DEFAULT '1m',
    event_time    timestamptz NOT NULL,
    open          numeric(28,12) NOT NULL,
    high          numeric(28,12) NOT NULL,
    low           numeric(28,12) NOT NULL,
    close         numeric(28,12) NOT NULL,
    volume        numeric(38,12) NOT NULL DEFAULT 0,
    trade_count   integer,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, timeframe, event_time)
);
SELECT create_hypertable('market.candle', 'event_time', chunk_time_interval => interval '7 days');

-- Snapshot orderbooka: trzymamy tylko zagregowane metryki + N poziomów.
-- Pełna głębokość tick-by-tick to inna klasa problemu (i inna baza).
CREATE TABLE market.book_snapshot (
    instrument_id integer     NOT NULL REFERENCES ref.instrument(id),
    event_time    timestamptz NOT NULL,
    mid           numeric(28,12) NOT NULL,
    spread        numeric(28,12) NOT NULL,
    bid_volume    numeric(38,12) NOT NULL,
    ask_volume    numeric(38,12) NOT NULL,
    imbalance     double precision NOT NULL,   -- (bidV - askV) / (bidV + askV)
    levels        jsonb,                        -- top-N: [{p,q}, ...]
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, event_time)
);
SELECT create_hypertable('market.book_snapshot', 'event_time', chunk_time_interval => interval '1 day');

-- Funding rate — tylko perpetuale (crypto).
CREATE TABLE market.funding_rate (
    instrument_id integer     NOT NULL REFERENCES ref.instrument(id),
    event_time    timestamptz NOT NULL,
    rate          double precision NOT NULL,
    next_at       timestamptz,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, event_time)
);
SELECT create_hypertable('market.funding_rate', 'event_time', chunk_time_interval => interval '30 days');

-- ---------------------------------------------------------------- agregaty
-- Wyższe timeframe'y liczone z 1m: jedno źródło prawdy, brak rozjazdu.
CREATE MATERIALIZED VIEW market.candle_5m
WITH (timescaledb.continuous) AS
SELECT
    instrument_id,
    time_bucket(interval '5 minutes', event_time) AS bucket,
    first(open, event_time)  AS open,
    max(high)                AS high,
    min(low)                 AS low,
    last(close, event_time)  AS close,
    sum(volume)              AS volume,
    sum(trade_count)         AS trade_count
FROM market.candle
WHERE timeframe = '1m'
GROUP BY instrument_id, bucket
WITH NO DATA;

CREATE MATERIALIZED VIEW market.candle_1h
WITH (timescaledb.continuous) AS
SELECT
    instrument_id,
    time_bucket(interval '1 hour', event_time) AS bucket,
    first(open, event_time)  AS open,
    max(high)                AS high,
    min(low)                 AS low,
    last(close, event_time)  AS close,
    sum(volume)              AS volume,
    sum(trade_count)         AS trade_count
FROM market.candle
WHERE timeframe = '1m'
GROUP BY instrument_id, bucket
WITH NO DATA;

CREATE MATERIALIZED VIEW market.candle_1d
WITH (timescaledb.continuous) AS
SELECT
    instrument_id,
    time_bucket(interval '1 day', event_time) AS bucket,
    first(open, event_time)  AS open,
    max(high)                AS high,
    min(low)                 AS low,
    last(close, event_time)  AS close,
    sum(volume)              AS volume,
    sum(trade_count)         AS trade_count
FROM market.candle
WHERE timeframe = '1m'
GROUP BY instrument_id, bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('market.candle_5m',
    start_offset => interval '3 days',  end_offset => interval '1 minute',
    schedule_interval => interval '1 minute');
SELECT add_continuous_aggregate_policy('market.candle_1h',
    start_offset => interval '30 days', end_offset => interval '1 minute',
    schedule_interval => interval '5 minutes');
SELECT add_continuous_aggregate_policy('market.candle_1d',
    start_offset => interval '365 days', end_offset => interval '1 hour',
    schedule_interval => interval '1 hour');

-- ---------------------------------------------------------------- kompresja i retencja
ALTER TABLE market.candle SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id, timeframe',
    timescaledb.compress_orderby   = 'event_time DESC'
);
SELECT add_compression_policy('market.candle', interval '7 days');

ALTER TABLE market.book_snapshot SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby   = 'event_time DESC'
);
SELECT add_compression_policy('market.book_snapshot', interval '1 day');
-- Orderbook to dane pomocnicze, nie audytowe — po 90 dniach lecą.
SELECT add_retention_policy('market.book_snapshot', interval '90 days');
