# Dzienniki audytowe — wrzesień 2026

Cztery dokumenty z sesji audytowych z 7–8 września 2026. Powstały w tymczasowych
katalogach roboczych agentów (`.claude/worktrees/`) i **nigdy nie zostały
zacommitowane** — katalogi były kasowane, dokumenty przetrwały przypadkiem.
Przeniesione tutaj 15 września 2026, bez zmian w treści.

Kod, który tym audytom towarzyszył, wszedł na `main` inną drogą. Poniżej stan
każdego ustalenia, sprawdzony wobec `main` w dniu przenoszenia.

## Co zostało załatwione

| Znalezisko | Gdzie | Stan na `main` |
|---|---|---|
| A — nieudane anulowanie przy wypełnieniu częściowym zostawia żywe zlecenie | `audyt-egzekucji` | **Zamknięte.** Trzy testy: `test_nieudane_anulowanie_NIE_gubi_wypelnienia`, `..._zostawia_SLAD_a_nie_cisze`, `..._ZOSTAWIA_plan_do_ponowienia` |
| D — prowizja księgowana zawsze w walucie kwotowanej | `audyt-egzekucji` | **Zamknięte.** `gielda_exec.py:265` bierze `stan.prowizja_w` |
| E/B — odrzucenie PostOnly zostaje jako `unknown` na zawsze | `audyt-egzekucji` | **Zamknięte.** `dozor.py:1310-1321` domyka wiersze `unknown` |
| F — brak `wyjscie_bez_pozycji` w ścieżce papierowej | `audyt-egzekucji` | **Zamknięte.** `loop.py:1066` `zapisz_wyjscie_bez_pozycji` |
| R1 — `posprzataj_zlecenia` gubi wypełnienie na wierszu `partial` | `audyt-5` | **Zamknięte.** Guard `!= "partial"` zniknął; `gielda_exec.py:514` |
| Zastąpienie `health-check.sh` CronJobem w klastrze | `research-health-check` | **Wdrożone** jako `charts/monitoring` w `renacode-infra`. Bash chodzi świadomie równolegle — patrz komentarz w `cron/crontab` |

## Co jest NADAL OTWARTE

Sekcja „Mutacje — czego testy NIE ŁAPIĄ" w `audyt-5` opisuje trzy miejsca,
w których wstrzyknięcie błędu NIE powoduje upadku żadnego testu. Sprawdzone
15 września — pierwsze z nich nadal obowiązuje:

- **`test_sprzatanie_NIE_RUSZA_planow_ktore_jeszcze_czekaja`**
  (`tests/test_gielda_exec.py:758`) sprawdza wyłącznie, czy tekst zapytania
  zawiera `p.status::text <> 'pending'` (linia 763). Zamiana filtra na
  `... OR TRUE` przepuszcza całą suitę. Bez tego guardu sprzątanie zdejmowałoby
  z arkusza zlecenie planu, którego świeca jeszcze się nie zamknęła — każde
  wejście i wyjście ginęłoby o świecę za wcześnie.

Pozostałe dwa punkty tej sekcji (`ORDER BY created_at` w `zbuduj_adapter`,
`test_WYJSCIE_poza_pasmem_jest_PRZYCIAGANE`) nie były weryfikowane ponownie.
