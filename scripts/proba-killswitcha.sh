#!/usr/bin/env bash
# PRÓBA POŻAROWA KILL-SWITCHA - doprowadza do prawdziwego zadziałania i sprawdza
# każde ogniwo łańcucha na żywym systemie.
#
# DLACZEGO TO ISTNIEJE, SKORO SĄ TESTY
#
# Testy jednostkowe sprawdzają reguły. Nie sprawdzają, czy silnik je woła, czy
# wiersz dociera do bazy, czy dozór to zauważa i czy powiadomienie wychodzi.
# Kill-switch jest jedynym mechanizmem, którego awaria nie objawia się niczym:
# silnik po jego zadziałaniu wygląda tak samo jak silnik bez sygnału do wejścia.
#
# Ten skrypt podnosi PRÓG KAPITAŁU powyżej bieżącego kapitału, czeka, aż silnik
# sam wykryje przekroczenie, i sprawdza:
#
#     1. czy powstał wiersz w risk.killswitch_event
#     2. czy silnik wstrzymał wejścia
#     3. czy dozór wygenerował powiadomienie
#     4. czy po zwolnieniu silnik wraca do handlu
#
# CO ZOSTAJE ZMIENIONE I JAK WRACA
#
# Zmieniana jest JEDNA wartość: risk.limit_def.value dla min_equity. Skrypt
# przywraca ją w pułapce EXIT, więc wraca także po przerwaniu i po błędzie.
# Kill-switch wywołany próbą jest na końcu zwalniany z notatką "PRÓBA POŻAROWA".
#
# Konto jest papierowe (mode=demo), więc próba nie dotyka prawdziwych pieniędzy.
# Na koncie live TEJ PRÓBY NIE URUCHAMIAĆ bez zamknięcia pozycji - wstrzymanie
# wejść na kilkanaście minut jest nieszkodliwe, ale nie jest niczym.
#
# Użycie (z katalogu projektu):
#   ssh root@185.201.112.104 'bash -s' < scripts/proba-killswitcha.sh
set -euo pipefail

ENGINE=${ENGINE:-crypto}
MODE=${MODE:-demo}
# Ile czekać na cykl silnika (co 300 s) i na dozór (co 600 s).
CZEKAJ_SILNIK=${CZEKAJ_SILNIK:-420}
CZEKAJ_DOZOR=${CZEKAJ_DOZOR:-660}

psql_() {
  kubectl exec -i deploy/trader-db -- sh -c \
    "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -At -F'|' -c \"$1\""
}

echo "=== PRÓBA POŻAROWA KILL-SWITCHA: $ENGINE/$MODE ==="

KAPITAL=$(psql_ "SELECT round(equity::numeric,2) FROM exec.equity_point \
  WHERE engine='$ENGINE' AND mode='$MODE' ORDER BY event_time DESC LIMIT 1")
PROG_PRZED=$(psql_ "SELECT value FROM risk.limit_def \
  WHERE engine='$ENGINE' AND mode='$MODE' AND name='min_equity'")

if [ -z "$PROG_PRZED" ]; then
  echo "BŁĄD: limit min_equity nie istnieje - próba nie ma czym wyzwolić" >&2
  exit 1
fi
AKTYWNE=$(psql_ "SELECT count(*) FROM risk.killswitch_event \
  WHERE engine='$ENGINE' AND mode='$MODE' AND released_at IS NULL")
if [ "$AKTYWNE" != "0" ]; then
  echo "BŁĄD: kill-switch jest już aktywny ($AKTYWNE). Zwolnij go najpierw -" >&2
  echo "      inaczej próba nie odróżni swojego zdarzenia od zastanego." >&2
  exit 1
fi

echo "kapitał: $KAPITAL   próg przed próbą: $PROG_PRZED   aktywnych: 0"

