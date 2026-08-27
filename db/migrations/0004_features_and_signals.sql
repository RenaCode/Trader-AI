-- 0004: feature store (lean, w Postgresie) + sygnały
--
-- Gwarancje, które ta migracja wymusza schematem, nie dyscypliną:
--   1. point-in-time correctness  -> kolumny event_time + ingested_at, funkcja feat.as_of()
--   2. brak training/serving skew -> jedna definicja cechy (feature_def), wersjonowana
--   3. odtwarzalność              -> model zapisuje, na jakich feature_def_id był trenowany

-- ---------------------------------------------------------------- definicje cech
-- Zmiana logiki cechy = NOWY WIERSZ z podbitą wersją. Nigdy UPDATE.
-- Inaczej model wytrenowany w marcu i model z maja "widzą" tę samą nazwę cechy,
-- ale liczoną inaczej — i porównanie ich metryk jest bez sensu.
CREATE TABLE feat.feature_def (
    id          serial      PRIMARY KEY,
    name        text        NOT NULL,               -- 'rsi_14', 'book_imbalance_5m'
    version     integer     NOT NULL DEFAULT 1,
    engine      ref.engine,                         -- NULL = wspólna dla obu silników
    dtype       text        NOT NULL DEFAULT 'float',
    params      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    code_ref    text        NOT NULL,               -- 'features.momentum:rsi'
    git_sha     text,
    description text,
    active      boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE feat.feature_value (
    feature_def_id integer     NOT NULL REFERENCES feat.feature_def(id),
    instrument_id  integer     NOT NULL REFERENCES ref.instrument(id),
    event_time     timestamptz NOT NULL,
    value          double precision,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature_def_id, instrument_id, event_time)
);
SELECT create_hypertable('feat.feature_value', 'event_time', chunk_time_interval => interval '7 days');

ALTER TABLE feat.feature_value SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'feature_def_id, instrument_id',
    timescaledb.compress_orderby   = 'event_time DESC'
);
SELECT add_compression_policy('feat.feature_value', interval '30 days');

-- ---------------------------------------------------------------- reżim rynkowy
-- Wagi sygnałów są per-reżim. To bezpośrednia odpowiedź na fakt, że modele
-- przeuczają się do DYNAMIKI rynku, więc małe przesunięcie reżimu je psuje.
CREATE TABLE feat.regime (
    instrument_id    integer     NOT NULL REFERENCES ref.instrument(id),
    event_time       timestamptz NOT NULL,
    regime           text        NOT NULL CHECK (regime IN ('trend_up','trend_down','range','high_vol','news_shock')),
    confidence       double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    model_version_id integer,                       -- FK dodany w 0006 (kolejność tabel)
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, event_time)
);
SELECT create_hypertable('feat.regime', 'event_time', chunk_time_interval => interval '30 days');

-- ---------------------------------------------------------------- odczyt point-in-time
-- JEDYNA dozwolona ścieżka odczytu cech do treningu i do decyzji live.
-- Filtruje po OBU znacznikach czasu: zdarzenie musiało zajść ORAZ musieliśmy je znać.
--
-- Bez warunku `ingested_at <= as_of` model uczyłby się na newsach opublikowanych
-- o 10:00, ale zescrapowanych o 10:07 — przy decyzji z 10:03. Backtest wyszedłby
-- świetnie i byłby całkowicie fałszywy.
CREATE FUNCTION feat.as_of(
    p_instrument_id integer,
    p_as_of         timestamptz,
    p_feature_ids   integer[] DEFAULT NULL,
    p_max_staleness interval  DEFAULT interval '1 hour'
)
RETURNS TABLE (feature_def_id integer, feature_name text, value double precision, event_time timestamptz)
LANGUAGE sql STABLE AS $fn$
    SELECT DISTINCT ON (fv.feature_def_id)
           fv.feature_def_id,
           fd.name || '@' || fd.version,
           fv.value,
           fv.event_time
    FROM feat.feature_value fv
    JOIN feat.feature_def   fd ON fd.id = fv.feature_def_id
    WHERE fv.instrument_id = p_instrument_id
      AND fv.event_time  <= p_as_of
      AND fv.ingested_at <= p_as_of                    -- <-- to zdanie chroni cały projekt
      AND fv.event_time  >= p_as_of - p_max_staleness  -- nie karmimy modelu trupem
      AND (p_feature_ids IS NULL OR fv.feature_def_id = ANY(p_feature_ids))
    ORDER BY fv.feature_def_id, fv.event_time DESC;
$fn$;

COMMENT ON FUNCTION feat.as_of IS
'Point-in-time odczyt cech. Uzywaj TEJ funkcji zarowno w treningu jak i w silniku live -
osobna implementacja "na szybko" po stronie live to definicja training/serving skew.';

-- Analogiczny odczyt dla sygnałów newsowych, z tą samą regułą bitemporalną.
CREATE FUNCTION news.as_of(
    p_instrument_id integer,
    p_as_of         timestamptz,
    p_lookback      interval DEFAULT interval '24 hours'
)
RETURNS TABLE (
    article_id bigint, title text, url text, source text,
    direction text, magnitude double precision, confidence double precision,
    novelty double precision, relevance double precision, event_time timestamptz
)
LANGUAGE sql STABLE AS $fn$
    SELECT a.id, a.title, a.url, f.name,
           an.direction, an.magnitude, an.confidence, an.novelty,
           ai.relevance, a.event_time
    FROM news.article_analysis an
    JOIN news.article            a  ON a.id  = an.article_id
    JOIN news.feed               f  ON f.id  = a.feed_id
    JOIN news.article_instrument ai ON ai.article_id = a.id
    WHERE ai.instrument_id = p_instrument_id
      AND a.event_time   <= p_as_of
      AND an.ingested_at <= p_as_of                    -- analiza musiała być GOTOWA
      AND a.event_time   >= p_as_of - p_lookback
    ORDER BY a.event_time DESC;
$fn$;

-- ---------------------------------------------------------------- sygnały
-- Sygnał = pojedyncza, prosta, wytłumaczalna hipoteza. Uczenie kalibruje ich wagi,
-- nie odkrywa sygnałów od zera — dzięki temu dashboard może uczciwie pokazać "dlaczego".
CREATE TABLE sig.signal_def (
    id          serial      PRIMARY KEY,
    name        text        NOT NULL,
    version     integer     NOT NULL DEFAULT 1,
    engine      ref.engine  NOT NULL,
    family      text        NOT NULL,               -- 'momentum','mean_reversion','flow','news','seasonality'
    params      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    code_ref    text        NOT NULL,
    description text,
    enabled     boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE sig.signal_value (
    signal_def_id integer     NOT NULL REFERENCES sig.signal_def(id),
    instrument_id integer     NOT NULL REFERENCES ref.instrument(id),
    event_time    timestamptz NOT NULL,
    score         double precision NOT NULL CHECK (score BETWEEN -1 AND 1),
    confidence    double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    inputs        jsonb,                            -- z jakich cech powstał
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_def_id, instrument_id, event_time)
);
SELECT create_hypertable('sig.signal_value', 'event_time', chunk_time_interval => interval '7 days');
