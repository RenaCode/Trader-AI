-- 0029: reguła wznowienia per limit — kill-switch zwalnia się sam, na dowodzie
--
-- POWÓD
--
-- Obsługa ma być autonomiczna. Zwolnienie kill-switcha wymagało człowieka przy
-- klawiaturze, więc każde zadziałanie zatrzymywało system do czasu, aż ktoś
-- zajrzy — w nocy oznacza to do rana.
--
-- Jednocześnie „kill-switch, który wygasa sam" jest sprzecznością: automatyczne
-- wznowienie po obsunięciu to najprostsza droga do powtórzenia tej samej straty.
--
-- Rozwiązaniem nie jest wybór między jednym a drugim, tylko zauważenie, że
-- TRZY LIMITY KRYTYCZNE ZNACZĄ TRZY RÓŻNE RZECZY, a system traktował je
-- identycznie:
--
--   max_daily_loss   limit DZIENNY. Mija razem z dobą — z definicji. Żądanie
--                    ręcznego zwolnienia limitu dziennego oznacza, że po
--                    pierwszym zadziałaniu system stoi, dopóki ktoś nie
--                    kliknie, czyli limit dzienny staje się dożywotni.
--
--   max_drawdown     sygnał o kondycji strategii. Wznowienie ma sens dopiero
--                    po ODBUDOWIE kapitału, a nie po upływie czasu.
--
--   min_equity       granica kapitału właściciela. Jej przekroczenie znaczy, że
--                    system złamał jedyną obietnicę, która była warunkiem
--                    wpłaty. Tego nie zwalnia się automatem.
--
-- HISTEREZA I LICZNIK, czyli dlaczego to nie jest po prostu poluzowanie
--
-- Wznowienie następuje przy progu CIAŚNIEJSZYM niż zadziałanie (połowa limitu),
-- więc system nie może oscylować wokół granicy. Do tego każde automatyczne
-- wznowienie jest liczone: po `max_wznowien` w ciągu doby limit przechodzi
-- w tryb ręczny sam z siebie. System wraca do handlu, ale nie w kółko —
-- powtarzające się zatrzymania znaczą, że dowód jest fałszywy, i wtedy człowiek
-- jest potrzebny.

BEGIN;

-- Które zdarzenie od którego limitu. Do tej pory kill-switch zapisywał wyłącznie
-- powód po polsku; reguła wznowienia musi wiedzieć, KTÓRY limit zadziałał,
-- a wyciąganie tego z tekstu działałoby do pierwszej zmiany sformułowania.
ALTER TABLE risk.killswitch_event ADD COLUMN IF NOT EXISTS limit_name text;

COMMENT ON COLUMN risk.killswitch_event.limit_name IS
'Nazwa limitu, który zażądał zatrzymania. NULL w zdarzeniach sprzed 0029 —
traktowane jako ręczne, bo nie wiadomo, jaka reguła miałaby je zwolnić.';

-- Kto zwolnił: człowiek czy system. Bez tego dziennik nie odróżnia zwolnienia
-- przemyślanego od automatycznego, a to jest pierwsza rzecz, o którą się pyta
-- po serii zatrzymań.
ALTER TABLE risk.killswitch_event ADD COLUMN IF NOT EXISTS released_auto boolean
    NOT NULL DEFAULT false;

ALTER TABLE risk.limit_def ADD COLUMN IF NOT EXISTS wznowienie text
    NOT NULL DEFAULT 'reczne'
    CHECK (wznowienie IN ('reczne', 'po_ustaniu', 'po_odbudowie'));

COMMENT ON COLUMN risk.limit_def.wznowienie IS
'Jak kill-switch wywołany tym limitem wraca do handlu:
  reczne       - wyłącznie przez człowieka (granice kapitału)
  po_ustaniu   - gdy warunek przestał obowiązywać (limity okresowe)
  po_odbudowie - gdy miara wróciła poniżej POŁOWY limitu (histereza)';

ALTER TABLE risk.limit_def ADD COLUMN IF NOT EXISTS max_wznowien integer
    NOT NULL DEFAULT 3;

COMMENT ON COLUMN risk.limit_def.max_wznowien IS
'Ile razy na dobę limit może zwolnić się sam. Po przekroczeniu zdarzenie czeka
na człowieka: powtarzające się zatrzymania znaczą, że warunek wznowienia jest
spełniany, a problem trwa.';

UPDATE risk.limit_def SET wznowienie = 'po_ustaniu'
 WHERE name = 'max_daily_loss' AND action = 'killswitch';

UPDATE risk.limit_def SET wznowienie = 'po_odbudowie'
 WHERE name = 'max_drawdown' AND action = 'killswitch';

-- Jawnie, mimo że to wartość domyślna: to jest DECYZJA, a nie brak decyzji.
UPDATE risk.limit_def SET wznowienie = 'reczne'
 WHERE name = 'min_equity';

-- PULS DOZORCY — w osobnej tabeli, nie w risk.engine_heartbeat.
--
-- Tamta ma `engine` typu ref.engine w kluczu głównym, więc dopisanie się do niej
-- wymagałoby rozszerzenia enuma silników handlowych o coś, co silnikiem nie
-- jest. Dozorca nadzoruje silniki, a nie handluje.
--
-- Bez tej tabeli czujka żywotności nie ma czego pilnować po stronie dozorcy —
-- a nadzorca, którego nikt nie nadzoruje, jest tylko przesunięciem problemu
-- o jeden poziom.
CREATE TABLE IF NOT EXISTS risk.dozorca_heartbeat (
    nazwa        text        PRIMARY KEY,
    last_beat_at timestamptz NOT NULL,
    detail       jsonb
);

COMMENT ON TABLE risk.dozorca_heartbeat IS
'Puls procesów nadzorujących. Osobno od risk.engine_heartbeat, bo dozorca nie
jest silnikiem handlowym i nie ma go w enumie ref.engine.';

COMMIT;
