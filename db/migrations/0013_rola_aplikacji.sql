-- 0013: osobna rola dla aplikacji, bez uprawnień superużytkownika
--
-- DLACZEGO
--
-- Do tej pory aplikacja wystawiona na internet łączyła się do bazy rolą `trader`,
-- która jest superużytkownikiem. Oznaczało to, że dowolne wstrzyknięcie SQL albo
-- błąd w kodzie mógł zrobić `DROP`, `TRUNCATE` albo — najgorsze —
-- `ALTER TABLE ... DISABLE TRIGGER`.
--
-- Ostatnie jest istotne, bo niezmienność dziennika decyzji, czyli fundament całej
-- oceny "czy algorytm się uczy", opiera się na wyzwalaczu journal.forbid_mutation().
-- Superużytkownik mógł go wyłączyć jednym poleceniem, a wtedy `UPDATE` na dzienniku
-- przestawał być niemożliwy i stawał się po prostu nieudokumentowany.
--
-- ZASADA PODZIAŁU
--
--   trader      — właściciel schematów, uruchamia WYŁĄCZNIE migracje i konserwację
--   trader_app  — łączy się aplikacja i procesy robocze; nie jest właścicielem
--                 żadnej tabeli, więc nie może wyłączyć wyzwalacza ani zmienić
--                 struktury, niezależnie od nadanych uprawnień
--
-- DELETE nadajemy WYBIÓRCZO. Aplikacja kasuje wyłącznie własne obiekty
-- konfiguracyjne (klucze, sekrety, portfele, kanały powiadomień). Na danych
-- rynkowych, cechach, dzienniku i historii treningu nie ma prawa kasować niczego —
-- to są dane audytowe.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trader_app') THEN
        CREATE ROLE trader_app LOGIN;
    END IF;
END $$;

-- Hasło ustawiane osobno przez migrację parametryzowaną (patrz db/migrate.py),
-- żeby nie trafiło do repozytorium.

GRANT CONNECT ON DATABASE trader TO trader_app;

DO $$
DECLARE s text;
BEGIN
    FOREACH s IN ARRAY ARRAY['ref','market','news','feat','sig','journal',
                             'exec','learn','risk','app','notify']
    LOOP
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO trader_app', s);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA %I TO trader_app', s);
        EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO trader_app', s);
        EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO trader_app', s);
        -- Tabele tworzone przez PRZYSZŁE migracje też muszą być dostępne,
        -- inaczej pierwsza nowa tabela wywali aplikację po wdrożeniu.
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT SELECT, INSERT, UPDATE ON TABLES TO trader_app', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT USAGE, SELECT ON SEQUENCES TO trader_app', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT EXECUTE ON FUNCTIONS TO trader_app', s);
    END LOOP;
END $$;

-- DELETE tylko tam, gdzie aplikacja naprawdę kasuje — obiekty konfiguracyjne
-- należące do użytkownika.
GRANT DELETE ON app.api_credential, app.backup_code, app.secret,
                app.totp_secret, app.wallet, app.session TO trader_app;
GRANT DELETE ON notify.channel TO trader_app;

-- Jawne odebranie na dzienniku decyzji. Wyzwalacz i tak by to zablokował, ale
-- uprawnienie i wyzwalacz to dwie niezależne warstwy, a dziennik jest tym
-- miejscem, w którym warto mieć obie.
REVOKE DELETE, TRUNCATE ON journal.decision, journal.decision_contribution
    FROM trader_app;

-- Zapis do tabel systemowych TimescaleDB nie jest aplikacji do niczego potrzebny.
REVOKE ALL ON SCHEMA information_schema FROM trader_app;
