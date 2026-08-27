-- 0012: ewidencja wydatków na API modelu + twardy limit dzienny
--
-- Do tej pory koszt zapisywał się wyłącznie przy udanej analizie
-- (news.article_analysis.cost_usd). Triaż taniego modelu — wywoływany dla KAŻDEGO
-- nowego artykułu, a więc kilkukrotnie częściej — nie był liczony w ogóle.
-- Raportowany koszt był z tego powodu systematycznie zaniżony, a nieudane
-- wywołania (odmowa, błąd po stronie modelu) nie zostawiały śladu, mimo że
-- są płatne.
--
-- Tabela jest też podstawą limitu: bez pojedynczego miejsca, w którym widać
-- sumę wydatków, "twardy stop po przekroczeniu" nie ma czego sprawdzać.

CREATE TABLE news.api_spend (
    id            bigserial   PRIMARY KEY,
    event_time    timestamptz NOT NULL DEFAULT now(),
    model         text        NOT NULL,
    purpose       text        NOT NULL,          -- 'triage' | 'analysis'
    input_tokens  integer     NOT NULL DEFAULT 0,
    cache_read    integer     NOT NULL DEFAULT 0,
    output_tokens integer     NOT NULL DEFAULT 0,
    cost_usd      numeric(12,6) NOT NULL,
    article_id    bigint      REFERENCES news.article(id) ON DELETE SET NULL,
    ok            boolean     NOT NULL DEFAULT true
);

CREATE INDEX api_spend_time_idx ON news.api_spend (event_time DESC);

-- Widok dobowy — używany przez limit i przez dashboard.
CREATE VIEW news.api_spend_daily AS
SELECT date_trunc('day', event_time) AS day,
       purpose,
       count(*)                       AS calls,
       sum(input_tokens)              AS input_tokens,
       sum(output_tokens)             AS output_tokens,
       round(sum(cost_usd), 4)        AS cost_usd
FROM news.api_spend
GROUP BY 1, 2;
