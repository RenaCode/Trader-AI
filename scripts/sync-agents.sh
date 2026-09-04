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

mkdir -p "$CEL"
cp "$ZRODLO"/*.md "$CEL"/
echo "Skopiowano $(ls -1 "$ZRODLO"/*.md | wc -l | tr -d ' ') definicji do $CEL"
ls -1 "$CEL"/*.md | sed 's#.*/#  #'
