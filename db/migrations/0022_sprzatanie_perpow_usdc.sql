-- 0022: usunięcie perpetuałów, których giełda nie ma
--
-- DLACZEGO ISTNIEJĄ
--
-- Proces `funding` wskazywał perpetuał doklejając `.P` do symbolu spotowego.
-- Po zarejestrowaniu par USDC (0021) utworzył więc wiersze BTCUSDC.P,
-- ADAUSDC.P i tak dalej — instrumenty, których na giełdzie nie ma. Zapytanie
-- o ich stawki kończyło się błędem „Symbol Invalid" i wywracało cały przebieg,
-- bo wyjątek szedł w górę pętli: jeden nowy instrument zatrzymywał pobieranie
-- finansowania dla wszystkich pozostałych.
--
-- Przyczyna jest naprawiona w kodzie — perpetuał wskazywany jest teraz po
-- AKTYWIE i kwotowany w USDT. Ta migracja sprząta wiersze, które zdążyły
-- powstać.
--
-- OSTROŻNIE, BO TO USUWANIE
--
-- Kasujemy wyłącznie perpetuały kwotowane w USDC, które NIE MAJĄ ani jednej
-- stawki finansowania i pod które nikt się nie podpina. Wiersz z danymi
-- albo z odwołaniem zostaje — wtedy zniknięcie byłoby cichą utratą, a nie
-- sprzątaniem.

DELETE FROM ref.instrument i
 WHERE i.kind = 'perp'
   AND i.quote_ccy = 'USDC'
   AND NOT EXISTS (SELECT 1 FROM market.funding f WHERE f.instrument_id = i.id)
   AND NOT EXISTS (SELECT 1 FROM market.candle  c WHERE c.instrument_id = i.id)
   AND NOT EXISTS (
        SELECT 1 FROM ref.instrument s
         WHERE (s.meta->>'funding_from')::int = i.id);
