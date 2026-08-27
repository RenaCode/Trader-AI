-- 0008: dane startowe — venues, instrumenty (waski zakres fazy 1), feedy RSS, limity ryzyka

INSERT INTO ref.venue (code, name, engine, timezone, is_24_7, demo_host, live_host) VALUES
    ('bybit', 'Bybit',               'crypto', 'UTC',              true,
     'https://api-demo.bybit.com', 'https://api.bybit.com'),
    ('ibkr',  'Interactive Brokers', 'equity', 'America/New_York', false,
     'https://localhost:5000/v1/api', 'https://localhost:5000/v1/api')
ON CONFLICT (code) DO NOTHING;

-- Faza 1: waski zakres. Walk-forward na malej liczbie instrumentow jest uczciwszy
-- statystycznie niz skanowanie calego rynku (mniej okazji do przeuczenia selekcja).
INSERT INTO ref.instrument (venue_id, symbol, engine, base_ccy, quote_ccy, kind, tick_size, lot_size)
SELECT v.id, i.symbol, 'crypto', i.base, 'USDT', 'spot', i.tick, i.lot
FROM ref.venue v,
     (VALUES ('BTCUSDT','BTC',0.01,0.000001),
             ('ETHUSDT','ETH',0.01,0.00001),
             ('SOLUSDT','SOL',0.001,0.001)) AS i(symbol, base, tick, lot)
WHERE v.code = 'bybit'
ON CONFLICT (venue_id, symbol) DO NOTHING;

INSERT INTO ref.instrument (venue_id, symbol, engine, quote_ccy, kind, tick_size, lot_size)
SELECT v.id, i.symbol, 'equity', 'USD', i.kind, 0.01, 1
FROM ref.venue v,
     (VALUES ('SPY','etf'), ('AAPL','stock')) AS i(symbol, kind)
WHERE v.code = 'ibkr'
ON CONFLICT (venue_id, symbol) DO NOTHING;

-- ---------------------------------------------------------------- feedy RSS
-- Dashboard pokazuje wprost, z ktorych zrodel korzysta system i ktory artykul
-- wspoltworzyl ktora decyzje.
INSERT INTO news.feed (name, url, engine, poll_interval_s) VALUES
    ('CoinDesk',         'https://www.coindesk.com/arc/outboundfeeds/rss/',        'crypto', 300),
    ('Cointelegraph',    'https://cointelegraph.com/rss',                          'crypto', 300),
    ('Decrypt',          'https://decrypt.co/feed',                                'crypto', 600),
    ('NewsBTC',          'https://www.newsbtc.com/feed/',                          'crypto', 600),
    ('CryptoPotato',     'https://cryptopotato.com/feed/',                         'crypto', 600),
    ('Bitcoin Magazine', 'https://bitcoinmagazine.com/feed',                       'crypto', 900),
    ('MarketWatch Top',  'https://feeds.content.dowjones.io/public/rss/mw_topstories', 'equity', 300),
    ('SEC EDGAR 8-K',    'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=8-K&output=atom', 'equity', 900),
    ('Federal Reserve',  'https://www.federalreserve.gov/feeds/press_all.xml',      NULL,     900),
    ('ECB Press',        'https://www.ecb.europa.eu/rss/press.html',                NULL,     900)
ON CONFLICT (url) DO NOTHING;

-- ---------------------------------------------------------------- limity ryzyka
-- Wartosci startowe, konserwatywne. Egzekwowane w kodzie risk kernela.
INSERT INTO risk.limit_def (engine, mode, name, scope, value, unit, action) VALUES
    ('crypto','demo','max_daily_loss',         'engine', 2.0,     'pct',       'killswitch'),
    ('crypto','demo','max_drawdown',           'engine', 10.0,    'pct',       'killswitch'),
    ('crypto','demo','max_position_notional',  'engine', 5000,    'quote_ccy', 'downsize'),
    ('crypto','demo','max_total_exposure',     'engine', 15000,   'quote_ccy', 'block'),
    ('crypto','demo','max_orders_per_hour',    'engine', 20,      'count',     'block'),
    ('equity','demo','max_daily_loss',         'engine', 2.0,     'pct',       'killswitch'),
    ('equity','demo','max_drawdown',           'engine', 10.0,    'pct',       'killswitch'),
    ('equity','demo','max_position_notional',  'engine', 5000,    'quote_ccy', 'downsize'),
    ('equity','demo','max_total_exposure',     'engine', 15000,   'quote_ccy', 'block'),
    ('equity','demo','max_orders_per_hour',    'engine', 10,      'count',     'block')
ON CONFLICT (engine, mode, name, scope, instrument_id) DO NOTHING;

-- ---------------------------------------------------------------- stan uczenia
INSERT INTO learn.learning_state (engine, status, reason) VALUES
    ('crypto','warmup','Brak danych historycznych - zbieranie w toku'),
    ('equity','warmup','Brak danych historycznych - zbieranie w toku')
ON CONFLICT (engine) DO NOTHING;

INSERT INTO risk.engine_heartbeat (engine, mode, last_beat_at, status) VALUES
    ('crypto','demo', now(), 'halted'),
    ('equity','demo', now(), 'halted')
ON CONFLICT (engine) DO NOTHING;
