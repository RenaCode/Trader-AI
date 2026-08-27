-- 0003: newsy, analiza przez Claude, embeddingi (pgvector)

CREATE TABLE news.feed (
    id              serial      PRIMARY KEY,
    name            text        NOT NULL,
    url             text        NOT NULL UNIQUE,
    engine          ref.engine,                        -- NULL = dotyczy obu (makro)
    enabled         boolean     NOT NULL DEFAULT true,
    poll_interval_s integer     NOT NULL DEFAULT 300,
    last_polled_at  timestamptz,
    last_status     text,                              -- 'ok' | 'http_500' | 'timeout' ...
    last_error      text,
    articles_seen   bigint      NOT NULL DEFAULT 0,
    articles_kept   bigint      NOT NULL DEFAULT 0,    -- ile przeszło filtr istotności
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE news.article (
    id            bigserial   PRIMARY KEY,
    feed_id       integer     NOT NULL REFERENCES news.feed(id),
    url           text        NOT NULL,
    title         text        NOT NULL,
    summary       text,
    body          text,
    author        text,
    lang          text,
    content_hash  bytea       NOT NULL,                -- sha256(tytuł + treść) — dedup
    event_time    timestamptz NOT NULL,                -- published_at
    ingested_at   timestamptz NOT NULL DEFAULT now(),  -- kiedy MY to zobaczyliśmy
    UNIQUE (content_hash)
);

CREATE INDEX article_event_time_idx  ON news.article (event_time DESC);
CREATE INDEX article_ingested_at_idx ON news.article (ingested_at DESC);
CREATE INDEX article_feed_idx        ON news.article (feed_id, ingested_at DESC);

-- Embedding do deduplikacji i liczenia `novelty`. 1024 wymiary.
-- pgvector jest produkcyjnie wystarczający do ~50-100 mln wektorów; my będziemy
-- mieć rząd 1e5 rocznie, więc HNSW w tym samym silniku wystarcza z zapasem.
CREATE TABLE news.article_embedding (
    article_id  bigint      PRIMARY KEY REFERENCES news.article(id) ON DELETE CASCADE,
    model       text        NOT NULL,
    embedding   vector(1024) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX article_embedding_hnsw_idx
    ON news.article_embedding USING hnsw (embedding vector_cosine_ops);

-- Wynik analizy przez model Anthropic. Structured outputs => sztywny kształt.
CREATE TABLE news.article_analysis (
    id             bigserial   PRIMARY KEY,
    article_id     bigint      NOT NULL REFERENCES news.article(id) ON DELETE CASCADE,
    model          text        NOT NULL,                -- 'claude-opus-5'
    prompt_version text        NOT NULL,
    direction      text        NOT NULL CHECK (direction IN ('bullish','bearish','neutral')),
    magnitude      double precision NOT NULL CHECK (magnitude BETWEEN 0 AND 1),
    horizon        text        NOT NULL CHECK (horizon IN ('minutes','hours','days','weeks')),
    confidence     double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    -- novelty: czy rynek już to wycenił. News bez nowości ma wartość zero.
    novelty        double precision NOT NULL CHECK (novelty BETWEEN 0 AND 1),
    reasoning      text,
    raw            jsonb       NOT NULL,
    input_tokens   integer,
    output_tokens  integer,
    cost_usd       numeric(12,6),
    event_time     timestamptz NOT NULL,                -- = article.event_time
    ingested_at    timestamptz NOT NULL DEFAULT now(),  -- kiedy analiza była gotowa
    UNIQUE (article_id, model, prompt_version)
);

CREATE INDEX article_analysis_ingested_idx ON news.article_analysis (ingested_at DESC);

-- Powiązanie artykuł -> instrument, z siłą związku.
CREATE TABLE news.article_instrument (
    article_id    bigint  NOT NULL REFERENCES news.article(id) ON DELETE CASCADE,
    instrument_id integer NOT NULL REFERENCES ref.instrument(id),
    relevance     double precision NOT NULL CHECK (relevance BETWEEN 0 AND 1),
    PRIMARY KEY (article_id, instrument_id)
);

CREATE INDEX article_instrument_inst_idx ON news.article_instrument (instrument_id);
