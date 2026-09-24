-- 0052: osobna rola bazodanowa dla skanera wiadomosci (`trader-news`)
--
-- POWOD
--
-- 0034 rozbilo jedna role `trader_app` na piec i uzasadnilo to zdaniem, ktore
-- jest sluszne: rozdzial uprawnien jest wart tyle, ile rozdzial POSWIADCZEN.
-- Tamto rozbicie szlo jednak wzdluz jednej osi - osi bezpiecznika: kto moze
-- zwolnic kill-switcha, kto moze ruszyc limit, kto tylko patrzy. Wszystkie
-- procesy zbierajace dane zostaly na wspolnej roli `trader_silnik`, bo pytanie
-- brzmialo wtedy „kto zatrzymuje handel", a nie „kto widzi klucze gieldy".
--
-- Stan na zywej bazie w dniu pisania tej migracji (2026-09-24):
--
--     trader_silnik | app.api_credential | SELECT
--     trader_silnik | app.secret         | SELECT
--
-- a pod `trader-news` dostaje w srodowisku `SECRETS_KEY` oraz `SECRETS_KEY_OLD`.
-- Czyli: szyfrogram kluczy gieldy ORAZ klucz, ktorym sie go odszyfrowuje,
-- w jednej przestrzeni adresowej. Przejecie tego procesu nie wymaga drugiego
-- kroku - obie polowy sa juz na miejscu.
--
-- DLACZEGO AKURAT TEN PROCES, A NIE „wszystkie po kolei"
--
-- `trader-news` jest JEDYNYM procesem w systemie, ktory przetwarza tresc od
-- obcych: pobiera i parsuje dziesiec kanalow RSS (XML i HTML z niekontrolowanych
-- zrodel) oraz obsluguje odpowiedzi zewnetrznych API modeli jezykowych. To jest
-- najwieksza powierzchnia ataku w calym wdrozeniu i jednoczesnie proces, ktory
-- nie handluje ani razu.
--
-- Ze skaner tych kluczy nie uzywa, nie jest domyslem: w `services/ingest/`
-- nie ma ani jednego odwolania do `app.api_credential`. Komplet miejsc, ktore
-- te tabele czytaja, to trzy pliki silnika - `loop.py`, `uzgodnienie.py`,
-- `waluta.py` - i wszystkie naleza do procesow rozmawiajacych z gielda
-- poswiadczeniami.
--
-- Dzisiaj ogranicznikiem szkody jest to, ze konto jest demonstracyjne, a tryb
-- `live` jest twardo zablokowany. To jest ograniczenie FAZY PROJEKTU, nie
-- ograniczenie architektury: w dniu wlaczenia trybu live ta sama konfiguracja
-- oddaje klucz do konta z prawdziwymi pieniedzmi.
--
-- CZEGO SKANER NAPRAWDE POTRZEBUJE
--
-- Wypisane z zapytan w `services/ingest/news_worker.py`, `llm.py` i `puls.py`,
-- a nie z domyslu o tym, „co worker danych zwykle robi":
--
--   news.feed                SELECT, UPDATE          stan odpytania kanalu
--   news.article             SELECT, INSERT, UPDATE  dedup i werdykt triazu
--   news.article_analysis    SELECT, INSERT          wynik analizy
--   news.article_instrument  INSERT                  powiazanie z instrumentem
--   news.api_spend           SELECT, INSERT          licznik limitu dobowego
--   ref.instrument           SELECT                  mapowanie symboli z analizy
--   risk.worker_heartbeat    SELECT, INSERT, UPDATE  wlasny puls (0035)
--   public.schema_migrations SELECT                  bramka startowa (0050)
--   klucz dostawcy LLM z `app.secret`                patrz nizej
--
-- I NIC WIECEJ. Nie ma tu `exec`, `journal`, `learn`, `feat`, `sig`, `market`,
-- `notify` ani `app."user"` - skaner nie siega do zadnego z nich. Rola silnika
-- dawala mu zapis do wszystkich osmiu schematow dziedzinowych; zostaje jeden.
--
-- CZY `SECRETS_KEY` JEST TAM W OGOLE POTRZEBNY - TAK, I DO CZEGO
--
-- Jest, i ma jedno zastosowanie: `_klucz()` w news_worker.py czyta
-- `app.secret.value_enc` i odszyfrowuje `SecretBox`-em klucz API dostawcy
-- analizy (Anthropic albo Gemini). Baza ma tu pierwszenstwo przed zmienna
-- srodowiskowa swiadomie - klucz wpisany w interfejsie dziala bez redeployu,
-- i tak jest opisany w chartcie.
--
-- Formalnie dalo by sie `SECRETS_KEY` z tego poda usunac i zostawic sama
-- sciezke zapasowa (`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` ze srodowiska).
-- Byloby to jednak cofniecie dzialajacej funkcji - wymiana klucza wracalaby do
-- edycji sekretu i restartu - i to bez zysku, o ktory chodzi: klucz do
-- szyfrogramu, ktorego proces nie moze przeczytac, nie otwiera niczego.
--
-- Dlatego zamiast usuwac KLUCZ, zawezamy SZYFROGRAM, do ktorego on pasuje.
-- Skaner przestaje widziec tabele `app.secret` i dostaje widok
-- `app.secret_llm`, ktory oddaje dokladnie dwa wiersze z kluczami dostawcow
-- analizy. Po tej zmianie `SECRETS_KEY` w tym podzie odszyfrowuje wylacznie to,
-- co ten pod ma prawo odszyfrowac.
--
-- Pelny rozdzial - osobny klucz szyfrujacy dla sekretow LLM i osobny dla kluczy
-- gieldy - jest mozliwy, ale jest inna zmiana: dotyka `services/api`,
-- `rotate_secrets.py` i wymaga przepisania istniejacych szyfrogramow. Widok
-- daje wiekszosc tego zysku dzisiaj i nie wymaga migracji danych.
--
-- KOLEJNOSC WDROZENIA (to samo juz raz wywrocilo te produkcje)
--
-- Rola musi ISTNIEC W BAZIE i miec HASLO, zanim chart wpisze ja workerowi.
-- Rola bez hasla nie zaloguje sie w ogole i pod nie wstanie - to jest
-- zachowanie zamierzone, dokladnie jak w 0034: pod, ktory nie wstal, widac
-- w `kubectl get pods`; proces chodzacy na cudzych poswiadczeniach nie widac
-- wcale. Haslo ustawia `db/migrate.py` ze zmiennej `NEWS_DB_PASSWORD`, wiec
-- klucz o tej nazwie musi byc w sekrecie `trader-secret` PRZED migracja.

