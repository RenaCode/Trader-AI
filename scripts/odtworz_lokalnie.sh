#!/usr/bin/env bash
# Odtworzenie ostatniej kopii produkcji w lokalnym kontenerze (audyt Fable T12).
#
#   scripts/odtworz_lokalnie.sh                 # pobierz ostatni dump z VPS i odtworz
#   KATALOG=~/kopie BAZA=trader_prod_kopia NADPISZ=1 scripts/odtworz_lokalnie.sh
#   BEZ_POBIERANIA=1 KATALOG=<katalog z plikami> scripts/odtworz_lokalnie.sh
#
# PO CO DWA RAZY. Ten sam przebieg jest (1) proba odtworzenia, ktorej nikt nie
# wykonal od uruchomienia kopii (F-8.2: `test_kopia_odtwarzalna.py` czyta YAML,
# nie odtwarza), i (2) harnessem badawczym z T3: lokalna baza z pelna historia,
# z ktorej `scripts/eksport_paneli.py` robi plik panelu - bez ani jednego
# polaczenia z baza produkcyjna.
#
# KOLEJNOSC JEST TA Z `*.odtwarzanie.txt` (plik lezy obok dumpu i jest
# wypisywany na poczatku): wersje, role, baza z rozszerzeniem w zapisanej
# wersji, pre_restore / pg_restore / post_restore, kontrola. Krok 4 tamtego
# pliku (hasla rol z `trader-secret`) jest tu RECZNY - patrz koniec skryptu.
#
# Z PRODUKCJA ROZMAWIA TYLKO PRZEZ `ssh renacode` I TYLKO CZYTA: `ls`, `cat`,
# `sha256sum` w /opt/trader-backups. Opcjonalne porownanie liczby decyzji
# z produkcja (POROWNAJ_Z_PROD=1) to jeden SELECT - domyslnie wylaczone.
#
# Zmienne (wszystkie opcjonalne):
#   KONTENER   lokalny kontener z PostgreSQL+TimescaleDB   [trader-db]
#   PGUSER     rola administracyjna w kontenerze           [trader]
#   BAZA       baza docelowa                               [trader_prod_kopia]
#   KATALOG    gdzie trzymac pliki kopii                   [./kopie]
#   NADPISZ    1 = usun istniejaca BAZA przed odtworzeniem [0]
#   BEZ_POBIERANIA 1 = uzyj plikow z KATALOG, bez ssh       [0]
#   ZNACZNIK   konkretna kopia (np. 20261003T043403Z) zamiast ostatniej
#   WYMUSZ_WERSJE 1 = odtworz mimo innej wersji TimescaleDB/PostgreSQL [0]
#   MIGRUJ     1 = po odtworzeniu zastosuj migracje z repo (db/migrate.py) [0]
#   POROWNAJ_Z_PROD 1 = policz decyzje na produkcji (SELECT przez ssh) [0]
#   PROD_NS / PROD_DB_DEPLOY  namespace i Deployment bazy na k3s [trader / trader-db]
#   PY         interpreter z psycopg (db/migrate.py)       [.venv/bin/python repo]
set -euo pipefail

KONTENER=${KONTENER:-trader-db}
PGUSER=${PGUSER:-trader}
BAZA=${BAZA:-trader_prod_kopia}
KATALOG=${KATALOG:-./kopie}
NADPISZ=${NADPISZ:-0}
BEZ_POBIERANIA=${BEZ_POBIERANIA:-0}
ZNACZNIK=${ZNACZNIK:-}
WYMUSZ_WERSJE=${WYMUSZ_WERSJE:-0}
MIGRUJ=${MIGRUJ:-0}
POROWNAJ_Z_PROD=${POROWNAJ_Z_PROD:-0}
PROD_NS=${PROD_NS:-trader}
PROD_DB_DEPLOY=${PROD_DB_DEPLOY:-trader-db}
ZDALNY=/opt/trader-backups
REPO=$(cd "$(dirname "$0")/.." && pwd)

