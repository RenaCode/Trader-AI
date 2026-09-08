-- 0032: osobna rola bazodanowa dla każdego procesu — kto może zwolnić kill-switch
--
-- POWÓD
--
-- Do tej pory silnik, dozorca ryzyka, czujka żywotności, próba pożarowa i API
-- łączyły się do bazy JEDNĄ rolą `trader_app`. Skutek jest zapisany wprost
-- w docs/06-architektura-bezpiecznika.md, w rozdziale „Czego ta architektura
-- nadal nie robi":
--
--     Silnik i dozorca chodzą na tej samej roli bazodanowej, więc nic nie
--     WYMUSZA, że silnik nie zwolni sobie kill-switcha. Dziś nie robi tego,
--     bo nie ma takiego kodu — nie dlatego, że nie może.
--
-- Cała architektura bezpiecznika stoi na zdaniu „nadzorca nie może dzielić losu
-- z nadzorowanym". Dopóki nadzorowany ma poświadczenia nadzorcy, to zdanie jest
-- konwencją w komentarzu, a nie własnością systemu. Wystarczy jedna linijka
-- dopisana w pośpiechu o trzeciej w nocy („silnik stoi, odblokuję go w pętli"),
-- jedno wstrzyknięcie SQL przez dane rynkowe albo jeden błąd w module egzekucji,
-- żeby proces, który właśnie traci pieniądze, sam sobie zdjął hamulec. Nie
-- byłoby po tym śladu innego niż `released_auto` w wierszu, którego i tak nikt
-- nie czyta w nocy.
--
-- Od tej migracji to jest niemożliwe na poziomie GRANT-u: rola silnika nie ma
-- UPDATE ani DELETE na risk.killswitch_event. Kod, który spróbuje, dostanie
-- InsufficientPrivilege i pod stanie — czyli awaria będzie widoczna zamiast
-- cicho skutecznej.
--
-- PODZIAŁ NA PIĘĆ RÓL
--
--   trader          właściciel schematów; wyłącznie migracje i konserwacja
--   trader_app      API wystawione na internet (bez zmian z 0013, poza
--                   zawężeniem zapisu na risk.* — patrz sekcja na końcu)
--   trader_silnik   pętla decyzyjna i procesy zbierające dane; HANDLUJE
--   trader_dozorca  nadzorca ryzyka; ZWALNIA kill-switch, ale nie handluje
--   trader_czujka   czujka żywotności i uzgodnienie z giełdą; TYLKO CZYTA
--   trader_proba    próba pożarowa kill-switcha; jedyna rola poza właścicielem,
--                   która może ruszyć wartość limitu
--
-- ROZDZIAŁ IDZIE W OBIE STRONY, i to jest w nim najważniejsze:
--
--   * silnik nie może zwolnić zatrzymania, które go dotyczy,
--   * dozorca nie może wysłać zlecenia ani zapisać pozycji — a więc pomyłka
--     w nadzorcy nie może zamienić się w transakcję,
--   * dozorca nie ma dostępu do zaszyfrowanych kluczy giełdy (app.api_credential).
--     docs/06 uzasadnia, dlaczego dozorca nie zamyka pozycji: „wymaga kluczy
--     giełdy w kolejnym procesie, co poszerza powierzchnię ataku". Skoro taka
--     jest decyzja, to baza ma ją egzekwować, a nie tylko dokumentacja.
--
-- DLACZEGO UPRAWNIENIA KOLUMNOWE, A NIE TABELOWE
--
-- `GRANT UPDATE ON risk.killswitch_event` pozwoliłby dozorcy nie tylko zwolnić
-- zatrzymanie, ale też przepisać `reason` i `triggered_at` — czyli zmienić
-- historię tego, dlaczego i kiedy system się zatrzymał. Dziennik zatrzymań jest
-- jedynym źródłem odpowiedzi na pytanie „czy bezpiecznik działał", więc prawo
-- zapisu kończy się na trzech kolumnach zwolnienia.
--
-- Tak samo próba pożarowa: podnosi `value` limitu i przywraca je w `finally`.
-- Nie ma prawa dotknąć `enabled` ani `action`, bo wyłączony limit nie wraca
-- sam po błędzie procesu — zostałby wyłączony na stałe i nikt by tego nie
-- zauważył, dopóki nie byłby potrzebny.

BEGIN;

-- --------------------------------------------------------------- role
--
-- Hasła NIE są tutaj. Ustawia je db/migrate.py ze zmiennych środowiskowych,
-- z tego samego powodu co hasło `trader_app` w 0013: hasło w pliku migracji
-- trafiłoby do repozytorium i do sumy kontrolnej, a wtedy rotacja wymagałaby
-- edycji zastosowanego już pliku.
--
-- Rola bez ustawionego hasła nie zaloguje się w ogóle — pod nie wstanie i widać
-- to w `kubectl get pods`. To jest zamierzone: cicha praca na cudzych
-- poświadczeniach byłaby gorsza niż nieuruchomiony pod.
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['trader_silnik','trader_dozorca',
                             'trader_czujka','trader_proba']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I LOGIN', r);
        END IF;
    END LOOP;
END $$;

GRANT CONNECT ON DATABASE trader TO trader_silnik, trader_dozorca,
                                    trader_czujka, trader_proba;

-- ------------------------------------------------- schematy dziedzinowe (dane)
--
-- Tu podziału nie ma i celowo go nie robimy: to są dane rynkowe, cechy, dziennik
-- decyzji i wyniki treningu. Rozdrabnianie ich na role nie chroni przed niczym,
-- czego nie chroni już 0013 (brak własności tabel => brak DDL, brak wyłączenia
-- wyzwalacza niezmienności dziennika), a każda nowa tabela byłaby okazją do
-- rozjazdu i awarii po wdrożeniu.
--
-- Pętla NIE DOTYKA schematów `risk` ani `app`. Tam każdy GRANT jest wypisany
-- dosłownie niżej, żeby dało się go przeczytać i przetestować bez uruchamiania
-- bazy. Test tests/test_role_bazodanowe.py pilnuje, że ta pętla nigdy nie
-- obejmie tych dwóch schematów — inaczej audyt uprawnień krytycznych czytałby
-- połowę prawdy.
DO $$
DECLARE s text;
BEGIN
    FOREACH s IN ARRAY ARRAY['ref','market','news','feat','sig','journal',
                             'exec','learn']
    LOOP
        -- Silnik i procesy zbierające dane: pełny zapis, tak jak dotąd.
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO trader_silnik', s);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA %I '
                       'TO trader_silnik', s);
        EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I '
                       'TO trader_silnik', s);
        EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO trader_silnik', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT SELECT, INSERT, UPDATE ON TABLES TO trader_silnik', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT USAGE, SELECT ON SEQUENCES TO trader_silnik', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT EXECUTE ON FUNCTIONS TO trader_silnik', s);

        -- Czujka żywotności odpowiada na pytanie „czy system jest w stanie,
        -- o którym człowiek MUSI wiedzieć". Odpowiedź na to pytanie nigdy nie
        -- wymaga zapisu, więc czujka nie ma prawa zapisu nigdzie poza
        -- powiadomieniami. Proces diagnostyczny, który potrafi zmienić stan,
        -- prędzej czy później go zmieni — i wtedy diagnoza mierzy siebie.
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO trader_czujka', s);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO trader_czujka', s);
        EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO trader_czujka', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT SELECT ON TABLES TO trader_czujka', s);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA %I '
                       'GRANT EXECUTE ON FUNCTIONS TO trader_czujka', s);
    END LOOP;
