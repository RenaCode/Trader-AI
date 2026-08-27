# Trader-AI — analiza funkcjonalności i kodu

Data: 2026-08-27, po pierwszym wdrożeniu. Analiza własnego kodu, więc traktuj ją jako
listę znanych słabości, a nie certyfikat jakości — część rzeczy znajdzie dopiero
zderzenie z prawdziwą giełdą.

## 1. Stan wdrożenia

| Komponent | Stan | Uwaga |
|---|---|---|
| PostgreSQL 17 + TimescaleDB + pgvector | **działa** | 8 hypertables, 3 ciągłe agregaty, PVC 20 Gi |
| Redis | **działa** | PVC 1 Gi |
| Certyfikat TLS | **działa** | Let's Encrypt, ważny do 2026-11-25, HTTP→HTTPS 301 |
| Ingress Traefik | **działa** | `trader.renacode.com` |
| `trader-api`, `trader-web` | **działa** | obrazy budowane na VPS (prywatne GHCR bez tokenu) |
| Logowanie (Argon2id + TOTP) | **działa** | zweryfikowane end-to-end przez publiczny URL |
| Test dymny w CI | **działa** | blokuje build obrazów przy niepowodzeniu |

Baza po 45 dniach danych demo: **25 MB**. Ekstrapolacja: ~200 MB/rok przy 5 instrumentach.
PVC 20 Gi wystarcza z bardzo dużym zapasem — potwierdza, że wybór Postgresa zamiast
QuestDB/ClickHouse był trafny dla tej skali.

## 2. Znalezione defekty

### 2.1 Brak jakiegokolwiek uwierzytelniania — naprawione

Żaden endpoint API nie był chroniony. Po wdrożeniu pod publiczną domeną cały dashboard —
pozycje, decyzje, stan konta — byłby czytelny dla każdego.

**Zrobione:** middleware BasicAuth na Traefiku (`charts/trader/templates/basicauth.yaml`),
użytkownik `admin`. Zgodnie z decyzją właściciela do czasu uruchomienia produkcyjnego
wystarcza admin + hasło; **2FA (TOTP) pozostaje celem docelowym** i musi wejść przed
przejściem na tryb live — wtedy ten middleware znika na rzecz logowania w samym API.

### 2.2 `feat.as_of` z domyślną świeżością 1 h zwraca pustkę

Domyślny `p_max_staleness := interval '1 hour'` przy cechach liczonych co 2 h zwraca
**zero wierszy** — sprawdzone: `feat.as_of(1, now(), NULL)` → 0, a z `interval '6 hours'` → 6.

To nie jest błąd w logice bitemporalnej (ta działa poprawnie, patrz 3.1), tylko źle dobrana
wartość domyślna. W produkcji cechy będą liczone gęściej, ale domyślna wartość nie powinna
milcząco zwracać pustki. **Do poprawy:** domyślna świeżość wyprowadzana z `feature_def.params`
albo jawny błąd zamiast cichego zera.

### 2.3 `signal-performance` sumuje ten sam P&L wielokrotnie

Zapytanie łączy `decision_contribution` z `exec.position`, więc decyzja z pięcioma sygnałami
dokłada pełny P&L pozycji do **każdego** z nich. Zmierzone: suma po sygnałach **1930**
przy faktycznym P&L **661** — prawie trzykrotne zawyżenie.

Jako *atrybucja* to jest obronne (każdy sygnał współtworzył wynik), ale kolumna nazywa się
„P&L" i użytkownik odczyta ją jako pieniądze. **Do poprawy:** albo ważyć P&L udziałem
sygnału w decyzji (`contribution / suma|contribution|`), albo przemianować na
„P&L decyzji, w których sygnał uczestniczył".

### 2.4 `TRUNCATE` omija trigger append-only

Trigger `journal.forbid_mutation` blokuje `UPDATE` i `DELETE`, ale `TRUNCATE` przechodzi —
potwierdzone. Wykorzystuje to `seed_demo.py`, i to jest wygodne w rozwoju, ale w produkcji
oznacza, że dziennik audytowy da się wyzerować jednym poleceniem.

**Do poprawy:** rola aplikacyjna bez uprawnienia `TRUNCATE` na schemacie `journal`
(osobna rola migracyjna je zachowuje). Naprawa jest w warstwie uprawnień, nie triggerów —
w Postgresie nie ma triggera `BEFORE TRUNCATE ... FOR EACH ROW`.

### 2.5 Cztery nieużywane importy — i zepsuty detektor

`fmtPct` i `STATE_TONE` w `Learning.jsx`, `fmtAgo` w `Moves.jsx`, `fmtDate` w `Overview.jsx`.
Usunięte.

