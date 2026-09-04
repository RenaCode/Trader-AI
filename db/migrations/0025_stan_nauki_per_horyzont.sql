-- 0025: stan nauki per HORYZONT, nie per silnik
--
-- DLACZEGO
--
-- `learning_state` miała klucz główny na samym `engine`, czyli schemat zakładał
-- jeden model na silnik. Założenie trzymało się dopóty, dopóki trenowana była
-- wyłącznie świeca 4-godzinna.
--
-- Przestało, gdy policzyliśmy cechy dla świecy 1h (4,17 mln wartości) i okazało
-- się, że te dwa horyzonty odpowiadają na pytanie inaczej — i to nie drobiazgiem:
--
--   świeca   dodatnich okien   mediana Sharpe   pokonał kup-i-trzymaj   okien
--     4 h      70,0–76,9%       +0,121…+0,176      41,0–50,0%           39–40
--     1 h      43,7–51,1%       −0,032…+0,001      50,0–56,3%           87–88
--
-- Świeca 4-godzinna wygrywa odsetkiem dodatnich okien, ale przegrywa jedyną
-- miarą, która mówi o przewadze: pokonaniem kup-i-trzymaj — i to w 4 przesunięciach
-- na 4. Wyjaśnienie jest proste i było zapowiedziane w tym dokumencie od dawna:
-- odsetek dodatnich okien nagradza NIEUCZESTNICZENIE. Strategia na świecy 4h
-- trzyma małą ekspozycję, więc rzadko traci — i rzadko wyprzedza rynek.
--
-- Dwa horyzonty muszą więc być śledzone RÓWNOLEGLE, a nie zastępować się
-- nawzajem w jednym wierszu. Bez tej zmiany nocny trening świecy 1h nadpisywałby
-- stan świecy 4h i dashboard pokazywałby liczby modelu, którym silnik nie handluje.
--
-- CO SIĘ NIE ZMIENIA
--
-- Silnik nadal handluje wyłącznie horyzontem domyślnym. API i dashboard czytają
-- ten jeden wiersz jawnym filtrem po `bar`, a nie przez przypadek wynikający
-- z tego, że innych wierszy nie ma.

ALTER TABLE learn.learning_state
    ADD COLUMN bar text NOT NULL DEFAULT '4h';

ALTER TABLE learn.learning_state DROP CONSTRAINT learning_state_pkey;
ALTER TABLE learn.learning_state ADD PRIMARY KEY (engine, bar);

COMMENT ON COLUMN learn.learning_state.bar IS
    'Horyzont, którego dotyczy ten stan. Silnik handluje horyzontem domyślnym '
    '(services/engine/horizons.py DEFAULT_BAR); pozostałe są torami '
    'porównawczymi i nie wpływają na decyzje.';

ALTER TABLE learn.learning_state_log
    ADD COLUMN bar text NOT NULL DEFAULT '4h';

DROP INDEX IF EXISTS learn.learning_state_log_engine_run_at;
CREATE INDEX learning_state_log_engine_bar_run_at
    ON learn.learning_state_log (engine, bar, run_at DESC);
