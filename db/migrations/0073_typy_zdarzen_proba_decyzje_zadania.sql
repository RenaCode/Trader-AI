-- 0073: trzy typy zdarzen z audytu Fable (2026-10-03)
--
--   killswitch.proba     - skutek proby pozarowej jako `info` (F-5.2)
--   engine.brak_decyzji  - swieca bez decyzji dla czesci uniwersum (F-8.3)
--   job.nieudany         - CronJob bez udanego przebiegu (F-8.4, risk.job_run)
--
-- KILLSWITCH.PROBA (F-5.2)
--
-- Codzienna proba kill-switcha (05:20) konczyla sie powiadomieniem
-- `killswitch.triggered` o severity `critical`. W dzwonku lezalo 15
-- nieprzeczytanych "krytycznych" z osmiu dni prob - prawdziwe zadzialanie
-- bezpiecznika utonelo by wsrod nich.
--
-- `dozor.sprawdz_ryzyko` wysyla teraz skutek proby (zdarzenie z
-- `proba_do > now()` i `triggered_at >= updated_at` limitu) tym kodem
-- z severity `info`. Zdarzenie, ktore przezyje okno proby, wraca jako
-- `killswitch.triggered` / `critical` - powiadomienie o probie go nie tlumi.
--
-- `always_on = false` i domyslnie tylko panel: proba o 05:20 nie ma budzic
-- nikogo przez webhook. Cooldown 0, bo rytm prowadzi czujka. UWAGA: wylaczenie
-- tego typu w preferencjach sprawi, ze proba oblewa ogniwo 4 ("czy dozor
-- zglosil") - `notify` nie zapisuje wtedy wiersza.
--
-- KOLEJNOSC WDROZENIA: migracja PRZED kodem (`notify` rzuca `ValueError` na
-- nieznany typ). Migrator chodzi jako kontener startowy `api`.

BEGIN;

SET LOCAL lock_timeout = '5s';

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'killswitch.proba',
    'Próba pożarowa kill-switcha',
    'Kill-switch zadziałał w oknie codziennej próby pożarowej — najpewniej ' ||
    'jej skutek. Jeśli bezpiecznik będzie aktywny po zamknięciu okna, ' ||
    'przyjdzie zwykły alarm krytyczny.',
    'ryzyko', 'info', false, 0)
ON CONFLICT (code) DO NOTHING;

-- ENGINE.BRAK_DECYZJI (F-8.3). `dozor --co decyzje`: dwie domkniete swiece
-- z rzedu z decyzjami dla mniej niz calego uniwersum, choc cechy przyszly.
-- Puls silnika tego nie widzi - bije, gdy cykl sie udal. `warning`, bo silnik
-- zyje i pozycje chroni dozorca; rytm (raz na dobe) prowadzi czujka.
INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'engine.brak_decyzji',
    'Świeca bez decyzji dla części instrumentów',
    'Przez dwie kolejne świece silnik nie zapisał decyzji dla każdego ' ||
    'instrumentu uniwersum, choć cechy tych świec przyszły.',
    'system', 'warning', false, 0)
ON CONFLICT (code) DO NOTHING;

-- JOB.NIEUDANY (F-8.4). `dozor --co zadania` czyta `risk.job_run` (0074):
-- ostatni przebieg CronJobu zakonczony kodem niezerowym albo brak udanego
-- przebiegu w oczekiwanym oknie. Do tej pory widoczne tylko w `kubectl get
-- jobs` (trainer 25.09, postmortem 02.10, zywotnosc 02.10).
INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'job.nieudany',
    'Zadanie cykliczne nie powiodło się',
    'Ostatni przebieg zadania cyklicznego zakończył się błędem albo zadanie ' ||
    'nie zakończyło się powodzeniem w oczekiwanym oknie.',
    'system', 'warning', false, 0)
ON CONFLICT (code) DO NOTHING;

INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, k.code, true, ARRAY['inapp']::notify.channel_kind[]
  FROM app."user" u
 CROSS JOIN (VALUES ('killswitch.proba'), ('engine.brak_decyzji'),
                    ('job.nieudany')) AS k(code)
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
