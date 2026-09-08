-- 0033: status instrumentu u giełdy przestaje być notatką, a staje się bramką
--
-- CO BYŁO
--
-- `services/ingest/instrumenty.py` pobierał z giełdy pole `status`, zapisywał je
-- do `meta->>'status_gieldy'` — i NIGDY GO NIE CZYTAŁ. Nie było kolumny, po
-- której dałoby się filtrować, nie było bramki przy składaniu zlecenia, a
-- instrument, który zniknął z odpowiedzi `instruments-info`, zostawiał tylko
-- linijkę „BRAK U GIEŁDY" w logu zadania, którego nikt nie ogląda.
--
-- Zapis bez odczytu jest GORSZY niż brak zapisu, bo wygląda na obsłużony.
-- Kolumna, której nikt nie czyta, mówi „sprawdzone i w porządku" o czymś, czego
-- nie sprawdzono.
--
-- CO SIĘ DZIEJE PRZY DELISTINGU
--
-- Giełda anuluje otwarte zlecenia, zatrzymuje handel, a po zamknięciu okna
-- wypłat konwertuje niewycofane tokeny po cenie z chwili, na którą nie mamy
-- wpływu. Proces liczy się w miesiącach. Uniwersum ma dziesięć par, więc jedna
-- zdelistowana pozycja to do ~10% ekspozycji zamrożonej na kwartał i wycenionej
-- cudzą ręką. Dla każdego limitu ryzyka wygląda to jak normalna otwarta pozycja:
-- instrument ma `tradeable = true`, świece przestają przychodzić, a silnik
-- dalej liczy z niej ekspozycję i blokuje nią kolejne wejścia.
--
-- DLACZEGO KOLUMNY, A NIE DALEJ `meta`
--
-- Po statusie trzeba UMIEĆ FILTROWAĆ — w czujce dozoru i w każdym zapytaniu
-- o ekspozycję. Klucz w jsonb da się odczytać, ale nie da się o nim zapomnieć
-- w sposób głośny: brak klucza i wartość „Trading" wyglądają w SQL prawie tak
-- samo. Kolumna z komentarzem to jedno miejsce, w którym pisze, co znaczy NULL.
--
-- TRZY STANY, KTÓRYCH NIE WOLNO ZLEWAĆ W JEDEN
--
--   status_sprawdzony_at IS NULL          -- NIKT NIGDY NIE PYTAŁ giełdy
--   status_sprawdzony_at + status NULL    -- pytaliśmy, giełda go NIE ZNA
--   status_sprawdzony_at + status tekst   -- pytaliśmy, to jest jej odpowiedź
--
-- Pierwszy i trzeci przypadek wyglądają identycznie, jeśli patrzeć wyłącznie na
-- `status_gieldy`. Różnica jest zasadnicza: „nie pytaliśmy" to niewiedza, a nie
-- zgodność, i czujka ma się z tego powodu odzywać.

ALTER TABLE ref.instrument
    ADD COLUMN status_gieldy          text,
    ADD COLUMN status_gieldy_od       timestamptz,
    ADD COLUMN status_sprawdzony_at   timestamptz,
    -- Powód, dla którego to MY zdjęliśmy `tradeable`. Bez tego nie da się
    -- odróżnić instrumentu wyłączonego świadomą decyzją (migracja 0020 wyłączyła
    -- czternaście par USDT, których podmiot unijny nie oferuje) od wyłączonego
    -- automatem — a wtedy nie da się BEZPIECZNIE nic przywrócić, bo przywracanie
    -- po ciemku cofałoby cudze decyzje.
    ADD COLUMN wylaczony_przez_status text
        CHECK (wylaczony_przez_status IN ('zawieszenie', 'wycofanie'));

COMMENT ON COLUMN ref.instrument.status_gieldy IS
    'Ostatni status z /v5/market/instruments-info (np. Trading, PreLaunch, '
    'Delivering, Closed). NULL przy niepustym status_sprawdzony_at znaczy, że '
    'giełda tego instrumentu NIE ZNA — to nie to samo co brak sprawdzenia.';
COMMENT ON COLUMN ref.instrument.status_gieldy_od IS
    'Kiedy status ostatnio się ZMIENIŁ. Odróżnia dwudziestominutową przerwę '
    'techniczną od wycofania ciągnącego się miesiącami — bez tej daty obie '
    'wyglądają tak samo.';
COMMENT ON COLUMN ref.instrument.status_sprawdzony_at IS
    'Kiedy status był ostatnio POTWIERDZONY u giełdy. Stary odczyt nie jest '
    'potwierdzeniem: gdy zadanie przestanie chodzić, kolumna statusu zamarza '
    'na wartości „Trading" i cicho zapewnia, że wszystko gra.';
COMMENT ON COLUMN ref.instrument.wylaczony_przez_status IS
    'Wypełnione, gdy tradeable zdjął automat: „zawieszenie" wraca samo, gdy '
    'giełda znów poda Trading; „wycofanie" wymaga decyzji człowieka.';

-- Przeniesienie tego, co już zebrano, i USUNIĘCIE kopii z `meta`.
--
-- Dwa miejsca z tą samą prawdą rozjeżdżają się przy pierwszej zmianie i wtedy
-- pytanie „jaki jest status" przestaje mieć jedną odpowiedź. Data sprawdzenia
-- zostaje pusta CELOWO: nie wiemy, kiedy ten wpis powstał, a wpisanie tu now()
-- byłoby fałszywym potwierdzeniem świeżości.
UPDATE ref.instrument
   SET status_gieldy = meta->>'status_gieldy',
       meta = meta - 'status_gieldy'
 WHERE meta ? 'status_gieldy';

-- ---------------------------------------------------------------- powiadomienia
--
-- DWA TYPY, NIE JEDEN. „Zawieszony na czas przerwy technicznej" i „wycofywany
-- z giełdy" to zdarzenia o różnym koszcie: pierwsze mija samo w kilkadziesiąt
-- minut, drugie kończy się przymusową konwersją po cudzej cenie za kilka
-- miesięcy. Zlanie ich w jeden alarm ustawia próg uwagi na tym częstszym —
-- czyli uczy przewijać ten, który jedyny wymaga działania.
INSERT INTO notify.event_type
    (code, label, description, category, default_severity, always_on, default_cooldown_s)
VALUES
    ('instrument.delisted', 'Instrument wycofywany z giełdy',
     'Giełda przestaje prowadzić ten instrument albo już go nie zna. Otwarte '
     'zlecenia są przy delistingu anulowane, a niewycofane środki po zamknięciu '
     'wypłat konwertowane po ówczesnej cenie rynkowej — proces liczony '
     'w miesiącach. Instrument został zdjęty z uniwersum decyzyjnego; OTWARTA '
     'POZYCJA NIE JEST ZAMYKANA AUTOMATYCZNIE, bo to decyzja handlowa.',
     'ryzyko', 'critical', true, 86400),
    ('instrument.suspended', 'Handel instrumentem wstrzymany',
     'Giełda chwilowo nie prowadzi handlu tym instrumentem (przerwa techniczna, '
     'okres przedstartowy). Instrument jest wstrzymany w uniwersum decyzyjnym '
     'i wróci sam, gdy giełda znów poda status Trading.',
     'ryzyko', 'warning', false, 3600)
ON CONFLICT (code) DO NOTHING;
