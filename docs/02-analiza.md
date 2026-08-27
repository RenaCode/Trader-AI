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
| `trader-api`, `trader-web` | **blokada** | `ImagePullBackOff` — prywatne pakiety GHCR bez poświadczeń |

Baza po 45 dniach danych demo: **25 MB**. Ekstrapolacja: ~200 MB/rok przy 5 instrumentach.
PVC 20 Gi wystarcza z bardzo dużym zapasem — potwierdza, że wybór Postgresa zamiast
QuestDB/ClickHouse był trafny dla tej skali.

## 2. Znalezione defekty

### 2.1 Brak jakiegokolwiek uwierzytelniania — naprawione doraźnie

Żaden endpoint API nie był chroniony. Po wdrożeniu pod publiczną domeną cały dashboard —
pozycje, decyzje, stan konta — byłby czytelny dla każdego.

**Zrobione:** middleware BasicAuth na Traefiku (`charts/trader/templates/basicauth.yaml`).
**To nie jest rozwiązanie docelowe** — zamyka okno do czasu wdrożenia hasła + TOTP.
Do usunięcia razem z wdrożeniem 2FA.

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
