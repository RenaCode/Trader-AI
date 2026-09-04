-- 0018: konto giełdowe stoi na podmiocie unijnym, nie globalnym
--
-- DLACZEGO
--
-- Po MiCA Bybit rozdzielił się na dwa podmioty i użytkownik z EOG ma konto
-- wyłącznie na bybit.eu. To NIE jest inna domena tej samej giełdy — to inna
-- giełda, z własnymi hostami, własną księgą zleceń i własną listą instrumentów.
-- Klucz wystawiony przez bybit.eu dostaje na hoście globalnym odpowiedź
-- 10003 "API key is invalid", nieodróżnialną od zwykłego złego klucza.
--
-- Zmierzone 2026-09-04:
--
--     bybit.com   541 par spot, 399 par *USDT, 859 kontraktów wieczystych
--     bybit.eu    140 par spot,   4 pary *USDT,   0 kontraktów wieczystych
--
-- CO ZOSTAJE NIEZMIENIONE I DLACZEGO
--
-- Dane publiczne — świece, arkusz zleceń, finansowanie — nadal pochodzą
-- z hosta globalnego (services/exchanges/bybit.py, PUBLIC_HOST). Tam jest
-- księga, na której model był trenowany: 810 tys. świec godzinowych i cztery
-- lata historii 24 instrumentów. Podmiot unijny ma dla BTCUSDT wolumen
-- 24-godzinny rzędu 0,1% globalnego, więc jego świece opisywałyby inny rynek.
--
-- Hosty w tej tabeli opisują wyłącznie to, GDZIE WYSYŁAMY ZAPYTANIA PODPISANE.
--
-- CZEGO TA MIGRACJA NIE ZAŁATWIA
--
-- Instrumenty w ref.instrument to pary *USDT z podmiotu globalnego. Na bybit.eu
-- istnieją 4 z nich; płynność unijna siedzi na parach USDC, gdzie sensowny
-- obrót ma 10 z naszych 24 aktywów bazowych (próg MIN_INSTRUMENTS to 8).
-- Przebazowanie uniwersum na USDC należy do fazy egzekucji, nie do tej zmiany:
-- dziś adapter i tak wyłącznie czyta, a składanie zleceń nie jest wdrożone.

UPDATE ref.venue
   SET name      = 'Bybit EU',
       demo_host = 'https://api-demo.bybit.eu',
       live_host = 'https://api.bybit.eu',
       meta      = meta || jsonb_build_object(
                     'podmiot',          'bybit.eu (MiCA, EOG)',
                     'host_danych',      'https://api.bybit.com',
                     'kontrakty_wieczyste', false,
                     'zmierzono',        '2026-09-04')
 WHERE code = 'bybit';