BEGIN;

-- --------------------------------------------------------------------- rola
--
-- Hasla NIE MA tutaj, z tego samego powodu co w 0013 i 0034: haslo w pliku
-- migracji trafiloby do repozytorium i do sumy kontrolnej, a wtedy rotacja
-- wymagalaby edycji zastosowanego juz pliku - czego migrator slusznie zabrania.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trader_news') THEN
        CREATE ROLE trader_news LOGIN;
    END IF;
END $$;

GRANT CONNECT ON DATABASE trader TO trader_news;

-- ------------------------------------------------ bramka startowa (jak 0050)
--
-- Kontener startowy `czekaj-na-migracje` chodzi TA SAMA waska rola co worker
-- i pyta o numer najwyzszej zastosowanej migracji. Bez tego nadania konczy sie
-- na „permission denied for table schema_migrations" - w petli ponawiania,
-- gdzie wyglada dokladnie jak „baza jeszcze nie wstala".
--
-- Wylacznie odczyt: proces tla z prawem zapisu zameldowalby migracje, ktorej
-- nie zastosowano, i otworzyl sobie wlasna bramke. Bramka mierzylaby wtedy
-- swoj wlasny wpis zamiast stanu schematu.
GRANT USAGE ON SCHEMA public TO trader_news;
GRANT SELECT ON public.schema_migrations TO trader_news;

-- Bez ALTER DEFAULT PRIVILEGES w `public` - tak samo jak w 0050. Przyszla
-- tabela w tym schemacie ma byc dla skanera niewidoczna do czyjejs decyzji.

