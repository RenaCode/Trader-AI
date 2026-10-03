-- 0073: typ zdarzenia `killswitch.proba` - skutek proby pozarowej jako `info`
--
-- CO BYLO ZLE (audyt Fable F-5.2, 2026-10-03)
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

INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, 'killswitch.proba', true, ARRAY['inapp']::notify.channel_kind[]
  FROM app."user" u
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
