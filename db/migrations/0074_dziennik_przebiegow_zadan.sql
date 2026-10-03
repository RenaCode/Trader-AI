-- 0074: `risk.job_run` - wynik kazdego przebiegu CronJobu w bazie.
--
-- CO BYLO ZLE (audyt Fable F-8.4, 2026-10-03)
--
-- Nieudane joby byly widoczne wylacznie w `kubectl get jobs`:
-- `trader-trainer-29838400` (25.09, kod 2), `trader-postmortem-29848670`
-- (02.10), `trader-zywotnosc-29848900` (02.10). `dozor` zglaszal wynik trenera,
-- ale nie post-mortemu ani wlasny. Kubernetes nie wysyla powiadomien, a historia
-- jobow ginie po `failedJobsHistoryLimit`.
--
-- Kazdy CronJob chodzi teraz przez `services/engine/przebieg_zadania.py`
-- (backup - przez `trap` w powloce, bo jego obraz nie ma naszego kodu), ktore
-- PO ZAKONCZENIU polecenia dopisuje jeden wiersz. `dozor --co zadania`
-- (w `trader-zywotnosc`) sprawdza ostatni przebieg kazdego wlaczonego zadania:
-- kod niezerowy albo cisza dluzsza niz dwa typowe odstepy -> `job.nieudany`.
--
-- JEDEN WIERSZ NA KONCU, NIE DWA (start + aktualizacja): wystarcza prawo
-- INSERT, a zadanie zabite SIGKILL-em / OOM nie zostawia wiersza - i to jest
-- dokladnie ten stan, ktory czujka wykrywa jako cisze.
--
-- `rola` = `current_user` i NIE DA SIE JEJ PODAC: prawo INSERT jest nadane na
-- kolumnach bez `rola`, wiec wartosc zawsze bierze sie z DEFAULT. Proces moze
-- dopisac przebieg pod obca nazwa zadania, ale nie pod obca rola - dziennik
-- zostaje dowodem tego, KTO pisal. Ten sam powod, dla ktorego czujka (0034) nie
-- ma zapisu w `risk` poza tym jednym, dopisywanym wierszem o samej sobie.
--
-- Bez retencji: ok. 170 wierszy na dobe (zywotnosc co 10 min), ~60 tys. na rok.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS risk.job_run (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nazwa       text        NOT NULL CHECK (nazwa ~ '^[a-z0-9][a-z0-9-]*$'),
    started_at  timestamptz NOT NULL,
    finished_at timestamptz NOT NULL DEFAULT now(),
    exit_code   integer     NOT NULL,
    pod         text,
    rola        text        NOT NULL DEFAULT current_user
);

CREATE INDEX IF NOT EXISTS job_run_nazwa_idx ON risk.job_run (nazwa, finished_at DESC);

-- Proba i post-mortem nie mialy dotad nic w `risk` poza tym, co nadal 0034.
GRANT USAGE ON SCHEMA risk TO trader_silnik, trader_czujka, trader_proba,
                              trader_postmortem;

GRANT INSERT (nazwa, started_at, exit_code, pod) ON risk.job_run
    TO trader_silnik, trader_czujka, trader_proba, trader_postmortem;
GRANT SELECT ON risk.job_run TO trader_czujka;

COMMIT;
