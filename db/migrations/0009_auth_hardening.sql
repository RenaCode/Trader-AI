-- 0009: utwardzenie warstwy uwierzytelniania
--
-- Migracje sa niemutowalne, wiec poprawki do 0007 ida jako nowy plik.

-- Sesja bez wygasania nie ma sensu; indeks przyspiesza czyszczenie i liste sesji.
CREATE INDEX IF NOT EXISTS session_expiry_idx ON app.session (expires_at)
    WHERE revoked_at IS NULL;

-- Kod zapasowy moze byc uzyty tylko raz - unikalnosc w schemacie, nie w kodzie.
CREATE UNIQUE INDEX IF NOT EXISTS backup_code_unique_idx
    ON app.backup_code (user_id, code_hash);

-- Login bez spacji i w jednej postaci - inaczej " Admin" i "admin" to dwa konta.
ALTER TABLE app.user
    ADD CONSTRAINT user_login_trimmed CHECK (login = btrim(login) AND length(login) >= 3);

-- Sprzatanie wygaslych sesji. Bez tego tabela rosnie w nieskonczonosc, a kazde
-- logowanie dodaje wiersz.
CREATE OR REPLACE FUNCTION app.purge_expired_sessions() RETURNS integer
LANGUAGE sql AS $fn$
    WITH gone AS (
        DELETE FROM app.session
        WHERE expires_at < now() - interval '7 days'
        RETURNING 1
    )
    SELECT count(*)::integer FROM gone;
$fn$;

COMMENT ON FUNCTION app.purge_expired_sessions IS
'Usuwa sesje wygasle ponad 7 dni temu. Wolane przez zadanie cykliczne.';