END $$;

-- Dozorca i próba widzą z danych wyłącznie to, na czym opierają decyzję:
-- krzywą kapitału i otwarte pozycje. Nie ma tu `journal` ani `learn`, bo
-- nadzorca, który czyta uzasadnienia decyzji, zaczyna je oceniać — a to jest
-- rola silnika.
GRANT USAGE ON SCHEMA exec, ref TO trader_dozorca, trader_proba;
GRANT SELECT ON exec.equity_point, exec.position, exec.order
    TO trader_dozorca, trader_proba;
GRANT SELECT ON ref.instrument TO trader_dozorca, trader_proba;

-- Brak INSERT/UPDATE na exec.order jest tu istotą, nie szczegółem: dozorca
-- ZATRZYMUJE handel i nie ma innego powodu, żeby dotykać zleceń. Gdyby miał
-- prawo zapisu, błąd w nadzorcy mógłby wysłać zlecenie — czyli mechanizm
-- powołany do ograniczania ryzyka stałby się jego źródłem.
REVOKE INSERT, UPDATE, DELETE ON exec.order, exec.fill, exec.position,
                                 exec.equity_point
    FROM trader_dozorca, trader_proba;

-- --------------------------------------------------------------- schemat risk
--
-- Tutaj jest cała treść tej migracji. Każdy GRANT wypisany dosłownie.

