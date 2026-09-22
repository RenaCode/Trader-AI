-- Próba pożarowa czyta dziennik decyzji — ogniwo 3 łańcucha kill-switcha.
--
-- DLACZEGO TA MIGRACJA W OGÓLE ISTNIEJE
--
-- Do 2026-09-22 `services/engine/proba_killswitcha.py` sprawdzała ogniwa 1-2
-- i 4, a na końcu wypisywała BEZWARUNKOWO „wszystkie ogniwa zadziałały".
-- Ogniwo 3 — „kill-switch aktywny -> silnik wstrzymał wejścia" — było wypisane
-- w nagłówku modułu i nigdy nie odpytane, mimo że jako jedyne z czterech
-- dotyczy PIENIĘDZY; pozostałe trzy opisują zapis i powiadomienie.
--
-- Śladem wstrzymania jest `journal.decision.powod_holdu = 'wstrzymanie_ryzykiem'`
-- (migracja 0040). Rola `trader_proba` nie miała do schematu `journal` ani
-- USAGE, ani SELECT — z rozdziału ról w 0034 wynikało dla niej wyłącznie to,
-- czego wymagało podniesienie progu i zwolnienie własnego zdarzenia.
--
-- Bez tego nadania zapytanie ogniwa 3 kończy się `permission denied for schema
-- journal` W ŚRODKU próby: wyjątek leci przez `finally` (próg wraca, zdarzenie
-- zostaje zwolnione), ale zadanie cykliczne pokazuje ślad stosu zamiast wyniku,
-- a łańcuch nie jest sprawdzony ani razu. To jest dokładnie ta klasa rozjazdu,
-- którą opisuje `tests/test_role_bazodanowe.py`: kod i nadanie wyglądają
-- poprawnie Z OSOBNA, a Postgres mówi o rozjeździe dopiero przy wykonaniu.
--
-- ZAKRES: SAM ODCZYT, SAMA JEDNA TABELA
--
-- Próba liczy w `journal.decision` dwie rzeczy i nic więcej: ile zamiarów
-- wejścia zostało wstrzymanych po zadziałaniu bezpiecznika i ile wejść mimo
-- niego zapisano. Nadanie na cały schemat (albo domyślne uprawnienie na przyszłe
-- tabele) dałoby jej wgląd w `journal.planned_order` i w każdą następną tabelę
-- dziennika — czyli rozszerzałoby zasięg procesu przy każdej kolejnej migracji,
-- bez niczyjej decyzji. Dziennik jest zapisem tego, co system postanowił;
-- proces, który go czyta szerzej, niż mierzy, przestaje być wąskim narzędziem.
--
-- ZAPISU NIE MA I NIE MOŻE BYĆ. `journal.decision` jest niemutowalna
-- (wyzwalacz `decision_immutable`, migracja 0005), a próba, która mogłaby do
-- dziennika dopisać, mierzyłaby własny INSERT zamiast zachowania silnika —
-- ten sam powód, dla którego 0034 odmawia jej INSERT-u na `risk.killswitch_event`.

GRANT USAGE ON SCHEMA journal TO trader_proba;
GRANT SELECT ON journal.decision TO trader_proba;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON journal.decision FROM trader_proba;