-- ------------------------------------------------------------ schemat `news`
--
-- Jedyny schemat, w ktorym skaner ma prawo zapisu, i jedyny, ktory w calosci
-- powstaje z jego pracy. Nadanie idzie tu na caly schemat, a nie tabela po
-- tabeli, swiadomie: kazda tabela w `news` jest wytworem tego procesu, wiec
-- wyliczanie ich z nazwy byloby druga kopia schematu, ktora rozjezdza sie przy
-- pierwszej nowej tabeli - i rozjezdza sie CICHO, bo brakujace nadanie objawia
-- sie dopiero przy pierwszym zapisie na produkcji.
GRANT USAGE ON SCHEMA news TO trader_news;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA news TO trader_news;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA news TO trader_news;
ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA news
    GRANT SELECT, INSERT, UPDATE ON TABLES TO trader_news;
ALTER DEFAULT PRIVILEGES FOR ROLE trader IN SCHEMA news
    GRANT USAGE, SELECT ON SEQUENCES TO trader_news;

-- Skaner NIE KASUJE. Artykul raz zapisany jest wejsciem warstwy uczenia
-- i punktem odniesienia dedupu po `content_hash`; proces, ktory potrafi go
-- usunac, potrafi tez usunac dowod na to, co widzial przed decyzja.
-- Nic wyzej tego prawa nie nadalo, wiec odebranie jest formalnie zbedne -
-- zostaje, bo jest jedynym miejscem mowiacym WPROST, czego tu nie ma.
REVOKE DELETE, TRUNCATE ON ALL TABLES IN SCHEMA news FROM trader_news;

-- ------------------------------------------------------------- schemat `ref`
--
-- Jedno zapytanie: `SELECT id, symbol FROM ref.instrument WHERE active`,
-- do zamiany symbolu zwroconego przez model na identyfikator instrumentu.
-- Tylko odczyt i tylko ta tabela - slownik instrumentow jest konfiguracja
-- systemu, a nie wytworem skanera.
GRANT USAGE ON SCHEMA ref TO trader_news;
GRANT SELECT ON ref.instrument TO trader_news;

-- Bez ALTER DEFAULT PRIVILEGES: nowa tabela w `ref` ma wymagac decyzji.

-- ------------------------------------------------------------ schemat `risk`
--
-- WYLACZNIE wlasny puls (0035). Zadnego dostepu do `risk.limit_def`,
-- `risk.killswitch_event` ani do pulsow silnika i dozorcy - skaner ich nie
-- czyta i nie ma powodu, zeby mogl.
--
-- To jest wezsze niz to, co dawala mu rola silnika: `trader_silnik` ma SELECT
-- na limitach i na dzienniku zatrzyman. Proces przetwarzajacy tresc od obcych
-- nie musi wiedziec, przy jakim progu obsuniecia system sie zatrzymuje.
GRANT USAGE ON SCHEMA risk TO trader_news;
GRANT SELECT, INSERT, UPDATE ON risk.worker_heartbeat TO trader_news;

-- Puls to jedyny dowod na to, ze worker zyje. Skasowanie wiersza kasuje
-- historie awarii razem z nim, a `TRUNCATE` zdjaloby puls WSZYSTKICH workerow
-- naraz - czyli uciszyloby czujke zywotnosci dla calego potoku danych.
REVOKE DELETE, TRUNCATE ON risk.worker_heartbeat FROM trader_news;

-- ZADNEGO ALTER DEFAULT PRIVILEGES w schemacie `risk`. To jest ten sam powod,
-- dla ktorego 0034 nadaje tu wszystko doslownie: `risk` to jedyne miejsce,
-- w ktorym „nikt nie pomyslal o uprawnieniach" znaczy „bezpiecznik jest
-- dostepny dla tego, co ma zatrzymywac".

-- ------------------------------------------------------------- schemat `app`
--
-- TU JEST CALA TRESC TEJ MIGRACJI.
--
-- Skaner dostaje USAGE na schemacie - bez niego nie siegnie do niczego - i ani
-- jednego nadania na tabele. Zamiast tabeli `app.secret` widzi widok, ktory
-- oddaje dokladnie te dwa wiersze, ktorych potrzebuje.
GRANT USAGE ON SCHEMA app TO trader_news;