# PRZYWRACANIE W PUŁAPCE, nie na końcu skryptu: próg musi wrócić także wtedy,
# gdy próba padnie w połowie albo ktoś ją przerwie. Zostawiony podniesiony próg
# zatrzymywałby silnik w nieskończoność, a to jest dokładnie ta awaria, przed
# którą kill-switch ma chronić.
przywroc() {
  echo
  echo "--- przywracanie progu na $PROG_PRZED ---"
  psql_ "UPDATE risk.limit_def SET value = $PROG_PRZED, updated_at = now() \
    WHERE engine='$ENGINE' AND mode='$MODE' AND name='min_equity'" > /dev/null
  psql_ "SELECT 'próg po przywróceniu: ' || value FROM risk.limit_def \
    WHERE engine='$ENGINE' AND mode='$MODE' AND name='min_equity'"
}
trap przywroc EXIT

PROG_PROBY=$(psql_ "SELECT round(($KAPITAL + 500)::numeric, 0)")
echo
echo "--- podnoszę próg na $PROG_PROBY (powyżej kapitału) ---"
psql_ "UPDATE risk.limit_def SET value = $PROG_PROBY, updated_at = now() \
  WHERE engine='$ENGINE' AND mode='$MODE' AND name='min_equity'" > /dev/null

echo
echo "--- OGNIWO 1-2: czekam na cykl silnika (do ${CZEKAJ_SILNIK}s) ---"
ID=""
KONIEC=$((SECONDS + CZEKAJ_SILNIK))
while [ $SECONDS -lt $KONIEC ]; do
  ID=$(psql_ "SELECT id FROM risk.killswitch_event \
    WHERE engine='$ENGINE' AND mode='$MODE' AND released_at IS NULL \
    ORDER BY triggered_at DESC LIMIT 1")
  [ -n "$ID" ] && break
  sleep 15
done
if [ -z "$ID" ]; then
  echo "NIEZALICZONE: silnik nie uruchomił kill-switcha mimo kapitału poniżej progu." >&2
  echo "  To jest awaria samego zabezpieczenia, nie próby." >&2
  exit 1
fi
psql_ "SELECT '  zadziałał: id=' || id || '  ' || reason FROM risk.killswitch_event WHERE id=$ID"

echo
echo "--- OGNIWO 3: czy silnik wstrzymał wejścia ---"
if kubectl logs deploy/trader-engine --tail=40 2>/dev/null | grep -q "wejścia wstrzymane"; then
  kubectl logs deploy/trader-engine --tail=40 | grep "wejścia wstrzymane" | tail -1
else
  echo "  (jeszcze nie widać w logu - silnik zdąży przy następnym cyklu)"
fi

echo
echo "--- OGNIWO 4: czy dozór zgłosił (do ${CZEKAJ_DOZOR}s) ---"
POW=""
KONIEC=$((SECONDS + CZEKAJ_DOZOR))
while [ $SECONDS -lt $KONIEC ]; do
  POW=$(psql_ "SELECT count(*) FROM notify.notification \
    WHERE event_code='killswitch.triggered' \
      AND created_at > (SELECT triggered_at FROM risk.killswitch_event WHERE id=$ID)")
  [ "$POW" != "0" ] && break
  sleep 20
done
if [ "${POW:-0}" = "0" ]; then
  echo "NIEZALICZONE: kill-switch zadziałał, a nikt się o tym nie dowiedział." >&2
  echo "  Cichy kill-switch jest gorszy niż jego brak - daje złudzenie kontroli." >&2
  exit 1
fi
echo "  powiadomień: $POW"

echo
echo "--- OGNIWO 5: zwolnienie ---"
psql_ "UPDATE risk.killswitch_event SET released_at = now(), \
  release_note = 'PRÓBA POŻAROWA - zdarzenie wywołane celowo przez scripts/proba-killswitcha.sh' \
  WHERE id = $ID" > /dev/null
psql_ "SELECT '  zwolniony: ' || released_at || '  ' || release_note \
  FROM risk.killswitch_event WHERE id=$ID"

echo
echo "=== PRÓBA ZALICZONA: wszystkie ogniwa zadziałały ==="
echo "Zwolnienie przez interfejs (zakładka Ryzyko) sprawdź osobno - ta ścieżka"
echo "wymaga zalogowanego człowieka i tego skrypt nie zastąpi."