GRANT USAGE ON SCHEMA risk TO trader_silnik, trader_dozorca,
                              trader_czujka, trader_proba;

-- ---- silnik ---------------------------------------------------------------
--
-- Silnik CZYTA kill-switch (żeby wstrzymać wejścia) i ŻĄDA go (bo ocenia limity
-- w każdym cyklu — to obrona w głąb, nie duplikat dozorcy). Nie zwalnia.
GRANT SELECT ON risk.limit_def, risk.killswitch_event, risk.engine_heartbeat
    TO trader_silnik;
GRANT INSERT ON risk.killswitch_event, risk.limit_usage, risk.violation
    TO trader_silnik;
GRANT INSERT, UPDATE ON risk.engine_heartbeat TO trader_silnik;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA risk TO trader_silnik;

-- WŁASNOŚĆ, DLA KTÓREJ POWSTAŁA TA MIGRACJA.
--
-- Nic wyżej nie nadało silnikowi UPDATE ani DELETE na tabeli zatrzymań, więc to
-- odebranie jest formalnie zbędne. Zostaje, bo jest jedynym miejscem w tym
-- pliku, które mówi wprost, czego tu NIE MA — a uprawnienie, którego brak jest
-- tylko brakiem, wraca przy pierwszym „dodajmy silnikowi UPDATE, bo coś nie
-- działa". Po tej linijce takie dodanie trzeba usunąć świadomie.
REVOKE UPDATE, DELETE, TRUNCATE ON risk.killswitch_event FROM trader_silnik;

-- Zmiana limitu jest równoważna zwolnieniu kill-switcha, tylko cichsza:
-- podniesienie progu obsunięcia z 10% na 90% nie zostawia wiersza w dzienniku
-- zatrzymań i nie wysyła powiadomienia. Silnik czyta limity i nic więcej.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.limit_def FROM trader_silnik;

-- Puls dozorcy to jedyny dowód, że nadzorca żyje. Gdyby silnik mógł go
-- odświeżać, martwy dozorca wyglądałby w czujce dokładnie jak żywy — a wtedy
-- ostatnie ogniwo łańcucha (docs/06) meldowałoby zdrowie tego, czego już nie ma.
REVOKE ALL ON risk.dozorca_heartbeat FROM trader_silnik;

-- Nowe tabele w schemacie `risk` mają być dla silnika CZYTELNE, ale nie
-- zapisywalne. Domyślne uprawnienie z prawem zapisu oznaczałoby, że kolejny
-- mechanizm bezpieczeństwa dostaje silnikowi prawo zapisu automatycznie —
-- czyli że ten rozdział trzyma się do najbliższej migracji.
ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA risk
    GRANT SELECT ON TABLES TO trader_silnik;

-- ---- dozorca --------------------------------------------------------------
--
-- Dozorca ocenia limity tym samym risk kernelem co silnik, więc żąda zatrzymania
-- tak samo (INSERT) — i jako jedyny proces automatyczny je zwalnia.
GRANT SELECT ON risk.limit_def, risk.killswitch_event TO trader_dozorca;
GRANT INSERT ON risk.killswitch_event TO trader_dozorca;

