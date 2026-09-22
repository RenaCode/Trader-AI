-- Role procesów czytają numer migracji — bramka startowa workerów.
--
-- DLACZEGO TA MIGRACJA W OGÓLE ISTNIEJE
--
-- InitContainer `migrate` stoi wyłącznie przy API (charts/.../api.yaml). Dziewięć
-- Deploymentów workerów chodzi na TYM SAMYM obrazie co API, ze `strategy:
-- Recreate`, więc przy podbiciu tagu restartują się natychmiast — równolegle
-- z migracją, nie po niej. Przez cały czas jej trwania nowy kod workerów
-- pracuje na starym schemacie. Gdy migracja padnie, API nie wstaje i to widać;
-- pętla decyzyjna i dozorca ryzyka chodzą dalej, na nowym kodzie i starym
-- schemacie, i nie mówi o tym nic.
--
-- Bramką jest kontener startowy `czekaj-na-migracje`, który pyta bazę o numer
-- najwyższej ZASTOSOWANEJ migracji i porównuje go z numerem najwyższej migracji
-- OBECNEJ W OBRAZIE. To jedno zapytanie:
--
--     SELECT version FROM public.schema_migrations
--
-- i właśnie na nie nie było nadania.
--
-- DLACZEGO TEGO NADANIA NIE BYŁO WCZEŚNIEJ
--
-- 0014 powstało, gdy istniała jedna rola aplikacji i jeden czytelnik tej tabeli:
-- `/api/health` raportujący wersję schematu. Nadało więc SELECT dokładnie temu
-- jednemu czytelnikowi — `trader_app` — i nikomu innemu, bo nikogo innego nie
-- było.
--
-- 0034 rozbiło jedną rolę na pięć, ale zajmowało się schematami DZIEDZINOWYMI:
-- kto handluje, kto zwalnia kill-switch, kto tylko czyta. Pętla nadań w tamtym
-- pliku obejmuje `ref, market, news, feat, sig, journal, exec, learn` i celowo
-- nie dotyka `risk` ani `app`; schematu `public` nie ma w niej w ogóle, bo
-- `public.schema_migrations` nie był wtedy potrzebny żadnemu procesowi tła.
-- To nie było przeoczenie — to był stan faktyczny do dzisiaj.
--
-- Bez tej migracji kontener startowy workera, uruchomiony jako `trader_silnik`
-- albo `trader_dozorca`, kończy się na `permission denied for table
-- schema_migrations`. I to jest najgorsze możliwe miejsce na taki błąd:
-- w kontenerze startowym, w pętli ponawiania, gdzie wygląda dokładnie jak
-- „baza jeszcze nie wstała" — czyli jak stan, który ma minąć sam.
--
-- CZTERY ROLE, A NIE DWIE
--
-- Bramka stoi dziś przy workerach, które chodzą rolą `silnik` (osiem) albo
-- `dozorca` (jeden). `czujka` i `proba` dostają nadanie razem z nimi, mimo że
-- ich zadania cykliczne bramki nie mają. Powód jest ten sam, dla którego ta
-- migracja w ogóle powstała: proces, który jutro dostanie bramkę, padłby
-- w kontenerze startowym na brak SELECT-a — czyli dokładnie tym błędem,
-- którego tu unikamy, i dokładnie tam, gdzie najtrudniej go rozpoznać.
--
-- To nie jest wyłom w rozdziale uprawnień z 0034. `schema_migrations` zawiera
-- numer, sumę kontrolną pliku z repozytorium, znacznik czasu i czas trwania —
-- nie ma w niej ani jednej danej, której proces tła nie mógłby zobaczyć gdzie
-- indziej. Rozdział ról broni prawa ZAPISU do bezpiecznika; odczyt numeru
-- wersji schematu nie należy do tej klasy.
--
-- WYŁĄCZNIE ODCZYT
--
-- Zapis do tej tabeli należy do migratora, który chodzi rolą właściciela — tak
-- samo jak w 0014. Proces tła, który mógłby dopisać wiersz, potrafiłby
-- zameldować migrację, której nie zastosowano, i otworzyć sobie własną bramkę.
-- Wtedy bramka mierzyłaby swój własny wpis zamiast stanu schematu.

BEGIN;

-- USAGE jest w Postgresie 15+ nadane roli PUBLIC i formalnie tego wiersza nie
-- potrzeba. Zostaje jawnie, z tego samego powodu co w 0014: nadanie, które
-- istnieje tylko jako domyślność serwera, znika przy pierwszej zmianie tej
-- domyślności albo przy `REVOKE ... FROM PUBLIC` w utwardzaniu bazy — i wtedy
-- przestaje działać coś, co nigdzie nie jest zapisane, że działało.
GRANT USAGE ON SCHEMA public TO trader_silnik, trader_dozorca,
                                trader_czujka, trader_proba;

GRANT SELECT ON public.schema_migrations TO trader_silnik, trader_dozorca,
                                             trader_czujka, trader_proba;

-- Bez ALTER DEFAULT PRIVILEGES dla schematu `public`. Nadanie dotyczy JEDNEJ
-- tabeli, o której wiadomo, co zawiera. Przyszła tabela w `public` ma być dla
-- procesów tła niewidoczna, dopóki ktoś świadomie nie zdecyduje inaczej.

COMMIT;
