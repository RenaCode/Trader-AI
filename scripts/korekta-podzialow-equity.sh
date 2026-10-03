#!/usr/bin/env bash
# Korekta swiec equity o podzialy akcji - procedura operatora, kroki 1-4.
#
# PO CO SKRYPT, SKORO KROKI STOJA W docs/08
#
# Bo cztery polecenia wpisywane recznie maja jedna kolejnosc, ktorej nie wolno
# pomylic, i jeden warunek wstepny, ktory latwo przeczytac zbyt pobieznie:
# pomiar `--sucho` z `adjustment=split` i z `adjustment=raw` MUSI dac ROZNE
# liczby. Rowne znacza, ze parametr nie dochodzi do dostawcy - wtedy
# `--przepisz` zapisalby te same nieskorygowane swiece, a `--znacznik` zdjalby
# bramke z danych, ktore nadal sa zatrute (services/ingest/korekta_podzialow.py,
# naglowek). Ten skrypt sprawdza to SAM i przerywa, zanim cokolwiek zapisze.
#
# DOMYSLNIE TYLKO MIERZY. Zapis (kroki 2-4) wymaga `--wykonaj`. Pierwsze
# uruchomienie ma pokazac pomiar czlowiekowi, a nie od razu przepisac 2,5 roku
# historii.
#
# Uzycie (z maszyny z dostepem do klastra, np. na VPS):
#   scripts/korekta-podzialow-equity.sh              # krok 1: pomiar + warunki
#   scripts/korekta-podzialow-equity.sh --wykonaj    # kroki 1-4
#
# Zmienne: KUBECTL (domyslnie kubectl), NS (default), CEL (deploy/trader-api).
# Polecenia ida przez `kubectl exec` do poda API, bo tam jest SECRETS_KEY
# (klucz Alpaki jest w bazie zaszyfrowany).
#
# DLACZEGO NIE ZATRZYMUJEMY `features-equity` NA CZAS KROKOW 2-4
#
# Raport audytu zaleca skalowanie do 0 jako "najlepiej", nie jako warunek.
# Pod tym chartem chodzi ArgoCD, ktore przywroci `replicas: 1` z repozytorium,
# wiec `kubectl scale` byloby obietnica bez pokrycia. I nie jest potrzebne:
# `features-equity` liczy z TYCH SAMYCH swiec (po kroku 2 juz skorygowanych),
# a jego okno (7 dni) lezy w calosci wewnatrz okna kroku 3. Krok 4 i tak
# porownuje KAZDA zapisana wartosc cechy ze swiecami - gdyby wyscig cokolwiek
# rozjechal, znacznik zostanie odmowiony, a wtedy wystarczy powtorzyc 3 i 4.
#
# NIE JEST URUCHAMIANY AUTOMATYCZNIE. Zaden szablon chartu go nie wola.
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NS="${NS:-default}"
CEL="${CEL:-deploy/trader-api}"
WYKONAJ=0
case "${1:-}" in
  --wykonaj) WYKONAJ=1 ;;
  "") ;;
  *) echo "uzycie: $0 [--wykonaj]" >&2; exit 2 ;;
esac

# Instrumenty, ktore MIALY podzial 10:1 w oknie historii (docs/08). Czynnik
# korekty musi u nich wyjsc okolo 0,1 - inny znaczy, ze dostawca skorygowal
# o cos innego niz podzial, albo ze pomiar porownuje nie te szeregi.
PODZIELONE="NVDA AVGO NFLX"

KATALOG="$(mktemp -d "${TMPDIR:-/tmp}/korekta-podzialow.XXXXXX")"
echo "==> logi krokow: $KATALOG"

uruchom() {
  "$KUBECTL" -n "$NS" exec "$CEL" -c api -- "$@"
}

razem() {
  # "RAZEM zmieni wartosc: N wierszy ..." -> N
  sed -n 's/^RAZEM zmieni wartosc: \([0-9][0-9]*\) wierszy.*/\1/p' "$1"
}

echo "==> krok 1a: pomiar, adjustment=split (nic nie zapisuje)"
uruchom python services/ingest/korekta_podzialow.py --sucho > "$KATALOG/1-split.txt"
cat "$KATALOG/1-split.txt"
echo "==> krok 1b: pomiar, adjustment=raw (punkt odniesienia)"
uruchom python services/ingest/korekta_podzialow.py --sucho --korekta raw > "$KATALOG/1-raw.txt"
cat "$KATALOG/1-raw.txt"

SPLIT="$(razem "$KATALOG/1-split.txt")"
RAW="$(razem "$KATALOG/1-raw.txt")"
if [ -z "$SPLIT" ] || [ -z "$RAW" ]; then
  echo "!! nie umiem odczytac linii RAZEM z pomiaru - przerywam" >&2
  exit 1
fi
if [ "$SPLIT" = "$RAW" ]; then
  echo "!! split i raw daja TE SAMA liczbe zmienionych wierszy ($SPLIT)." >&2
  echo "!! Parametr korekty nie dochodzi do dostawcy - przepisanie nic by nie" >&2
  echo "!! skorygowalo, a znacznik zdjalby bramke z zatrutych danych." >&2
  exit 1
fi
if grep -q "NIEPELNE" "$KATALOG/1-split.txt"; then
  echo "!! dostawca nie oddal czesci swiec, ktore mamy (NIEPELNE wyzej)." >&2
  echo "!! --przepisz odmowi tym instrumentom; najpierw wyjasnic dziury w feedzie." >&2
  exit 1
fi
for S in $PODZIELONE; do
  LINIA="$(grep -E "^  $S[[:space:]]" "$KATALOG/1-split.txt" || true)"
  if [ -z "$LINIA" ]; then
    echo "!! $S nie ma w pomiarze - nie ma czym sprawdzic korekty" >&2
    exit 1
  fi
  CZ="$(printf '%s\n' "$LINIA" | sed -n 's/.*x\([0-9.]*\)$/\1/p')"
  if [ -z "$CZ" ] || ! awk -v c="$CZ" 'BEGIN { exit !(c > 0.05 && c < 0.2) }'; then
    echo "!! $S: czynnik korekty '${CZ:-brak}' zamiast okolo 0,1 (podzial 10:1)" >&2
    exit 1
  fi
done
echo "==> warunki kroku 1 spelnione: split $SPLIT != raw $RAW, pelne pokrycie," \
     "czynnik ~0,1 dla: $PODZIELONE"

if [ "$WYKONAJ" != 1 ]; then
  echo "==> tryb pomiaru - nic nie zapisano. Kroki 2-4: $0 --wykonaj"
  exit 0
fi

echo "==> krok 2: przepisanie swiec (jedna transakcja na instrument, bez znacznika)"
uruchom python services/ingest/korekta_podzialow.py --przepisz | tee "$KATALOG/2-przepisz.txt"

echo "==> krok 3: przeliczenie cech 1h equity na calym oknie"
uruchom python services/ingest/features.py --bar 1h --engine equity --okno-dni 1400 --once \
  | tee "$KATALOG/3-cechy.txt"

echo "==> krok 4: znacznik (sprawdza cechy vs swiece, rozjazd = odmowa)"
uruchom python services/ingest/korekta_podzialow.py --znacznik | tee "$KATALOG/4-znacznik.txt"

echo "==> gotowe. Krok 5: trening toru equity - nocny trainer-cron albo recznie:"
echo "    $KUBECTL -n $NS create job --from=cronjob/trader-trainer trener-po-korekcie"
