-- 0015: ochrona kodu TOTP przed powtórzeniem
--
-- `verify_totp(valid_window=1)` przyjmuje kod przez około 90 sekund. Nigdzie nie
-- zapisywaliśmy, że dany kod już zużyto, więc przechwycony kod działał ponownie
-- w tym oknie — a przy 2FA chroniącym dostęp do systemu obracającego pieniędzmi
-- to jest realna różnica, nie formalność.
--
-- RFC 6238 §5.2 mówi o tym wprost: weryfikator MUSI odrzucić drugie użycie tego
-- samego kroku czasowego.
--
-- Trzymamy numer ostatnio zaakceptowanego kroku (unix_time / 30). Kod z krokiem
-- nie większym niż zapisany jest odrzucany niezależnie od tego, czy sam w sobie
-- jest poprawny.

ALTER TABLE app.totp_secret
    ADD COLUMN last_step bigint;

COMMENT ON COLUMN app.totp_secret.last_step IS
    'Ostatnio zaakceptowany krok czasowy TOTP (unix/30). Chroni przed ponownym użyciem kodu.';
