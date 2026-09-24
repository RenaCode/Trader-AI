-- 0054: osobna rola bazodanowa dla dziennika jakosciowego (`trader-postmortem`)
--
-- POWOD
--
-- 0052 rozstrzygnelo zasade, ktora ta migracja tylko stosuje po raz drugi:
-- proces przetwarzajacy tresc od obcych - a odpowiedz zewnetrznego modelu
-- jezykowego nia jest - nie moze dzielic roli z procesami widzacymi
-- zaszyfrowane klucze gieldy.
--
-- `services/engine/postmortem.py` robi obie rzeczy naraz, i to jest jego
-- istota: CZYTA pelna historie handlu (pozycje, decyzje, uzasadnienia) i WYSYLA
-- jej streszczenie do dostawcy LLM. Zaden istniejacy podzial nie pasuje:
--
--   trader_silnik  - ma `app.api_credential` i `app.secret`, czyli szyfrogram
--                    kluczy gieldy ORAZ (przez SECRETS_KEY w srodowisku) klucz
--                    do niego. Dokladnie ten uklad, ktory 0052 rozmontowalo.
--   trader_news    - nie widzi kluczy gieldy i to jest dobre, ale 0052 zapisalo
--                    wprost: "I NIC WIECEJ. Nie ma tu `exec`, `journal`,
--                    `learn` (...)". Poszerzenie tej roli o trzy schematy
--                    dziedzinowe cofneloby tamta migracje, zostawiajac jej
--                    uzasadnienie w pliku jako nieprawde.
--   trader_czujka  - "TYLKO CZYTA, poza powiadomieniami" (0034). Ten proces
--                    zapisuje `learn.postmortem` i dziennik wydatkow.
--
-- CZEGO TEN PROCES NAPRAWDE POTRZEBUJE
--
-- Wypisane z zapytan w `services/engine/postmortem.py`, a nie z domyslu:
--
--   exec.position                 SELECT   zamkniete pozycje okresu
--   ref.instrument                SELECT   symbol do promptu
--   journal.decision              SELECT   rezim, pewnosc, uzasadnienie
--   journal.decision_contribution SELECT   sygnaly i artykuly za decyzja
--   learn.postmortem              SELECT   koniec poprzedniego okresu
--                                 INSERT   zapis dziennika
--   news.api_spend                SELECT   obie bramki limitu dobowego
--                                 INSERT   wpis wlasnego wydatku
--   app.secret_llm                SELECT   klucz dostawcy analizy (widok z 0052)
--   app."user" (5 kolumn)         SELECT   odbiorcy powiadomienia
--   notify.*                      jak 0034 odbiorcy powiadomien
--   public.schema_migrations      SELECT   bramka startowa (0050)
--
-- I NIC WIECEJ. Nie ma tu `app.api_credential`, `app.secret`, `market`, `feat`,
-- `sig`, `risk` ani `exec.order`/`exec.fill`. Dziennik jakosciowy opisuje to,
-- co sie JUZ zdarzylo - nie sklada zlecen, nie liczy cech i nie dotyka
-- bezpiecznika.
--
-- ZAPIS OGRANICZONY DO DWOCH TABEL, I OBIE SA JEGO WLASNYM WYTWOREM. Proces,
-- ktory ocenia historie, nie ma prawa jej poprawic: `journal.decision`
-- i `journal.decision_contribution` sa zreszta niemutowalne triggerem (0005),
-- ale brak nadania jest tanszy niz poleganie na tym, ze trigger zostanie.
--
-- KOLEJNOSC WDROZENIA (to samo juz dwa razy wywrocilo te produkcje)
--
-- Rola musi ISTNIEC I MIEC HASLO, zanim chart wpisze ja zadaniu. Haslo ustawia
-- `db/migrate.py` ze zmiennej `POSTMORTEM_DB_PASSWORD`, wiec klucz o tej nazwie
-- musi byc w sekrecie `trader-secret` PRZED migracja. Bez niego rola zostaje
-- bez hasla, a Job sie nie zaloguje - i to jest zachowanie zamierzone: Job,
-- ktory nie wstal, widac w `kubectl get jobs`; proces chodzacy na cudzych
-- poswiadczeniach nie widac wcale.

BEGIN;

-- --------------------------------------------------------------------- rola
--
-- Hasla NIE MA tutaj, z tego samego powodu co w 0013, 0034 i 0052: haslo
-- w pliku migracji trafiloby do repozytorium i do sumy kontrolnej, a wtedy
-- rotacja wymagalaby edycji zastosowanego juz pliku.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trader_postmortem') THEN
        CREATE ROLE trader_postmortem LOGIN;
    END IF;
END $$;

GRANT CONNECT ON DATABASE trader TO trader_postmortem;