-- WIDOK, A NIE NADANIE KOLUMNOWE NA `app.secret`.
--
-- Nadanie kolumnowe (`GRANT SELECT (name, value_enc)`) zawezilo by KOLUMNY,
-- ale nie WIERSZE - a to wiersze sa tu problemem. `app.secret` jest opisany
-- jako „sekrety konfiguracyjne aplikacji" i dzis mieszcza sie w nim same
-- klucze dostawcow analizy, ale to jest stan, a nie gwarancja: pierwszy sekret
-- dotyczacy pieniedzy, ktory ktos tam wpisze, bylby dla skanera czytelny od
-- chwili powstania. Widok z jawna lista nazw sprawia, ze taki sekret jest
-- niewidoczny DOMYSLNIE, a jego udostepnienie wymaga wrocenia do tego pliku.
--
-- Widok nalezy do roli `trader` i to JEJ uprawnienia decyduja o odczycie tabeli
-- pod spodem - dlatego `trader_news` nie potrzebuje (i nie dostaje) zadnego
-- nadania na sama `app.secret`.
--
-- Lista nazw jest zwiazana z kodem testem
-- `tests/test_rola_skanera_wiadomosci.py`: gdy ktos doda trzeciego dostawce do
-- `ZRODLA_KLUCZY` w news_worker.py i zapomni o tym pliku, test pada. Bez tego
-- wiazania nowy dostawca po cichu spadalby na sciezke zapasowa ze zmiennej
-- srodowiskowej i nikt by tego nie zauwazyl, bo worker pracuje dalej.
CREATE OR REPLACE VIEW app.secret_llm AS
    SELECT name, value_enc
      FROM app.secret
     WHERE name IN ('anthropic_api_key', 'gemini_api_key');

COMMENT ON VIEW app.secret_llm IS
'Klucze dostawcow analizy wiadomosci - jedyny szyfrogram, ktory widzi skaner
wiadomosci. Istnieje po to, zeby SECRETS_KEY w tamtym podzie nie pasowal do
niczego wiecej niz to, co ten pod ma prawo odszyfrowac.';

GRANT SELECT ON app.secret_llm TO trader_news;

-- Rola silnika dostaje ten sam widok, i to NIE jest poszerzenie jej uprawnien:
-- ma juz SELECT na calej `app.secret`, wiec widok jest scisle wezszy. Powod
-- jest wdrozeniowy - obraz z nowym kodem i zmiana chartu ida OSOBNO, wiec
-- miedzy nimi istnieje okno, w ktorym nowy `news_worker.py` chodzi jeszcze
-- rola silnika. Bez tego nadania skaner stracilby w tym oknie klucz dostawcy
-- i przestal analizowac, a kanaly RSS nie oddaja jutro tego, czego dzis nie
-- pobralismy.
GRANT SELECT ON app.secret_llm TO trader_silnik;

-- WLASNOSC, DLA KTOREJ POWSTALA TA MIGRACJA.
--
-- Nic wyzej nie nadalo skanerowi dostepu do zaszyfrowanych kluczy gieldy, wiec
-- te odebrania sa formalnie zbedne. Zostaja, bo sa jedynym miejscem w tym
-- pliku, ktore mowi WPROST, czego tu nie ma - a uprawnienie, ktorego brak jest
-- tylko brakiem, wraca przy pierwszym „dodajmy newsom SELECT, bo cos nie
-- dziala". Po tych linijkach takie dodanie trzeba bedzie usunac swiadomie.
REVOKE ALL ON app.api_credential FROM trader_news;
REVOKE ALL ON app.secret         FROM trader_news;

-- `app."user"` rowniez poza zasiegiem. Skaner nikogo nie powiadamia (nie ma
-- ani jednego zapisu do `notify.*`), wiec nie potrzebuje nawet identyfikatorow
-- uzytkownikow, ktore 0034 nadalo pozostalym procesom tla.
REVOKE ALL ON app."user" FROM trader_news;

-- Bez ALTER DEFAULT PRIVILEGES w schemacie `app` - tak samo jak w 0034.
-- Kazdy przyszly magazyn poswiadczen ma byc dla skanera niedostepny, dopoki
-- ktos swiadomie nie zdecyduje inaczej.

COMMIT;