Ważniejsza jest przyczyna, dla której prawie ich nie zauważyłem: pierwszy detektor
zgłosił „brak" i wyglądał wiarygodnie. Dopiero wstrzyknięcie znanego-nieistniejącego symbolu
pokazało, że nie wykrywał **niczego** — regex nie obsługiwał importów wieloliniowych,
a wszystkie cztery takie były. Cisza narzędzia nie jest dowodem.

## 3. Co faktycznie zweryfikowano

Poniższe zostały sprawdzone przez wstrzyknięcie przypadku, który **musi** zostać odrzucony —
nie przez „przeszło bez błędu".

### 3.1 Ochrona przed leakage'em (najważniejsza właściwość projektu)
Cecha o `event_time` 10:00 i `ingested_at` 10:07 jest **niewidoczna** dla zapytania
`as_of` 10:03 (zwraca starszą, znaną na czas wartość 31.5), a widoczna dla 10:10.
Bez tego cała warstwa uczenia produkowałaby fałszywe wyniki miesiącami.

### 3.2 Niemutowalność dziennika
`UPDATE` i `DELETE` na `journal.decision` kończą się błędem; wiersz pozostaje nietknięty.

### 3.3 Klucz API z prawem wypłaty
`CHECK (NOT (active AND can_withdraw))` odrzuca wstawienie aktywnego klucza z prawem
wypłaty. Zabezpieczenie w schemacie, nie w kodzie aplikacji.

