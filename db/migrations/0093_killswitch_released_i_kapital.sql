-- 0093: alarm o zwolnieniu kill-switcha i o przepływie kapitału (przegląd 2026-10-04)
--
--   killswitch.released  - zwolnienie RĘCZNE (panel) albo AUTOMATYCZNE
--                          (`wznowienie.py`), zawsze `critical`, czyli przez
--                          bramkę RenaCode na telefon (T-6, T-4)
--   kapital.przeplyw     - wpłata albo wypłata zapisana w panelu (T-7)
--
-- DLACZEGO ZWOLNIENIE TEŻ JEST ALARMEM. Do dziś zwolnienie z panelu zostawiało
-- ślad wyłącznie w `app.audit`, a automatyczne szło jako `killswitch.triggered`
-- z `warning`, czyli tylko do dzwonka. Właściciel, który dostał na telefon
-- „kill-switch zadziałał", nie dowiadywał się, że handel wrócił - ani wtedy, gdy
-- zrobił to automat po godzinie, ani wtedy, gdy zrobiła to przejęta sesja.
-- Osobny kod, a nie `killswitch.triggered`: dozór liczy rytm przypomnień
-- z powiadomień TEGO kodu (`dozor.py`), a zwolnienie nie jest przypomnieniem.
--
-- DLACZEGO PRZEPŁYW KAPITAŁU. Kapitał crypto/demo liczy się z przepływów (host
-- demo nie wystawia sald), więc wpis wpłaty podnosi budżet każdego limitu
-- liczonego od kapitału. Taka zmiana ma dojść do właściciela, a nie tylko
-- zostać w tabeli.
--
-- Plik idempotentny (ON CONFLICT DO NOTHING, UPDATE na stałą treść) - migrator
-- zapisuje plik i wpis w `schema_migrations` w osobnych transakcjach.

BEGIN;

-- Jak 0091/0092: po 5 s czekania na blokadę migracja pada i `api` próbuje
-- ponownie, zamiast trzymać kolejkę za odczytem dozoru.
SET LOCAL lock_timeout = '5s';

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'killswitch.released',
    'Kill-switch zwolniony',
    'Handel wznowiony: kill-switch zwolniony ręcznie z panelu albo automatycznie ' ||
    'według reguły limitu (`risk.limit_def.wznowienie`). Silnik wraca do handlu ' ||
    'przy najbliższym cyklu.',
    'ryzyko', 'critical', true, 0)
ON CONFLICT (code) DO NOTHING;

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'kapital.przeplyw',
    'Wpłata lub wypłata',
    'W panelu zapisano przepływ kapitału. Kapitał, od którego liczone są limity ' ||
    'i wielkość pozycji, zmienił się o tę kwotę.',
    'konto', 'critical', true, 0)
ON CONFLICT (code) DO NOTHING;

UPDATE notify.event_type
   SET description = 'System zatrzymał handel po przekroczeniu limitu ryzyka. ' ||
       'Wznowienie: ręczne albo automatyczne według reguły limitu ' ||
       '(`risk.limit_def.wznowienie`).'
 WHERE code = 'killswitch.triggered';

-- Jak 0073: preferencja w dzwonku dla aktywnych użytkowników, żeby nowe typy
-- były widoczne w ustawieniach powiadomień. `always_on` i tak omija preferencje.
INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, k.code, true, ARRAY['inapp']::notify.channel_kind[]
  FROM app."user" u
 CROSS JOIN (VALUES ('killswitch.released'), ('kapital.przeplyw')) AS k(code)
 WHERE u.is_active
ON CONFLICT (user_id, event_code) DO NOTHING;

COMMIT;
