#!/usr/bin/env bash
# Kopiuje definicje agentow tam, gdzie Claude Code ich szuka.
#
# DLACZEGO TO ISTNIEJE
#
# Agenci sa czescia projektu i ich zrodlem jest to repozytorium - wersjonowane,
# przegladane, spojne z kodem, ktory opisuja. Ale Claude Code szuka ich
# w katalogu `.claude/agents/` PROJEKTU SESJI, a sesje uruchamiane sa czesto
# z katalogu nadrzednego (~/workspace), gdzie tych plikow nie ma.
#
# Dowiazanie symboliczne bylo pierwszym pomyslem i zostalo odrzucone: nie
# wiadomo, czy skaner za nim podaza, a "prawdopodobnie dziala" to zla podstawa
# dla czegos, czego awaria objawia sie cisza.
#
# Kopie sa wiec jawne, a ten skrypt jest jedynym sposobem ich powstawania -
# dzieki temu rozjazd jest do wykrycia jednym `diff`, a nie do odkrycia
# przypadkiem.
#
# Synchronizacja jest LUSTREM, nie dosypywaniem: plik usuniety z repozytorium
# znika takze z kopii. Zanim cokolwiek zostanie nadpisane albo usuniete,
# obecna zawartosc katalogu docelowego jest odkladana obok, w
# `<cel>.kopia-<data>` -
# a gdy kopii zrobic sie nie da, przebieg sie przerywa.
#
# Uzycie:
#   scripts/sync-agents.sh              # kopiuje do ~/workspace/.claude/agents
#   scripts/sync-agents.sh --sprawdz    # tylko sprawdza rozjazd, kod 1 gdy jest
#   scripts/sync-agents.sh ~/inny/kat   # inny katalog docelowy
set -euo pipefail

ZRODLO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.claude/agents"
SPRAWDZ=0
CEL="${HOME}/workspace/.claude/agents"

for a in "$@"; do
  case "$a" in
    --sprawdz) SPRAWDZ=1 ;;
    *) CEL="$a" ;;
  esac
done

if [ "$SPRAWDZ" = 1 ]; then
  if diff -q "$ZRODLO" "$CEL" > /dev/null 2>&1; then
    echo "zgodne: $CEL"
  else
    echo "ROZJAZD miedzy $ZRODLO a $CEL:" >&2
    diff -rq "$ZRODLO" "$CEL" >&2 || true
    exit 1
  fi
  exit 0
fi

# SYNCHRONIZACJA MUSI TEZ USUWAC.
#
# Do 2026-09-23 stalo tu samo `cp "$ZRODLO"/*.md "$CEL"/`, czyli sciezka
# kopiujaca wylacznie DODAWALA i NADPISYWALA. Sciezka sprawdzajaca porownuje
# katalogi w OBU kierunkach, wiec plik obecny w celu i nieobecny w zrodle byl
# dla niej rozjazdem - i te dwie sciezki nie mogly sie zgodzic: po usunieciu
# definicji agenta z repozytorium `--sprawdz` zwracal 1, synchronizacja tego
# nie naprawiala, a kolejny `--sprawdz` znowu zwracal 1.
#
# Scenariusz, ktory to czynil grozniejszym niz kosmetyka: definicje agenta
# usuwa sie z repozytorium wlasnie dlatego, ze ma za szerokie uprawnienia
# narzedziowe. Kopia w $CEL zostawala, Claude Code dalej ja widzial i dalej
# dalo sie ja wywolac - usuniecie z repozytorium nie mialo zadnego skutku.
#
# Dlatego ponizej jest LUSTRO: po synchronizacji $CEL zawiera dokladnie to,
# co $ZRODLO, i `--sprawdz` po niej przechodzi z definicji.

# Puste zrodlo przy wlaczonym usuwaniu wyczyscilo by cel. Pusty katalog
# w repozytorium znaczy raczej "cos jest nie tak ze sciezka" niz "skasuj
# wszystkich agentow", wiec to jest blad, a nie polecenie.
ZRODLOWE=$(ls -1 "$ZRODLO" 2>/dev/null || true)
if [ -z "$ZRODLOWE" ]; then
  echo "BLAD: $ZRODLO jest pusty albo nie istnieje - nie synchronizuje." >&2
  exit 1
fi

# Katalogi po ktorejkolwiek stronie rozjechalyby te dwie sciezki z powrotem:
# `diff -rq` schodzi w glab, a lustro nizej dziala na plikach plaskich.
for k in "$ZRODLO" "$CEL"; do
  [ -d "$k" ] || continue
  if find "$k" -mindepth 1 -maxdepth 1 -type d | grep -q .; then
    echo "BLAD: $k zawiera podkatalog, a ten skrypt synchronizuje plaska liste." >&2
    echo "      Bez tego '--sprawdz' meldowalby rozjazd, ktorego nie umie naprawic." >&2
    exit 1
  fi
done

# Co by zniknelo i co by sie zmienilo - liczone PRZED dotknieciem czegokolwiek.
DO_USUNIECIA=""
ZMIENIANE=""
if [ -d "$CEL" ]; then
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    if [ ! -e "$ZRODLO/$n" ]; then
      DO_USUNIECIA="$DO_USUNIECIA$n
"
    elif ! cmp -s "$ZRODLO/$n" "$CEL/$n"; then
      ZMIENIANE="$ZMIENIANE$n
"
    fi
  done <<< "$(ls -1 "$CEL" 2>/dev/null || true)"
fi

# KOPIA PRZED NADPISANIEM, NIE OSTRZEZENIE PO NIM.
#
# `cp` nadpisywal pliki w celu bez pytania i bez kopii, wiec reczna zmiana
# w $CEL ginela nieodwracalnie - mimo ze skrypt ma mechanizm (`--sprawdz`)
# pozwalajacy ja wykryc. Teraz kazdy przebieg, ktory cokolwiek nadpisuje albo
# usuwa, najpierw odklada obecna zawartosc obok; gdy kopii zrobic sie NIE DA,
# przebieg sie przerywa, zamiast tylko o tym napisac.
if [ -n "$DO_USUNIECIA$ZMIENIANE" ]; then
  KOPIA="${CEL%/}.kopia-$(date +%Y%m%d-%H%M%S)"
  if ! mkdir -p "$KOPIA" || ! cp -R "$CEL"/. "$KOPIA"/; then
    echo "BLAD: nie udalo sie odlozyc kopii $CEL do $KOPIA." >&2
    echo "      Nie nadpisuje i nie usuwam niczego." >&2
    exit 1
  fi
  echo "Kopia poprzedniej zawartosci: $KOPIA"
fi

mkdir -p "$CEL"
cp "$ZRODLO"/* "$CEL"/

while IFS= read -r n; do
  [ -n "$n" ] || continue
  rm -f "$CEL/$n"
  echo "  USUNIETY (nie ma go juz w repozytorium): $n"
done <<< "$DO_USUNIECIA"

echo "Zsynchronizowano $(printf '%s\n' "$ZRODLOWE" | grep -c .) definicji do $CEL"
ls -1 "$CEL" | sed 's#^#  #'
