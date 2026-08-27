-- 0014: odczyt tabeli migracji dla roli aplikacji
--
-- Migracja 0013 nadała uprawnienia jedenastu schematom dziedzinowym i pominęła
-- `public.schema_migrations` — tabelę, którą zakłada sam migrator. Endpoint
-- /api/health czyta z niej numer ostatniej migracji, żeby raportować wersję
-- schematu, więc po przełączeniu na rolę aplikacji sonda gotowości zaczęła
-- zwracać 500 i pod nie wchodził do serwisu.
--
-- Wyłapała to sonda gotowości, nie użytkownik — czyli dokładnie to, po co ona
-- jest. Stara wersja poda dalej obsługiwała ruch, bo Kubernetes nie kieruje go
-- do poda, który nie zgłosił gotowości.
--
-- Nadajemy WYŁĄCZNIE SELECT. Zapis do tej tabeli należy do migratora, który
-- chodzi rolą właściciela.

GRANT USAGE ON SCHEMA public TO trader_app;
GRANT SELECT ON public.schema_migrations TO trader_app;
