# Przegląd Trader-AI — notatki na bieżąco

Stan: start. Egzekucja WŁĄCZONA na demo.

## Plan
1. decided_for_bar / okno decyzji
2. PostOnly
3. pasmo ceny (sprawdz_zlecenie, zloz_plan)
4. zapisz_wejscie(order_id)
5. rozstrzygnij_plan / nie_zdjete
6. anuluj_wejscia
7. update_equity realized_pnl
8. RiskKernel limity
9. migracje 0032/0033
10. atrapa Kursor w testach

## Znaleziska
(w toku)

### [1] KRYTYCZNE — `zloz_oczekujace` odwołuje się do nieistniejącej kolumny `fv.feature_id`
Plik: services/engine/gielda_exec.py:349
Kolumna w bazie nazywa się `feature_def_id` (db/migrations/0004:28). Cały SELECT
pobierający plany do wysyłki rzuci UndefinedColumn -> ŻADNE zlecenie nie idzie na giełdę.
Wprowadzone razem z pasmem ceny (commit 65f443a).
Do potwierdzenia: czy jest test pokrywający tę ścieżkę (nie ma - suite przechodzi).
