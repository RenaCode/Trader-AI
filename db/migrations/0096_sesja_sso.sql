-- 0096: `app.session.sso` - sesja zalozona z naglowka Authelii (SSO).
--
-- Przy `SSO_NAGLOWKI=1` (chart: `sso.enabled`) `current_user` zaklada sesje
-- bez hasla i bez TOTP aplikacji dla konta o loginie == `Remote-User`
-- (services/api/auth.py, renacode-infra docs/sso-tozsamosc.md). Kolumna
-- odroznia je od sesji z wlasnego logowania: w liscie sesji w profilu,
-- w `/api/auth/me` (panel pokazuje, skad jest sesja) i przy dochodzeniu po
-- incydencie - `mfa_satisfied` jest w obu przypadkach `true`, ale drugi
-- skladnik sprawdzil kto inny.
--
-- Wylacznie ADDYTYWNA: `NOT NULL DEFAULT false`, wiec sesje istniejace
-- w chwili migracji sa tym, czym byly - z wlasnego logowania. Uprawnienia
-- roli aplikacji sa nadane na cala tabele (0013), nowa kolumna nie wymaga
-- GRANT-a.

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE app.session
    ADD COLUMN IF NOT EXISTS sso boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN app.session.sso IS
    'Sesja z naglowka Remote-User (Authelia, 2FA po stronie portalu), nie z hasla i TOTP aplikacji.';

COMMIT;
