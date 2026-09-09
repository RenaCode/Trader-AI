-- 0043: dozorca ryzyka musi móc ZOSTAWIĆ ŚLAD po pomiarze limitów konta
--
-- CO SIĘ ZMIENIŁO W KODZIE
--
-- `RiskKernel.check_account()` liczyło zużycie każdego limitu konta w każdym
-- cyklu i nie zapisywało niczego. Jedynym pisarzem do `risk.limit_usage` była
-- ścieżka wejścia w pozycję, więc w dniach bez wejść tabela milczała — a to są
-- dokładnie te dni, w których ocena limitów jest jedyną rzeczą, jaką system
-- robi. Od tej zmiany `check_account()` zapisuje pomiar w stałym rytmie
-- (services/engine/risk.py, POMIAR_CO_S).
--
-- DLACZEGO TO WYMAGA MIGRACJI
--
-- `check_account()` woła DWÓCH: pętla decyzyjna (rola `trader_silnik`, ma
-- INSERT od 0034) i dozorca ryzyka (rola `trader_dozorca`, NIE MA). Bez tego
-- nadania dozorca wywracałby się na pierwszym cyklu po wdrożeniu — a jego pętla
-- łapie wyjątek i idzie dalej, więc awaria wyglądałaby tak:
--
--     * ocena limitów przerywana w połowie, kill-switch nigdy nie wyzwalany,
--     * `wznow()` i `bij_heartbeat()` NIE WYKONYWANE, bo leżą za rzuconym
--       wyjątkiem w tym samym `try` — czyli czujka żywotności zgłosiłaby
--       martwego dozorcę, a prawdziwą przyczyną byłby brak jednego GRANT-a.
--
-- To jest ten sam kształt cichej awarii, którą ta zmiana usuwa, więc nadanie
-- idzie razem z nią, a nie po niej.
--
-- DLACZEGO TO JEST BEZPIECZNE NADANIE
--
-- `risk.limit_usage` to DZIENNIK POMIARU, nie tabela sterująca: nic z niej nie
-- czyta reguła, która o czymkolwiek decyduje. Dozorca ma już INSERT na
-- `risk.killswitch_event`, czyli prawo ZATRZYMANIA handlu — prawo dopisania
-- liczby, którą sam zmierzył, jest przy tym drobiazgiem. Świadomie NIE dajemy
-- mu UPDATE ani DELETE: proces, który potrafi przepisać własny pomiar, nie jest
-- już dowodem na to, że mierzył.

GRANT INSERT ON risk.limit_usage TO trader_dozorca;

-- Ani UPDATE, ani DELETE, ani TRUNCATE — patrz wyżej. Odebranie jest formalnie
-- zbędne (nic ich nie nadało), ale zostaje jako jedyne miejsce, które mówi
-- wprost, czego tu NIE MA: bez tej linijki pierwsze „dodajmy dozorcy UPDATE, bo
-- coś nie działa" przeszłoby bez śladu w przeglądzie.
REVOKE UPDATE, DELETE, TRUNCATE ON risk.limit_usage FROM trader_dozorca;

-- Czujka pozostaje CZYTELNIKIEM. `dozor.py --co ryzyko` wykrywa teraz ciszę
-- w tej tabeli, więc kusi, żeby dać jej prawo „odświeżenia pomiaru". To by
-- znaczyło, że obserwator produkuje dowód, którego pilnuje.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.limit_usage FROM trader_czujka;


-- ------------------------------------------------- zdarzenie: bezpiecznik milczy
--
-- DLACZEGO OSOBNY TYP, A NIE `engine.down`
--
-- `engine.down` znaczy „pętla decyzyjna nie odpowiada" i ma oczywistą reakcję:
-- podnieś pod. Tutaj jest inaczej i różnica jest cała treścią alarmu: PROCESY
-- MOGĄ ŻYĆ, handel może iść dalej, pulsy mogą bić — a limity konta nie są
-- mierzone. Zlanie tego z `engine.down` ustawiłoby próg uwagi na częstszym
-- i łatwiejszym zdarzeniu, czyli nauczyłoby przewijać to rzadsze.
--
-- `always_on = true` i cooldown 0 — dokładnie jak `killswitch.triggered`, i z
-- tego samego powodu: rytm przypomnień trzyma KOD czujki
-- (`ocen_cisze_pomiaru` + `odstep_przypomnienia_ks` w dozor.py), bo tam jest
-- znany wiek zdarzenia. Cooldown w katalogu byłby drugą kopią tej samej liczby
-- i rozjechałby się z pierwszą przy pierwszej zmianie. `always_on` znaczy też,
-- że alarmu o niedziałającym bezpieczniku nie da się wyłączyć w preferencjach.
INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES
    ('risk.pomiar_zamilkl', 'Bezpiecznik przestał raportować',
     'Ocena limitów całego konta przestała zostawiać ślad w risk.limit_usage. '
     'To nie jest informacja o stracie — to informacja o tym, że nie wiadomo, '
     'czy strata zostałaby zauważona: kill-switch nie zadziała na podstawie '
     'liczby, której nikt nie policzył. Handel przy tym może iść dalej, więc '
     'stan jest niewidoczny wszędzie indziej.',
     'ryzyko', 'critical', true, 0)
ON CONFLICT (code) DO NOTHING;

-- Preferencja dla istniejących kont. `always_on` i tak wymusza dostarczenie,
-- ale bez wiersza zdarzenie nie pokazuje się na liście do skonfigurowania —
-- czyli właściciel nie wie, że taka czujka w ogóle istnieje.
INSERT INTO notify.preference (user_id, event_code, enabled, channels)
SELECT u.id, 'risk.pomiar_zamilkl', true, '{inapp}'::notify.channel_kind[]
FROM app.user u
ON CONFLICT DO NOTHING;