krok() { printf '\n== %s\n' "$*"; }
blad() { printf 'BŁĄD: %s\n' "$*" >&2; exit 1; }
sql() { docker exec -i "$KONTENER" psql -U "$PGUSER" -v ON_ERROR_STOP=1 -Atq "$@"; }
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }

docker inspect -f '{{.State.Running}}' "$KONTENER" 2>/dev/null | grep -qx true \
  || blad "kontener $KONTENER nie działa (docker compose up -d db?)"
mkdir -p "$KATALOG"

# --------------------------------------------------------------- 1. pliki
krok "1. Pliki kopii"
if [ "$BEZ_POBIERANIA" = 1 ]; then
  if [ -z "$ZNACZNIK" ]; then
    ZNACZNIK=$(ls -t "$KATALOG"/trader-*.dump 2>/dev/null | head -1 | sed 's/.*trader-\(.*\)\.dump/\1/')
  fi
  [ -n "$ZNACZNIK" ] || blad "brak trader-*.dump w $KATALOG"
else
  if [ -z "$ZNACZNIK" ]; then
    ZNACZNIK=$(ssh renacode "sudo ls -t $ZDALNY" | grep -m1 '^trader-.*\.dump$' \
               | sed 's/^trader-\(.*\)\.dump$/\1/') || true
  fi
  [ -n "$ZNACZNIK" ] || blad "nie znalazłem żadnego trader-*.dump w $ZDALNY"
fi
STEM="trader-$ZNACZNIK"
echo "kopia: $STEM"

if [ "$BEZ_POBIERANIA" != 1 ]; then
  ZDALNY_SHA=$(ssh renacode "sudo sha256sum $ZDALNY/$STEM.dump" | cut -d' ' -f1)
  for rozsz in odtwarzanie.txt role.sql wersje; do
    ssh renacode "sudo cat $ZDALNY/$STEM.$rozsz" > "$KATALOG/$STEM.$rozsz"
  done
  if [ -f "$KATALOG/$STEM.dump" ] && [ "$(sha "$KATALOG/$STEM.dump")" = "$ZDALNY_SHA" ]; then
    echo "dump już jest lokalnie i zgadza się co do sumy - bez ponownego pobierania"
  else
    echo "pobieram $STEM.dump ..."
    ssh renacode "sudo cat $ZDALNY/$STEM.dump" > "$KATALOG/$STEM.dump.part"
    mv "$KATALOG/$STEM.dump.part" "$KATALOG/$STEM.dump"
  fi
  [ "$(sha "$KATALOG/$STEM.dump")" = "$ZDALNY_SHA" ] \
    || blad "suma SHA-256 dumpu różni się od tej na VPS - plik uszkodzony w drodze"
  echo "$ZDALNY_SHA  $STEM.dump" > "$KATALOG/$STEM.dump.sha256"
fi
for rozsz in dump odtwarzanie.txt role.sql wersje; do
  [ -s "$KATALOG/$STEM.$rozsz" ] || blad "brak albo pusty $KATALOG/$STEM.$rozsz - komplet idzie RAZEM"
done
echo "--- $STEM.odtwarzanie.txt (procedura, według której idą kroki niżej) ---"
cat "$KATALOG/$STEM.odtwarzanie.txt"
echo "---"

