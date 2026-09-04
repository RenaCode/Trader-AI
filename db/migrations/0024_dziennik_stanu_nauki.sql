-- 0024: dziennik stanu nauki — żeby dało się powiedzieć, CZY COŚ SIĘ ZMIENIŁO
--
-- DLACZEGO
--
-- `learn.learning_state` to jeden wiersz na silnik, nadpisywany przy każdym
-- przebiegu trenera (ON CONFLICT DO UPDATE). Po zapisie poprzednia wartość
-- przestaje istnieć, więc pytanie „czy dzisiejszy wynik różni się od
-- wczorajszego" jest nieodpowiadalne — a to jedyne pytanie, które ma sens
-- zadawać codziennie.
--
-- Bez tej historii każde automatyczne czuwanie musiałoby albo milczeć, albo
-- odzywać się co dobę niezależnie od tego, czy zaszło cokolwiek. Drugie jest
-- gorsze: powiadomienie, które przychodzi zawsze, przestaje cokolwiek znaczyć.
--
-- CO TU NIE TRAFIA
--
-- To nie jest kopia `learning_state` „na wszelki wypadek”. Zapisujemy wyłącznie
-- te wielkości, na podstawie których podejmowana jest decyzja o odezwaniu się:
-- status, liczba okien, odsetek dodatnich z przedziałem, mediana Sharpe, dryf
-- i licznik hipotez. Wiersze są niemutowalne — porównujemy dwa ostatnie.

CREATE TABLE learn.learning_state_log (
    id                bigserial PRIMARY KEY,
    engine            ref.engine       NOT NULL,
    run_at            timestamptz      NOT NULL DEFAULT now(),
    status            text             NOT NULL,
    windows_total     integer          NOT NULL,
    windows_positive  integer          NOT NULL,
    positive_ci_low   double precision,
    positive_ci_high  double precision,
    median_oos_sharpe double precision,
    max_drawdown      double precision,
    max_psi           double precision,
    trials_run        integer,
    model_version_id  integer REFERENCES learn.model_version(id)
);

CREATE INDEX learning_state_log_engine_run_at
    ON learn.learning_state_log (engine, run_at DESC);

COMMENT ON TABLE learn.learning_state_log IS
    'Append-only: jeden wiersz na przebieg trenera. Służy wyłącznie do '
    'stwierdzenia, czy między przebiegami zaszła zmiana istotna — patrz '
    'services/engine/dozor.py.';

-- Punkt odniesienia z bieżącego stanu, żeby pierwszy nocny przebieg miał
-- z czym się porównać. Bez tego dozór odezwałby się raz z komunikatem
-- „pierwszy zapisany przebieg" — nieszkodliwie, ale bez treści.
INSERT INTO learn.learning_state_log
    (engine, run_at, status, windows_total, windows_positive, positive_ci_low,
     positive_ci_high, median_oos_sharpe, max_drawdown, max_psi, trials_run,
     model_version_id)
SELECT engine, updated_at, status, windows_total, windows_positive,
       positive_ci_low, positive_ci_high, median_oos_sharpe, max_drawdown,
       max_psi, trials_run, model_version_id
  FROM learn.learning_state;