-- ------------------------------------------------ bramka startowa (jak 0050)
--
-- Kontener startowy `czekaj-na-migracje` chodzi TA SAMA waska rola co zadanie.
-- Bez tego nadania konczy sie na "permission denied for table
-- schema_migrations" - w petli ponawiania, gdzie wyglada dokladnie jak "baza
-- jeszcze nie wstala". Wylacznie odczyt: proces z prawem zapisu zameldowalby
-- migracje, ktorej nie zastosowano, i otworzyl sobie wlasna bramke.
GRANT USAGE ON SCHEMA public TO trader_postmortem;
GRANT SELECT ON public.schema_migrations TO trader_postmortem;

-- ------------------------------------------------------- historia do oceny
--
-- Sam odczyt, trzy schematy. `decision_contribution` jest tu rownie wazne co
-- `decision`: bez niego dziennik wiedzialby, ZE pozycja przegrala, ale nie
-- KTORY sygnal i ktory artykul za nia staly - czyli nie mialby z czego
-- postawic hipotezy nadajacej sie do sprawdzenia.
GRANT USAGE ON SCHEMA exec, ref, journal TO trader_postmortem;
GRANT SELECT ON exec.position TO trader_postmortem;
GRANT SELECT ON ref.instrument TO trader_postmortem;
GRANT SELECT ON journal.decision, journal.decision_contribution TO trader_postmortem;

-- Bez ALTER DEFAULT PRIVILEGES w tych schematach: przyszla tabela z historia
-- handlu ma byc dla tego procesu niewidoczna do czyjejs decyzji. Proces, ktory
-- wysyla tresc na zewnatrz, nie powinien dostawac nowych zrodel automatycznie.

-- -------------------------------------------------------- wlasny wytwor
GRANT USAGE ON SCHEMA learn TO trader_postmortem;
GRANT SELECT, INSERT ON learn.postmortem TO trader_postmortem;
GRANT USAGE, SELECT ON SEQUENCE learn.postmortem_id_seq TO trader_postmortem;

-- --------------------------------------------------- dziennik wydatkow LLM
--
-- TEN SAM dziennik i te same dwa progi, ktorymi chodzi skaner wiadomosci.
-- Osobny licznik dla tego procesu znaczylby, ze dwa procesy wydaja z jednego
-- konta, pilnujac dwoch roznych sum - czyli ze zaden nie pilnuje niczego.
-- `purpose='postmortem'` rozroznia je w rozliczeniu, nie w limicie.
GRANT USAGE ON SCHEMA news TO trader_postmortem;
GRANT SELECT, INSERT ON news.api_spend TO trader_postmortem;
GRANT USAGE, SELECT ON SEQUENCE news.api_spend_id_seq TO trader_postmortem;

-- NIE MA TU `news.article` ANI `news.article_analysis`. Etykiety artykulow,
-- ktore trafiaja do promptu, sa juz przepisane do
-- `journal.decision_contribution.label` w chwili decyzji - proces oceniajacy
-- nie musi wiec siegac do surowej tresci od obcych, zeby o niej opowiedziec.

-- ------------------------------------------------------- klucz dostawcy LLM
--
-- Widok z 0052, nie tabela `app.secret`. `SECRETS_KEY` w tym podzie odszyfrowuje
-- wylacznie klucze dostawcow analizy - szyfrogram kluczy gieldy jest stad
-- niewidoczny, wiec klucz do niego nie otwiera tutaj niczego.
GRANT USAGE ON SCHEMA app TO trader_postmortem;
GRANT SELECT ON app.secret_llm TO trader_postmortem;

-- Odbiorcy powiadomienia. Te same piec kolumn co w 0034 - bez
-- `password_hash`, `locked_until` i `failed_logins`.
GRANT SELECT (id, login, email, is_active, role) ON app."user" TO trader_postmortem;

-- ------------------------------------------------------------ powiadomienia
--
-- Komplet jak dla pozostalych procesow powiadamiajacych (0034). `postmortem.ready`
-- istnieje w katalogu od 0011 i do 2026-09-24 nie zostal wyslany ANI RAZU,
-- bo nie bylo procesu, ktory by go wysylal.
GRANT USAGE ON SCHEMA notify TO trader_postmortem;
GRANT SELECT ON ALL TABLES IN SCHEMA notify TO trader_postmortem;
GRANT INSERT ON notify.notification, notify.delivery TO trader_postmortem;
-- Wylacznie slad po wysylce. Kanal jest konfiguracja uzytkownika i zmienia go
-- tylko API.
GRANT UPDATE (last_sent_at, last_error) ON notify.channel TO trader_postmortem;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA notify TO trader_postmortem;
ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA notify
    GRANT SELECT ON TABLES TO trader_postmortem;

COMMIT;
