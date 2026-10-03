-- 0071: typ zdarzenia `reconcile.mismatch` - rozjazd z gielda pod wlasnym kodem
--
-- CO BYLO ZLE (audyt 3 T3, 2026-10-03)
--
-- `uzgodnienie.py` zglaszal rozjazd stanu z gielda kodem `killswitch.triggered`,
-- choc zaden kill-switch nie zadzialal. Szkoda nie konczyla sie na mylacym
-- tytule w dzwonku:
--
--   * `dozor.sprawdz_kill_switch` liczy odstep od OSTATNIEGO powiadomienia
--     `killswitch.triggered` danego silnika po `triggered_at` aktywnego
--     zdarzenia. Alarm uzgodnienia w tym oknie wygladal jak przypomnienie
--     o kill-switchu i tlumil prawdziwe przypomnienie;
--   * `proba_killswitcha` musiala filtrowac po `data->>'event_id'`, bo pod tym
--     samym kodem chodzily cudze powiadomienia.
--
-- Rozjazd zostaje alarmem krytycznym i `always_on` (omija cooldown i godziny
-- ciszy, idzie wszystkimi kanalami) - zmienia sie tylko nazwa, nie waga.
--
-- KOLEJNOSC WDROZENIA: migracja musi wejsc PRZED kodem, ktory wysyla nowy kod
-- (`notify` rzuca `ValueError` na nieznany typ). Migrator chodzi jako kontener
-- startowy `api`, wiec wystarczy, ze `api` wstanie przed pierwszym przebiegiem
-- `trader-zywotnosc` na nowym obrazie.

BEGIN;

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'reconcile.mismatch',
    'Nasz stan nie zgadza się z giełdą',
    'Uzgodnienie znalazło różnicę między pozycjami w naszym dzienniku a saldami ' ||
    'lub rejestrem giełdy, której nie wyjaśnia żadne zlecenie w locie. ' ||
    'Wymaga wyjaśnienia przed kolejnym zleceniem.',
    'ryzyko', 'critical', true, 0)
ON CONFLICT (code) DO NOTHING;

INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, 'reconcile.mismatch', true, ARRAY['inapp']::notify.channel_kind[]
  FROM app."user" u
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