-- ZWOLNIENIE TO TRZY KOLUMNY, NIE CAŁY WIERSZ. Powód i moment zadziałania są
-- poza zasięgiem dozorcy: to jest dziennik tego, czy bezpiecznik zadziałał,
-- a proces, który potrafi go przepisać, nie jest już dowodem.
GRANT UPDATE (released_at, released_auto, release_note)
    ON risk.killswitch_event TO trader_dozorca;

GRANT SELECT, INSERT, UPDATE ON risk.dozorca_heartbeat TO trader_dozorca;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA risk TO trader_dozorca;

-- Dozorca nie zmienia limitów. Wznowienie ma iść „na dowodzie, że warunek
-- ustał" — a najprostszym sposobem spełnienia warunku jest przesunięcie progu.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.limit_def FROM trader_dozorca;

-- ---- czujka ---------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA risk TO trader_czujka;
ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA risk
    GRANT SELECT ON TABLES TO trader_czujka;

-- Czujka pilnuje OBU pulsów (silnika i dozorcy) i zgłasza zatrzymania. Zapis
-- do czegokolwiek w `risk` uczyniłby ją uczestnikiem tego, co obserwuje.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA risk
    FROM trader_czujka;

-- ---- próba pożarowa -------------------------------------------------------
--
-- Próba podnosi próg kapitału powyżej bieżącego stanu, czeka aż SILNIK sam
-- wykryje przekroczenie, i zwalnia zdarzenie, które sama wywołała.
GRANT SELECT ON risk.limit_def, risk.killswitch_event TO trader_proba;
GRANT UPDATE (value, updated_at) ON risk.limit_def TO trader_proba;
GRANT UPDATE (released_at, released_auto, release_note)
    ON risk.killswitch_event TO trader_proba;

-- Próba NIE MOŻE wyłączyć limitu ani zmienić jego akcji. Podniesiona wartość
-- wraca w bloku `finally`, więc przeżywa błąd i przerwanie procesu; `enabled`
-- ustawione na false przez proces, który zginął, nie wróci nigdy i nikt tego
-- nie zauważy, bo wyłączony limit wygląda dokładnie jak limit niespełniony.
REVOKE INSERT, DELETE, TRUNCATE ON risk.limit_def FROM trader_proba;
REVOKE INSERT, DELETE, TRUNCATE ON risk.killswitch_event FROM trader_proba;

-- --------------------------------------------------------------- schemat app
--
-- Tu mieszkają konta, sesje, sekrety i zaszyfrowane klucze giełdy. Żaden proces
-- tła nic tu nie zapisuje (sprawdzone: w services/engine i services/ingest nie
-- ma ani jednego INSERT/UPDATE na app.*), więc nadajemy wyłącznie odczyt — i to
-- kolumnowo tam, gdzie tabela zawiera coś więcej niż to, czego proces używa.
GRANT USAGE ON SCHEMA app TO trader_silnik, trader_dozorca, trader_czujka;

-- Powiadomienia idą do aktywnych użytkowników, więc procesy potrzebują ich
-- identyfikatorów. NIE potrzebują `password_hash`, `locked_until` ani
-- `failed_logins` — a wyciek hasha z procesu, który dotyka danych z internetu
-- (skaner wiadomości, dane rynkowe), byłby najdroższą możliwą konsekwencją
-- błędu w parsowaniu cudzej treści.
GRANT SELECT (id, login, email, is_active, role) ON app."user"
    TO trader_silnik, trader_dozorca, trader_czujka;

