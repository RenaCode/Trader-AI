-- 0047: uniwersum equity na Alpaca (paper) — 20 płynnych spółek USA.
--
-- DLACZEGO DWADZIEŚCIA, NIE „CAŁY RYNEK". Ten sam argument co w seedzie 0008
-- dla krypto: walk-forward na wąskim, płynnym uniwersum jest statystycznie
-- uczciwszy (mniej okazji do przeuczenia selekcją) i tańszy w danych (feed
-- IEX w planie darmowym). Dobór: największe, najpłynniejsze spółki z S&P 500
-- z kilku sektorów, bez ETF-ów (SPY zostaje jako odniesienie kup-i-trzymaj,
-- `tradeable = false`).
--
-- `tradeable = false` NA START DLA WSZYSTKICH: silnik equity jeszcze nie
-- istnieje, a historia zbiera się dla `active = true` niezależnie od tej flagi
-- (0020). Włączenie do uniwersum decyzyjnego to osobna, świadoma migracja po
-- pierwszym walk-forwardzie — nie skutek uboczny dodania instrumentu.
--
-- Placeholderowe instrumenty na `ibkr` (0008: SPY, AAPL) idą w `active = false`,
-- żeby dwa wiersze `AAPL` (ibkr + alpaca) nie liczyły się podwójnie w UI
-- ani w workerach; wiersze zostają ze względu na klucze obce.
UPDATE ref.instrument SET active = false, tradeable = false
 WHERE venue_id = (SELECT id FROM ref.venue WHERE code = 'ibkr');

INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind, tick_size, lot_size,
                            min_notional, tradeable, meta)
SELECT v.id, i.symbol, 'equity', 'USD', i.kind, 0.01, 1, 1, false,
       jsonb_build_object('sektor', i.sektor, 'feed', 'iex')
FROM ref.venue v,
     (VALUES
        ('AAPL',  'stock', 'technologia'),
        ('MSFT',  'stock', 'technologia'),
        ('NVDA',  'stock', 'technologia'),
        ('AVGO',  'stock', 'technologia'),
        ('AMZN',  'stock', 'konsumpcja'),
        ('GOOGL', 'stock', 'komunikacja'),
        ('META',  'stock', 'komunikacja'),
        ('NFLX',  'stock', 'komunikacja'),
        ('TSLA',  'stock', 'konsumpcja'),
        ('COST',  'stock', 'konsumpcja'),
        ('HD',    'stock', 'konsumpcja'),
        ('JPM',   'stock', 'finanse'),
        ('V',     'stock', 'finanse'),
        ('MA',    'stock', 'finanse'),
        ('UNH',   'stock', 'zdrowie'),
        ('LLY',   'stock', 'zdrowie'),
        ('JNJ',   'stock', 'zdrowie'),
        ('XOM',   'stock', 'energia'),
        ('PG',    'stock', 'dobra_podstawowe'),
        ('CAT',   'stock', 'przemysl'),
        ('SPY',   'etf',   'odniesienie')
     ) AS i(symbol, kind, sektor)
WHERE v.code = 'alpaca'
ON CONFLICT (venue_id, symbol) DO NOTHING;
