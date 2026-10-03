-- 0076: `trader_silnik` traci SELECT na `app.secret` (audyt Fable U-9).
--
-- 0034 nadalo silnikowi odczyt tabeli sekretow ustawien (klucze dostawcow LLM
-- i inne wartosci z zakladki Ustawienia). Zaden proces tej roli jej nie czyta:
-- klucze LLM ida przez widok `app.secret_llm` (0052), ktory chodzi prawami
-- wlasciciela, wiec dalej dziala. Szyfrogram bez SECRETS_KEY jest bezuzyteczny,
-- ale procesy tej roli (engine, market, history) TEN KLUCZ MAJA - wiec odczyt
-- tabeli byl odczytem sekretow, nie szyfrogramow.

BEGIN;

SET LOCAL lock_timeout = '5s';

REVOKE SELECT ON app.secret FROM trader_silnik;

COMMIT;