# --------------------------------------------------------------- 0. wersje
krok "0. Wersje klastra (krok 0 procedury)"
TS_WERSJA=$(sed -n 's/^timescaledb=//p' "$KATALOG/$STEM.wersje")
PG_WERSJA=$(sed -n 's/^postgresql=\([0-9.]*\).*/\1/p' "$KATALOG/$STEM.wersje")
[ -n "$TS_WERSJA" ] && [ -n "$PG_WERSJA" ] || blad "nie odczytałem wersji z $STEM.wersje"
PG_LOKALNY=$(sql -d postgres -c "SHOW server_version" | sed 's/^\([0-9.]*\).*/\1/')
TS_DOSTEPNA=$(sql -d postgres -c "SELECT count(*) FROM pg_available_extension_versions
                                  WHERE name = 'timescaledb' AND version = '$TS_WERSJA'")
echo "kopia: PostgreSQL $PG_WERSJA, TimescaleDB $TS_WERSJA; kontener: PostgreSQL $PG_LOKALNY," \
     "TimescaleDB $TS_WERSJA $([ "$TS_DOSTEPNA" = 1 ] && echo dostępna || echo NIEDOSTĘPNA)"
if [ "$PG_LOKALNY" != "$PG_WERSJA" ] || [ "$TS_DOSTEPNA" != 1 ]; then
  [ "$WYMUSZ_WERSJE" = 1 ] || blad "wersje się nie zgadzają - obraz wg $STEM.wersje albo WYMUSZ_WERSJE=1"
  echo "UWAGA: wersje różne, WYMUSZ_WERSJE=1 - dalej na własne ryzyko"
fi

# --------------------------------------------------------------- 1. role
krok "1. Role przed danymi"
# Bez ON_ERROR_STOP: w lokalnym klastrze czesc rol juz istnieje (rola
# administracyjna zawsze, role procesow po migracjach dev). „already exists"
# jest tu oczekiwane; wszystko inne jest wypisane.
docker exec -i "$KONTENER" psql -U "$PGUSER" -d postgres -q < "$KATALOG/$STEM.role.sql" 2>&1 \
  | grep -v 'already exists' || true

# --------------------------------------------------------------- 2. baza
krok "2. Baza $BAZA i rozszerzenie w wersji $TS_WERSJA"
ISTNIEJE=$(sql -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$BAZA'")
if [ "$ISTNIEJE" = 1 ]; then
  [ "$NADPISZ" = 1 ] || blad "baza $BAZA już istnieje - NADPISZ=1 albo inna BAZA"
  [ "$BAZA" != trader ] || blad "nie nadpisuję bazy deweloperskiej 'trader' - podaj inną BAZA"
  docker exec "$KONTENER" dropdb -U "$PGUSER" "$BAZA"
fi
# Z template0: obraz timescaledb-ha ma rozszerzenie w template1, a wtedy
# CREATE EXTENSION ... VERSION nizej nie mialby czego zalozyc.
docker exec "$KONTENER" createdb -U "$PGUSER" -O "$PGUSER" -T template0 "$BAZA"
sql -d "$BAZA" -c "CREATE EXTENSION timescaledb VERSION '$TS_WERSJA'"

# --------------------------------------------------------------- 3. dane
krok "3. Dane (pre_restore / pg_restore / post_restore)"
LOG="$KATALOG/$STEM.restore.log"
sql -d "$BAZA" -c "SELECT timescaledb_pre_restore()" > /dev/null
set +e
docker exec -i "$KONTENER" pg_restore -U "$PGUSER" -d "$BAZA" --no-owner < "$KATALOG/$STEM.dump" > "$LOG" 2>&1
RC=$?
set -e
sql -d "$BAZA" -c "SELECT timescaledb_post_restore()" > /dev/null
# pg_restore konczy sie kodem != 0 takze przy samych ostrzezeniach. Ostrzezenie
# o cyklicznym kluczu `continuous_agg` jest w procedurze opisane jako
# oczekiwane; reszta bledow idzie na ekran.
BLEDY=$(grep -c '^pg_restore: error' "$LOG" || true)
echo "pg_restore: kod $RC, błędów w logu: $BLEDY (pełny log: $LOG)"
grep '^pg_restore: error' "$LOG" | head -20 || true

# --------------------------------------------------------------- 5. kontrola
krok "5. Kontrola (krok 5 procedury)"
DECYZJE=$(sql -d "$BAZA" -c "SELECT count(*) FROM journal.decision")
OSTATNIA=$(sql -d "$BAZA" -c "SELECT max(decided_at) FROM journal.decision")
echo "journal.decision: $DECYZJE wierszy, ostatnia $OSTATNIA"
[ "$DECYZJE" -gt 0 ] || blad "pusty journal.decision - to schemat bez danych, nie odtworzona baza"
sql -d "$BAZA" -c "SELECT 'rozszerzenie timescaledb ' || extversion FROM pg_extension WHERE extname = 'timescaledb'"
echo "role: $(sql -d "$BAZA" -c "SELECT string_agg(rolname, ' ' ORDER BY rolname) FROM pg_roles WHERE rolname LIKE 'trader%'")"
echo "ostatnia migracja w kopii: $(sql -d "$BAZA" -c "SELECT max(version) FROM schema_migrations")"

PORT=$(docker port "$KONTENER" 5432/tcp | head -1 | sed 's/.*://')
HASLO=$(docker exec "$KONTENER" printenv POSTGRES_PASSWORD 2>/dev/null || true)
URL="postgresql://$PGUSER:${HASLO}@localhost:$PORT/$BAZA"
PY=${PY:-$REPO/.venv/bin/python}; [ -x "$PY" ] || PY=python3
OCZEKUJACE=$(mktemp)
DATABASE_URL="$URL" "$PY" "$REPO/db/migrate.py" --oczekujace "$OCZEKUJACE" || true
if [ -s "$OCZEKUJACE" ]; then
  if [ "$MIGRUJ" = 1 ]; then
    # Tylko lokalna kopia. Hasla rol procesow zostaja nieustawione - migrator
    # to wypisze i to jest w porzadku: kopia sluzy do odczytu.
    DATABASE_URL="$URL" "$PY" "$REPO/db/migrate.py"
  else
    echo "UWAGA: kod w repo ma migracje, których kopia nie ma - kod czytający nowe kolumny"
    echo "       (np. eksport paneli) padnie. Lokalnie: MIGRUJ=1 $0 (z NADPISZ=1) albo"
    echo "       DATABASE_URL=$URL $PY db/migrate.py"
  fi
fi
rm -f "$OCZEKUJACE"

if [ "$POROWNAJ_Z_PROD" = 1 ]; then
  # Jeden SELECT na produkcji: decyzje do chwili ostatniej decyzji w kopii -
  # po zrobieniu kopii produkcja zapisuje dalej, wiec samo count(*) by sie
  # nie zgodzilo.
  PROD=$(ssh renacode "sudo k3s kubectl -n $PROD_NS exec deploy/$PROD_DB_DEPLOY -- \
      psql -U trader -d trader -Atc \"SELECT count(*) FROM journal.decision WHERE decided_at <= '$OSTATNIA'\"")
  echo "produkcja: $PROD decyzji do $OSTATNIA; kopia: $DECYZJE"
  [ "$PROD" = "$DECYZJE" ] || blad "liczba decyzji w kopii różni się od produkcji"
fi

# --------------------------------------------------------------- 4. hasla
krok "4. Hasła ról i trader-secret - KROK RĘCZNY, DECYZJA WŁAŚCICIELA"
cat <<EOF
Kopia jest odtworzona do ODCZYTU: rola $PGUSER, $URL
Czego ten skrypt NIE robi i robić nie powinien:
  * hasła ról procesów (trader_app, trader_silnik, ...) - zrzut globalny jest
    bez haseł; ustawia je migrator z sekretu trader-secret (krok 4 procedury);
  * SECRETS_KEY z trader-secret - bez niego totp_secret, api_credential,
    app.secret i notify.channel.config_enc są nieczytelne, więc logowanie
    właściciela do odtworzonego systemu NIE zadziała (audyt U-5).
trader-secret nie ma dziś kopii poza klastrem. Gdzie ją trzymać (age do
renacode-backup?) i czy kopiować klucz na laptopa - decyduje właściciel.
Dopóki tego nie ma, odtworzenie kończy się na danych, nie na działającym systemie.
EOF