### 3.4 Idempotencja migracji
Drugi przebieg `db/migrate.py` jest no-opem; zmiana zastosowanego pliku jest wykrywana
przez sumę kontrolną i blokuje start. Weryfikowane też w CI (job „Weryfikacja migracji").

### 3.5 API
Wszystkie 34 endpointy używane przez dashboard zwracają 200 z danymi, dla obu silników.

## 4. Czego jeszcze nie ma

Kolejność odzwierciedla zależności, nie ważność.

1. **Uwierzytelnianie: hasło (Argon2id) + TOTP + szyfrowane klucze API.** Blokuje wszystko
   inne, bo to tutaj podasz poświadczenia do giełdy. Schemat bazy jest gotowy
   (`app.user`, `app.totp_secret`, `app.api_credential`, `app.webauthn_credential`).
2. **Adapter Bybit demo + ingest danych.** Do tej pory wszystko żywi się `seed_demo.py`.
3. **Worker RSS + analiza Claude.** Schemat `news.*` gotowy, workera brak.
4. **Silnik sygnałów i risk kernel.** Limity są w bazie, ale nikt ich jeszcze nie egzekwuje —
   `risk.limit_def` to na razie deklaracja, nie mechanizm.
5. **Warstwa uczenia.** `learn.*` jest zapełniane przez seeder; prawdziwego walk-forward
   jeszcze nie ma.
6. **Adapter IBKR.** Wymaga IB Gateway w kontenerze + IBeam do podtrzymania sesji —
   jedyny komponent systemu, który nie będzie w pełni bezobsługowy.

## 5. Dług techniczny do spłacenia wcześniej niż później

| Rzecz | Dlaczego teraz |
|---|---|
| Testy API (pytest) | Zero testów automatycznych; CI sprawdza tylko migracje |
| Rola aplikacyjna bez `TRUNCATE`/`SUPERUSER` | Patrz 2.4 |
| Rotacja `SECRETS_KEY` | Klucz szyfruje sekrety TOTP i klucze giełd — brak procedury wymiany |
| Backup bazy | PVC bez snapshotów. Dziennik decyzji jest nieodtwarzalny — to nie są dane, które można przeliczyć ponownie |
| Limit `max_tokens`/koszt Anthropic | Worker newsów będzie wołał API w pętli; brak twardego budżetu |
| `sig.signal_value` bez polityki kompresji | Pozostałe hypertables ją mają |

---

## 6. Uzupełnienie po wdrożeniu logowania (27.08, wieczorem)

Cztery kolejne defekty, wszystkie znalezione dopiero przez oglądanie działającej
aplikacji — żadnego nie zgłosił build ani lint.

| # | Defekt | Skutek | Dlaczego umknął |
|---|---|---|---|
| 1 | Pula połączeń bez `autocommit` | Zapisy sesji i audytu byłyby cofane przy zwrocie połączenia | Testy odczytu przechodziły; problem dotyczył wyłącznie zapisu |
| 2 | Brak `SECRETS_KEY` wywalał się dopiero przy pierwszym użyciu 2FA | Aplikacja startowała „zdrowa" z niedziałającą funkcją | Health check nie dotykał szyfrowania |
| 3 | `viewBox` o stałej szerokości przy sztywnej wysokości | Wykresy renderowały się na ~1/3 karty | Build kompiluje się poprawnie |
| 4 | Ścieżka SVG liczona przed sprawdzeniem pustych danych | **Biały ekran po każdym zalogowaniu** | j.w. |
| 5 | `executemany` z 21 600 wierszami | Seeder ubijany przez jądro na limicie 1 GiB, dane wgrane w połowie | Lokalnie limitu nie ma |

Wnioski, które zmieniły sposób pracy:

- **Cisza narzędzia nie jest dowodem.** Pierwszy detektor nieużywanych importów
  zgłosił „brak" i nie wykrywał niczego — nie obsługiwał importów wieloliniowych,
  a wszystkie cztery znalezione takie były. Wyszło dopiero po wstrzyknięciu
  znanego-złego symbolu.
- **Ten sam test zastosowany do testu dymnego.** Zanim uznałem go za działający,
  wstrzyknąłem obie naprawione usterki z powrotem: regresja wykresów daje dwa
  jawne błędy, biały ekran przerywa test. Dopiero wtedy trafił do CI.
- **Globalny `.gitignore` użytkownika po cichu wykluczał** `package-lock.json`
  i cały katalog `scripts` — dwa commity twierdziły, że dodają pliki, których
  `git add` nigdy nie objął. Build wywalał się dopiero na VPS.

---

## 7. Etap silnika decyzyjnego (27.08, wieczorem)

### Co działa na realnych danych

| Warstwa | Stan |
|---|---|
| Świece 1 m z Bybita | 414 tys., 90 dni, zero luk w szeregu |
| Cechy z realnych świec | 697 tys. wartości, 9 cech × 3 instrumenty |
| Sygnały i reżim | 6 sygnałów, 5 reżimów, wagi jako jawne założenia początkowe |
| Risk kernel | limity egzekwowane przed każdym zleceniem |
| Dziennik decyzji | realne decyzje z pełnym rozbiciem na wkłady |
| Trener walk-forward | 38 okien, trening 14 dni / test 2 dni, krok 2 dni |

### Trzy błędy złapane w tym etapie

**1. Sharpe rzędu 40 milionów.** Przy dwóch niemal identycznych transakcjach
w oknie odchylenie standardowe schodziło niemal do zera, a iloraz `mean/sd`
eksplodował. Do tego anualizacja z okna dwudniowego mnożyła szum przez ~50.
Naprawione trzema rzeczami: minimum 5 transakcji (poniżej okno jest oznaczone
jako *bez informacji* i nie liczy się ani na plus, ani na minus), podłoga na
odchyleniu, oraz rezygnacja z anualizacji na rzecz Sharpe'a **na transakcję**.

To jest przykład liczby, która *wyglądała* jak wynik. Gdyby przeszła, zakładka
„Stopień nauki" pokazywałaby imponujące wartości bez żadnego znaczenia.

**2. `hash()` jako identyfikator cechy.** Wbudowany `hash()` w Pythonie jest
losowany przy każdym starcie interpretera (`PYTHONHASHSEED`), więc ta sama cecha
dostawałaby inne `ref_id` po restarcie, a `ON CONFLICT` przestałby działać.
Zamienione na `crc32`.

**3. Długie zadanie w podzie API.** Trening uruchomiony przez `kubectl exec`
ginął przy każdym wdrożeniu razem z podem — dwa razy straciłem kilkadziesiąt
minut obliczeń. Trener jest teraz osobnym procesem z własnym cyklem.

### Decyzje projektowe warte zapamiętania

**Datowanie wstecz cech.** Cecha liczona wyłącznie z zamkniętych świec dostaje
`ingested_at = event_time + 3 s`, bo gdyby system działał wtedy, policzyłby ją
wtedy — jej wejścia były dostępne. Cecha newsowa **nie może** być datowana wstecz:
dziedziczy opóźnienie analizy artykułu (`an.ingested_at <= T` w zapytaniu).
To rozróżnienie jest granicą między poprawnym backtestem a takim, który cicho
uczy się z przyszłości.

**Wejście po cenie następnej świecy.** Decyzja podjęta na zamknięciu świecy T
jest wykonywana po cenie świecy T+1. Wykonanie po cenie tej samej świecy to
jeden z najczęstszych i najbardziej optymistycznych błędów w backtestach.

**Wagi sygnałów to jawne założenia, nie wynik dopasowania.** Do czasu, aż
walk-forward je zastąpi, każda decyzja systemu jest decyzją tych założeń — i tak
należy ją czytać. Założenia startują jako jeden z kandydatów w każdym oknie:
jeśli losowanie nie potrafi ich pobić na treningu, zostają.

### Subskrypcja Claude.ai a dostęp do API

Osobna sprawa, która często myli: plan Pro/Max na claude.ai **nie obejmuje**
wywołań API. To odrębny produkt z odrębnym rozliczeniem i nie istnieje mechanizm
„zaloguj się subskrypcją" dla aplikacji zewnętrznej. Klucz powstaje w Claude
Console (`platform.claude.com`) po doładowaniu konta. Interfejs mówi to wprost
i pozwala sprawdzić klucz prawdziwym wywołaniem — poprawny format nie dowodzi
niczego, bo klucz może być odwołany albo bez środków.
