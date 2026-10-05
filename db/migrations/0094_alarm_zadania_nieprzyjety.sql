-- 0094: `notify.alarm_nieprzyjety` - alarm o zadaniu, ktorego `notify()` nie zapisal.
--
-- CO BYLO ZLE (2026-10-05, log trader-zywotnosc-29853150)
--
-- `dozor --co zadania` wykryl padniety planowy trening, wolal `notify()` dla
-- `job.nieudany`, a ten wracal `None` bez zapisu (preferencja wylaczona albo
-- cooldown z preferencji - jedyne dwie drogi do `None` w
-- services/api/notifications.py). Bez wiersza nastepny przebieg nie wiedzial,
-- ze juz probowal: kod 1 co 10 minut, a do czlowieka nie dochodzilo nic.
--
-- To zdarzenie jest DRUGA DROGA dla tego jednego przypadku: `always_on`, wiec
-- omija preferencje i cooldown katalogu, a tresc mowi wprost, ze pierwotny
-- alarm nie zostal przyjety i dlaczego to jest blad kanalu. Tlumienie powtorek
-- (doba, dwie, cztery, potem tydzien) liczy dozor z `data->>'klucz'`, tak jak
-- dla `job.nieudany` - always_on nie znaczy "co 10 minut".

BEGIN;

SET LOCAL lock_timeout = '5s';

INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES (
    'notify.alarm_nieprzyjety',
    'Alarm o zadaniu nie został przyjęty',
    'Czujka wykryła nieudane zadanie cykliczne, ale powiadomienie `job.nieudany` ' ||
    'nie zostało zapisane dla żadnego odbiorcy (wyłączona preferencja albo jej ' ||
    'cooldown). To jest błąd kanału alarmowego: sprawdź preferencje ' ||
    'powiadomień dla „Zadanie cykliczne nie powiodło się”.',
    'system', 'warning', true, 0)
ON CONFLICT (code) DO NOTHING;

COMMIT;
