-- 0094: powiadomienie o rozjeździe cennika giełdy z cennikiem w kodzie (2026-10-05)
--
--   koszty.rozjazd - `mozliwosci.py --stawki` (czujka `trader-zywotnosc`)
--                    zmierzyła stawki inne niż `koszty.CENNIK`
--
-- DLACZEGO. Do dziś jedynym sygnałem rozjazdu był kod wyjścia, a rozjazd jest
-- STANEM: 2026-10-05 maker na api-demo.bybit.eu skoczył z 0,100% na 0,250%
-- i od tej chwili każdy przebieg czujki (co 10 min) kończył się czerwonym Jobem,
-- ArgoCD trzymało `trader` w Degraded, a monitoring VPS alarmował bez przyczyny.
-- Teraz jedno powiadomienie na zmianę stawek; tłumienie po stronie czujki
-- (`mozliwosci.zglos_rozjazd_stawek`), więc katalogowy cooldown 0.
--
-- `critical`: stawka fałszuje każdy modelowany wynik naraz - ma dojść na
-- telefon przez bramkę, a nie zostać w dzwonku.
--
-- Plik idempotentny (ON CONFLICT DO NOTHING) - migrator zapisuje plik i wpis
-- w `schema_migrations` w osobnych transakcjach.

BEGIN;

SET LOCAL lock_timeout = '5s';

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'koszty.rozjazd',
    'Rozjazd stawek giełdy',
    'Giełda nalicza prowizje inne niż cennik w kodzie (`services/engine/koszty.py`). ' ||
    'Rachunek papierowy, backtest i trening liczą po nieprawdziwej stawce.',
    'handel', 'critical', true, 0)
ON CONFLICT (code) DO NOTHING;

INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, 'koszty.rozjazd', true, ARRAY['inapp']::notify.channel_kind[]
  FROM app."user" u
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