-- Klucze giełdy: silnik handluje, więc ich potrzebuje; uzgodnienie z giełdą
-- (czujka) porównuje nasz obraz pozycji z obrazem giełdy, więc też.
--
-- DOZORCY TU NIE MA i to jest decyzja, nie przeoczenie. docs/06 tłumaczy,
-- dlaczego dozorca nie domyka pozycji awaryjnie: wymagałoby to kluczy giełdy
-- w kolejnym procesie, co poszerza powierzchnię ataku. Dopóki ta decyzja
-- obowiązuje, dozorca nie ma nawet z czego odszyfrować klucza.
GRANT SELECT ON app.api_credential TO trader_silnik, trader_czujka;
GRANT SELECT ON app.secret TO trader_silnik;
GRANT SELECT ON app.backup_run TO trader_czujka;

-- Bez ALTER DEFAULT PRIVILEGES dla schematu `app`: nowa tabela z sekretami ma
-- być niedostępna procesom tła DOPÓKI ktoś świadomie nie nada do niej prawa.
-- Domyślny odczyt oznaczałby, że każdy przyszły magazyn poświadczeń jest
-- czytelny dla skanera wiadomości od chwili powstania.

-- ------------------------------------------------------------ schemat notify
--
-- Powiadomienie jest jedynym zapisem, jaki wolno zrobić procesowi nadzorującemu.
-- System, który sam siebie zatrzymuje i sam wznawia w ciszy, jest
-- nieodróżnialny od systemu, który nie robi ani jednego, ani drugiego.
GRANT USAGE ON SCHEMA notify TO trader_silnik, trader_dozorca, trader_czujka;
GRANT SELECT ON ALL TABLES IN SCHEMA notify
    TO trader_silnik, trader_dozorca, trader_czujka;
GRANT INSERT ON notify.notification, notify.delivery
    TO trader_silnik, trader_dozorca, trader_czujka;

-- Wyłącznie ślad po wysyłce. Kanał (adres, token WhatsApp) jest konfiguracją
-- użytkownika i zmienia go tylko API.
GRANT UPDATE (last_sent_at, last_error) ON notify.channel
    TO trader_silnik, trader_dozorca, trader_czujka;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA notify
    TO trader_silnik, trader_dozorca, trader_czujka;
ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA notify
    GRANT SELECT ON TABLES TO trader_silnik, trader_dozorca, trader_czujka;

-- Próba pożarowa liczy powiadomienia (ogniwo 4 łańcucha), ale sama żadnego nie
-- wysyła — inaczej sprawdzałaby własną wysyłkę zamiast wysyłki dozoru.
GRANT USAGE ON SCHEMA notify TO trader_proba;
GRANT SELECT ON notify.notification TO trader_proba;

-- ---------------------------------------------- zawężenie roli API (trader_app)
--
-- 0013 nadało `trader_app` SELECT, INSERT i UPDATE na WSZYSTKICH tabelach
-- jedenastu schematów, więc API do dziś może wstawić zdarzenie kill-switcha,
-- przepisać powód zadziałania i zmienić dowolny limit. Żadnej z tych rzeczy nie
-- robi: w services/api nie ma ani jednego zapisu do risk.limit_def, a jedyny
-- zapis do risk.killswitch_event to ręczne zwolnienie przez zalogowanego
-- właściciela (CSRF, sesja, wpis w audycie).
--
-- API jest jedyną z tych ról wystawioną na internet, więc jest też jedyną,
-- w której zbędne uprawnienie ma czym zostać wykorzystane.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.killswitch_event FROM trader_app;
GRANT UPDATE (released_at, released_by, release_note)
    ON risk.killswitch_event TO trader_app;

-- `released_auto` świadomie POZA nadaniem dla API: zwolnienie przez człowieka
-- ma zostać zwolnieniem przez człowieka. Gdyby API mogło ustawić tę flagę,
-- odpowiedź na pytanie „czy to system wrócił do handlu sam" przestałaby być
-- rozstrzygalna z bazy.

REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON risk.limit_def FROM trader_app;

-- Zmiana limitów zostaje przy właścicielu (migracja) i przy próbie pożarowej.
-- Gdy limity trafią kiedyś do interfejsu, ten GRANT wróci — świadomie, razem
-- z ekranem, potwierdzeniem i wpisem w audycie, a nie przy okazji.

COMMIT;
