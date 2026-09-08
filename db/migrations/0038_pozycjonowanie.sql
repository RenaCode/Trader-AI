-- Pozycjonowanie tłumu na perpetualach: udział kont po stronie długiej oraz
-- otwarte pozycje.
--
-- SKĄD TEN POMYSŁ. Pytanie brzmiało „czy kopiować ruchy najlepszych traderów".
-- Kopiować się nie da i nie warto: Bybit nie wystawia copy tradingu w API v5
-- (404 na wszystkich hostach), podmiot unijny, na którym handlujemy, nie ma
-- perpetuali w ogóle, a ranking „najlepszych" to prawy ogon rozkładu — przy
-- dziesiątkach tysięcy uczestników czołówka trzymiesięcznego ROI jest
-- zdominowana przez szczęście pomnożone przez dźwignię. Wybór po fakcie, bez
-- próby out-of-sample, to dokładnie ten błąd, przeciwko któremu zbudowany jest
-- cały ten system.
--
-- Wartościowa jest natomiast ZAGREGOWANA informacja o tym, jak ustawiony jest
-- tłum — bez wybierania zwycięzców. To ta sama rodzina co `market.funding_rate`,
-- która jest tu od migracji 0021 i której uzasadnienie brzmi: „jedyne źródło
-- w całym zestawie, które nie jest przetworzoną ceną, i dlatego jako jedyne
-- może wnieść informację, której w cenie jeszcze nie ma".
--
-- DWA ŹRÓDŁA W JEDNEJ TABELI, obie kolumny NULLOWALNE. Pochodzą z osobnych
-- wywołań (`/v5/market/account-ratio` i `/v5/market/open-interest`), które mogą
-- zawieść niezależnie, ale mają wspólny klucz: ten sam perpetual i ta sama
-- godzinowa siatka. Dwie bliźniacze tabele oznaczałyby dwa razy tę samą
-- logikę uzupełniania luk; NULL mówi wprost „tego źródła nie mamy dla tej
-- godziny" i nie udaje zera.
--
-- INSTRUMENT WSKAZUJE NA PERPETUAL, nie na spot — tak samo jak przy
-- finansowaniu. Instrument spotowy ma w `meta.funding_from` jego id i tą samą
-- drogą dziedziczy pozycjonowanie. Udawanie, że spot ma otwarte pozycje,
-- byłoby zapisaniem nieprawdy do bazy, którą potem ktoś przeczyta jako fakt.

CREATE TABLE IF NOT EXISTS market.positioning (
    instrument_id   bigint      NOT NULL REFERENCES ref.instrument(id),
    event_time      timestamptz NOT NULL,
    -- Udział kont po stronie długiej, 0..1. NIE jest to udział KAPITAŁU:
    -- Bybit liczy konta, więc jeden duży gracz waży tyle samo co jeden mały.
    -- Zapisane wprost, bo przy interpretacji tej liczby to jest istotne.
    long_ratio      numeric(10,6),
    -- Otwarte pozycje w kontraktach. Poziom nie jest porównywalny w czasie
    -- ani między instrumentami — informacją jest ZMIANA, i to ona pójdzie
    -- do cechy.
    open_interest   numeric(28,8),
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, event_time)
);

CREATE INDEX IF NOT EXISTS positioning_time_idx
    ON market.positioning (instrument_id, event_time DESC);

COMMENT ON TABLE market.positioning IS
    'Zagregowane pozycjonowanie tłumu na perpetualu (udział kont długich, '
    'otwarte pozycje). Źródło informacji o zachowaniu innych uczestników BEZ '
    'wybierania zwycięzców po fakcie — patrz uzasadnienie w migracji 0038.';
