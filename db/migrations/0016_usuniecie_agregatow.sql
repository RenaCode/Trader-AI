-- 0016: usunięcie nieużywanych agregatów ciągłych
--
-- Migracja 0002 utworzyła `market.candle_5m`, `candle_1h` i `candle_1d` jako
-- agregaty ciągłe liczone ze świec minutowych, z komentarzem "jedno źródło
-- prawdy, brak rozjazdu".
--
-- Od tamtej pory potok poszedł inną drogą i komentarz przestał opisywać
-- rzeczywistość:
--
--   * świece godzinowe pobieramy WPROST z giełdy (`market.candle`
--     z `timeframe='1h'`, 809 tys. wierszy) — to bar wyliczony przez samą
--     giełdę, a więc bardziej miarodajny niż nasza agregacja,
--   * świece 4-godzinne liczymy z tych godzinowych w locie,
--   * świece 5-minutowe liczy `features.py` z minutowych w locie.
--
-- Żaden fragment kodu aplikacji nie odczytuje tych trzech widoków — sprawdzone
-- przeszukaniem całego repozytorium. Zostały więc dwa źródła tej samej prawdy,
-- czyli dokładnie to, przed czym ostrzegał komentarz w 0002, plus polityki
-- odświeżania chodzące co minutę dla danych, których nikt nie czyta.
--
-- Zmierzona zgodność obu źródeł przed usunięciem: średnia różnica zamknięcia
-- 0,000006%, maksymalna 0,033%. Nie tracimy więc niczego poza redundancją.
--
-- Świec 15-minutowych (25 920 wierszy) NIE usuwamy. Też są nieużywane, ale to
-- prawdziwe obserwacje rynku, kosztują około 2 MB, a skasowanie realnych danych
-- dla porządku nie daje nic w zamian.

DROP MATERIALIZED VIEW IF EXISTS market.candle_5m CASCADE;
DROP MATERIALIZED VIEW IF EXISTS market.candle_1h CASCADE;
DROP MATERIALIZED VIEW IF EXISTS market.candle_1d CASCADE;
