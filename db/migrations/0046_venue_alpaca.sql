-- 0046: Alpaca Markets jako giełda silnika `equity` (konto paper).
--
-- Seed 0008 wpisał `ibkr` z hostem `localhost:5000` — to był placeholder pod
-- IB Gateway, którego nigdy nie uruchomiono. Decyzja właściciela 2026-09-22:
-- giełda tradycyjna startuje na Alpaca (klucz API, REST, konto paper od
-- pierwszego dnia), IBKR ewentualnie później jako trzeci adapter.
-- `ibkr` zostaje w tabeli — usunięcie wiersza z kluczem obcym w
-- `app.api_credential` nie jest sprawą migracji — ale przestaje być jedyną
-- giełdą equity.
--
-- `is_24_7 = false` i `timezone = America/New_York` nie są ozdobą: silnik
-- equity pyta o sesję (`/v2/clock`), a świece mają dziury nocą i w weekendy.
INSERT INTO ref.venue (code, name, engine, timezone, is_24_7, demo_host, live_host, meta) VALUES
    ('alpaca', 'Alpaca Markets', 'equity', 'America/New_York', false,
     'https://paper-api.alpaca.markets', 'https://api.alpaca.markets',
     '{"data_host": "https://data.alpaca.markets", "feed": "iex", "prowizja": "0"}'::jsonb)
ON CONFLICT (code) DO NOTHING;
