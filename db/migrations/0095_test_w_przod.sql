-- 0095: wyniki testu w przod H-4 „czy trening sie uczy" (2026-10-07)
--
--   learn.w_przod_okno - jedna para (v_k, v_k-1) na jednym zamknietym tygodniu
--                        (`services/engine/w_przod.py`, docs/hipotezy.md H-4)
--
-- DLACZEGO TABELA, a nie log Joba. Werdykt H-4 bierze PIERWSZE 12 okien
-- z roznica != 0 i ani jednego wiecej. Okno policzone raz musi wiec zostac
-- policzone NA ZAWSZE tak samo: przeliczanie co tydzien od nowa (po rewizji
-- swiec, po wdrozeniu) zmienialoby wyniki, ktore juz weszly do serii - po cichu.
-- `w_przod.py` liczy wylacznie okna, ktorych tu jeszcze nie ma.
--
-- `definicja_miary` W KLUCZU, `git_sha` OBOK. Sha zmienia sie z kazdym
-- wdrozeniem, takze takim, ktore miary nie dotyka - jako filtr serii rwalby ja
-- co tydzien. Etykieta definicji zmienia sie recznie razem z kodem miary;
-- nowa etykieta = nowa seria = nowa hipoteza. Sha mowi, z czego policzono okno.
--
-- `szczegoly` (jsonb): zwrot, ekspozycja, rundy, wypelnienia obu wersji
-- i zalozen poczatkowych, koszyk B1, doplata, uniwersum - zeby dalo sie
-- sprawdzic kazda roznice bez ponownego liczenia.
--
-- Wylacznie ADDYTYWNA: nowa tabela, nic istniejacego sie nie zmienia.
-- Nadania: domyslne uprawnienia schematu `learn` (0034) daja silnikowi
-- SELECT/INSERT/UPDATE na nowych tabelach; GRANT nizej jest jawny, zeby nie
-- trzeba bylo tego wiedziec. Trener (rola silnika) pisze, API nic.

BEGIN;

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS learn.w_przod_okno (
    id                   bigserial PRIMARY KEY,
    engine               ref.engine       NOT NULL,
    bar                  text             NOT NULL,
    okno_od              timestamptz      NOT NULL,
    okno_do              timestamptz      NOT NULL,
    wersja_nowa_id       integer          NOT NULL REFERENCES learn.model_version(id),
    wersja_poprzednia_id integer          NOT NULL REFERENCES learn.model_version(id),
    miara_nowa           double precision NOT NULL,
    miara_poprzednia     double precision NOT NULL,
    roznica              double precision NOT NULL,
    szczegoly            jsonb            NOT NULL,
    definicja_miary      text             NOT NULL,
    git_sha              text,
    policzono            timestamptz      NOT NULL DEFAULT now(),
    UNIQUE (engine, bar, okno_od, definicja_miary)
);

COMMENT ON TABLE learn.w_przod_okno IS
    'H-4: jedna para wersji modelu na jednym zamknietym tygodniu. Append-only; '
    'okno policzone raz nie jest liczone ponownie (services/engine/w_przod.py).';
COMMENT ON COLUMN learn.w_przod_okno.roznica IS
    'miara_nowa - miara_poprzednia; 0 = remis (nie jest proba testu znakow).';
COMMENT ON COLUMN learn.w_przod_okno.definicja_miary IS
    'w_przod.DEFINICJA_MIARY; inna etykieta = inna seria (nowa hipoteza).';

GRANT SELECT, INSERT ON learn.w_przod_okno TO trader_silnik;
GRANT USAGE, SELECT ON SEQUENCE learn.w_przod_okno_id_seq TO trader_silnik;

COMMIT;
