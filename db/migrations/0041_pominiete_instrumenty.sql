-- 0041: pominięty instrument ZOSTAWIA WIERSZ
--
-- POWÓD
--
-- Pętla decyzyjna ma dwie ścieżki, którymi instrument wypada z przebiegu BEZ
-- WIERSZA I BEZ LICZNIKA (services/engine/loop.py, pierwszy przebieg):
--
--     len(features) < 4    -- za mało cech, żeby cokolwiek policzyć
--     price is None        -- ani świecy, ani ceny minutowej
--
-- Obie kończą się `continue`. Po takim przebiegu w bazie nie ma NICZEGO: ani
-- decyzji, ani śladu, że decyzji miało nie być. Instrument pominięty jest
-- nieodróżnialny od instrumentu, który rozważono i odrzucono — a to są dwie
-- zupełnie różne rzeczy. Pierwsza znaczy „system nie wiedział"; druga znaczy
-- „system wiedział i powiedział nie".
--
-- Na dosłownie zadane pytanie „czemu 5 września o 12:00 nie weszliśmy
-- w SOLUSDC" z bazy odpowiedzieć się NIE DAŁO.
--
-- CZEMU NIE `journal.decision` Z AKCJĄ „POMINIĘTY"
--
-- Trzy powody, każdy wystarczający:
--
--   * `journal.action` to enum, a jego wartości są kontraktem trenera.
--     `train.py` czyta dziennik jako panel treningowy; nowa wartość enuma
--     trafiłaby do zbioru uczącego jako obserwacja, której nikt nie liczył.
--   * `journal.decision` wymaga `feature_snapshot`, `confidence`
--     i `risk_verdict` NOT NULL. Przy pominięciu żadnej z tych rzeczy NIE MA —
--     musielibyśmy je zmyślić, czyli wpisać do dziennika liczby, których
--     system nigdy nie policzył.
--   * `decided_for_bar()` pyta wprost `journal.decision` o to, czy świeca jest
--     rozstrzygnięta. Wiersz „pominięty" udawałby rozstrzygnięcie i zamykał
--     świecę na resztę okna — pominięcie z braku danych stałoby się trwałe,
--     nawet gdyby dane dojechały minutę później.
--
-- Osobna tabela nie ma żadnej z tych własności.
--
-- CZEMU NIE SAM LICZNIK W `risk.engine_heartbeat.detail`
--
-- Licznik tam JEST (patrz `stats` w run_once) i zostaje — odpowiada na pytanie
-- „co się dzieje TERAZ". Nie odpowiada na pytanie „co się stało wtedy":
-- `risk.engine_heartbeat` ma `engine ref.engine PRIMARY KEY` i jest zapisywana
-- przez `ON CONFLICT (engine) DO UPDATE`, więc każdy przebieg — co 300 s —
-- kasuje poprzedni. Historii tam nie ma i nie da się jej tam zrobić inaczej niż
-- zmieniając klucz główny tabeli, po której filtrują czujki żywotności.
--
-- KLUCZ GŁÓWNY ZAWIERA `powod`, A NIE SAM (instrument, świeca)
--
-- Pętla budzi się co 300 s, a świeca trwa 4 h, więc ten sam instrument bywa
-- pomijany kilkadziesiąt razy w obrębie jednej świecy. Bez klucza tabela rosłaby
-- o 48 wierszy na instrument na świecę i po tygodniu nikt by w nią nie zajrzał.
-- Z kluczem zostaje JEDEN wiersz na (instrument, świeca, powód) — a `powod`
-- jest w kluczu, bo instrument, któremu w jednym cyklu brakuje cech, a w drugim
-- ceny, ma dwie różne awarie i obie mają być widoczne.
--
-- Wiersz zapisuje PIERWSZE wystąpienie (ON CONFLICT DO NOTHING), więc
-- `pominiety_at` mówi, kiedy problem się zaczął, a nie kiedy ostatnio trwał.
-- To jest ta użyteczniejsza z dwóch liczb: koniec widać po tym, że dla
-- następnej świecy wiersza już nie ma.

BEGIN;

CREATE TABLE IF NOT EXISTS journal.pominiecie (
    engine        ref.engine     NOT NULL,
    mode          ref.trade_mode NOT NULL,
    instrument_id integer        NOT NULL REFERENCES ref.instrument(id),

    -- ŚWIECA, NIE CZAS ZEGAROWY. Pytanie brzmi „czemu dla świecy 12:00 nie było
    -- decyzji", a nie „co się działo o 12:07". Ta sama wielkość, po której
    -- `decided_for_bar()` rozstrzyga, czy decyzja dla świecy zapadła — dzięki
    -- czemu jedno złączenie zestawia „miała zapaść" z „zapadła".
    bar_start     timestamptz    NOT NULL,

    -- Wartości odpowiadają dokładnie miejscom `continue` w pierwszym przebiegu
    -- run_once. CHECK, a nie sam komentarz: powód spoza tej listy znaczy, że
    -- w pętli pojawiła się TRZECIA cicha ścieżka, o której ta migracja nie wie —
    -- i lepiej, żeby wywróciła zapis, niż żeby wpadła do tabeli pod nową nazwą,
    -- której nikt nie szuka.
    powod         text           NOT NULL
                  CHECK (powod IN ('za_malo_cech', 'brak_ceny')),

    -- Czytelny dla człowieka opis TEGO wystąpienia: ile cech się znalazło i
    -- których, albo jakiej ceny zabrakło. Sam `powod` mówi, która gałąź zadziałała;
    -- to mówi, czego konkretnie brakowało — i to jest różnica między „worker cech
    -- stoi" a „ta jedna cecha się nie liczy".
    szczegol      text,

    pominiety_at  timestamptz    NOT NULL DEFAULT now(),

    PRIMARY KEY (engine, mode, instrument_id, bar_start, powod)
);

-- Pytanie zadaje się zawsze po czasie i zawsze wstecz: „co pominięto ostatniej
-- doby". Klucz główny zaczyna się od `engine`, więc sam po `bar_start` nie
-- prowadzi.
CREATE INDEX IF NOT EXISTS pominiecie_bar_idx
    ON journal.pominiecie (bar_start DESC);

COMMENT ON TABLE journal.pominiecie IS
'Instrumenty, które WYPADŁY z przebiegu przed oceną — brak cech albo brak ceny.
Odpowiada na pytanie „czemu dla tej świecy nie ma decyzji", którego samo
journal.decision nie potrafi odróżnić od „nie było przebiegu".';

COMMENT ON COLUMN journal.pominiecie.powod IS
'za_malo_cech = feat.as_of() oddało mniej niż 4 cechy; brak_ceny = nie było ani
domkniętej świecy, ani ceny minutowej.';

-- Ta sama niemutowalność, co reszta schematu `journal` (0005). Ślad pominięcia,
-- który da się po cichu poprawić, jest wart tyle, co jego brak.
CREATE TRIGGER pominiecie_immutable
    BEFORE UPDATE OR DELETE ON journal.pominiecie
    FOR EACH ROW EXECUTE FUNCTION journal.forbid_mutation();

-- UPRAWNIENIA: bez nowych nadań, i to jest świadome. 0034 ustawiło dla schematu
-- `journal` domyślne nadanie SELECT, INSERT, UPDATE dla `trader_silnik`
-- i SELECT dla `trader_czujka`, a 0013 to samo dla `trader_app`. Silnik pisze,
-- czujka i dashboard czytają — dokładnie ten rozkład, który jest potrzebny.
-- UPDATE i DELETE odcina wyzwalacz wyżej, więc szersze nadanie nic nie daje.

COMMIT;
