-- 0063: rytm pomiaru limitow wymaga ODCZYTU tabeli pomiarow
--
-- CO JEST NIE TAK
--
-- `RiskKernel.check_account()` - jedyne miejsce, ktore ocenia limity CALEGO
-- konta i wyzwala kill-switch - konczy sie na bazie zbudowanej WYLACZNIE
-- z tych migracji bledem:
--
--     psycopg.errors.InsufficientPrivilege:
--         permission denied for table limit_usage
--
-- Dla ROLI DOZORCY I DLA ROLI SILNIKA, oba silniki, kazdy cykl.
--
-- Przyczyna jest w jednym zapytaniu. `record_usage(..., co_najmniej_s=...)`
-- trzyma RYTM pomiaru w bazie, a nie w pamieci procesu (uzasadnienie w jej
-- docstringu: licznik w procesie omija rytm po kazdym restarcie poda i przy
-- drugim procesie oceniajacym to samo konto). Robi to przez
-- `INSERT ... SELECT ... WHERE NOT EXISTS (SELECT 1 FROM risk.limit_usage ...)`,
-- czyli CZYTA te tabele. Migracja 0043 nadala dozorcy sam `INSERT`, a 0034
-- silnikowi tez sam `INSERT` - i to wystarczalo dokladnie do dnia, w ktorym
-- doszedl warunek rytmu.
--
-- DLACZEGO NIKT TEGO NIE ZAUWAZYL
--
-- Bo produkcja ma te nadania. `information_schema.role_table_grants` na
-- produkcji 2026-09-27 pokazuje `trader_dozorca|SELECT` i `trader_silnik|SELECT`
-- na `risk.limit_usage`; swiezo zmigrowana baza nie pokazuje ani jednego z nich.
-- Nadania weszly wiec POZA migracjami - recznym `GRANT`-em przy diagnozie - i od
-- tej chwili repozytorium przestalo opisywac stan, w ktorym system dziala.
--
-- CENA TEGO ROZJAZDU JEST DOKLADNIE TA, KTORA TEN PROJEKT JUZ ZAPLACIL. Baza
-- odtworzona z kopii i migracji (awaria wezla, nowe srodowisko, klon do proby)
-- dostaje dozorce, ktory wywraca sie na KAZDYM cyklu, spi 60 s i probuje znowu -
-- czyli stan z 2026-09-11/12: `Running 1/1`, `restartCount 0`, ArgoCD `Healthy`,
-- 900 przerwanych cykli pod rzad i ZERO pomiarow limitow konta. Tamta awaria
-- miala te sama tresc komunikatu, tylko inny schemat (`journal`).
--
-- Licznik `POD_RZAD_MAX` z 2026-09-12 zamknie dzis ten pod po pieciu minutach,
-- wiec awaria bedzie WIDOCZNA - ale bezpiecznika w niej nie ma tak samo.
--
-- ZAKRES: SAM `SELECT`, I TYLKO NA TABELI POMIARU
--
-- `risk.limit_usage` jest sladem pomiaru, ktory te dwie role SAME zapisuja.
-- Odczyt wlasnego sladu nie przekracza zadnej granicy z 0034: nie ma tu
-- uzasadnien decyzji (`journal`), nie ma cen (`market`), nie ma kluczy gieldy
-- i nie ma prawa zmiany limitu. `UPDATE`, `DELETE` i `TRUNCATE` zostaja odebrane
-- przez 0043 i nic tu tego nie odwraca - historia pomiarow ma byc dopisywana,
-- nie przepisywana.
--
-- CZEGO TA MIGRACJA NIE ROBI: nie nadaje `SELECT` domyslnie na przyszle tabele
-- w schemacie `risk`. Kolejny mechanizm bezpieczenstwa ma dostac prawa
-- swiadomie, a nie dlatego, ze powstal.

BEGIN;

GRANT SELECT ON risk.limit_usage TO trader_dozorca, trader_silnik;

COMMIT;
