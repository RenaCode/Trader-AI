# Trader-AI

Autonomiczny system tradingowy z własnym dashboardem, uruchamiany na VPS RenaCode.
**Dwa niezależne silniki** — krypto (Bybit EU) i giełda tradycyjna (Alpaca, akcje USA) —
z osobnymi modelami, osobnym ryzykiem i osobnymi zakładkami w UI.

> **Faza demo.** System handluje wyłącznie na kontach demonstracyjnych. Przejście na
> live wymaga spełnienia kryteriów opisanych w dokumentacji wewnętrznej (research, §9.2) i osobnego,
> jawnego potwierdzenia w interfejsie. Projekt nie zakłada rentowności — sześć miesięcy
> demo to test tej hipotezy, a wynik negatywny jest realnym rezultatem.

## Dokumentacja

Dokumentacja wewnętrzna projektu (research, architektura danych, pomiary, rejestr
hipotez, audyty) jest prowadzona w osobnym, prywatnym repozytorium.

## Architektura w skrócie

```
web (nginx + React)  ──► api (FastAPI) ──┬──► PostgreSQL 17 + TimescaleDB + pgvector
   trader.renacode.com                    ├──► Redis
                                          └──► API modelu (Anthropic albo Gemini)
       engine-crypto ─┐
       engine-equity ─┴──► wspólny rdzeń: dane, newsy, cechy, risk kernel, audyt
```

Trzy decyzje, które niosą resztę projektu:

1. **Bitemporalność.** Każdy fakt ma `event_time` (kiedy zaszło) i `ingested_at` (kiedy
   system się dowiedział). Odczyt do treningu idzie wyłącznie przez `feat.as_of()`, która
   filtruje po obu. Bez tego model uczyłby się na newsach z przyszłości.
2. **Dziennik decyzji jest append-only** (wymuszone triggerem) i zapisywany *przed*
   poznaniem wyniku, razem ze snapshotem cech. To jednocześnie odpowiedź na „dlaczego"
   w UI i dataset wolny od leakage'u.
3. **Metryki tylko out-of-sample**, z licznikiem przetestowanych konfiguracji. Nie ma
   gdzie wpisać „wyniku backtestu", bo świetny backtest jest sygnałem przeuczenia.

## Uruchomienie lokalne

```bash
cp .env.example .env
# wygeneruj klucz szyfrowania sekretów:
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # -> SECRETS_KEY

docker compose up -d              # PostgreSQL + TimescaleDB + pgvector, Redis
python -m venv .venv && .venv/bin/pip install -r services/api/requirements.txt

set -a; . ./.env; set +a
.venv/bin/python db/migrate.py            # migracje

.venv/bin/uvicorn services.api.main:app --port 8099 &
cd web && npm install && npm run dev      # http://localhost:5174
```

**Dane demonstracyjne zostały usunięte** — system pracuje wyłącznie na realnych
świecach z giełdy. `purge_demo.py`
kasuje wszystko, co wygenerował, zostawiając realne dane rynkowe.

## Procesy tła

| Proces | Co robi | Zależność |
|---|---|---|
| `market` | świece 1 m i snapshoty orderbooka, co 60 s | — |
| `history` | świece 1 h i stawki finansowania, co 20 min | — |
| `features` | cechy na obu horyzontach (`4h,1h`), co 15 min | historia |
| `engine` | pętla decyzyjna; crypto: `--strategia momentum` (reguła H-3 → risk kernel → giełda demo), equity (`engine-equity`): ensemble reżim → sygnały → risk kernel | cechy |
| `trainer` | walk-forward, raz na dobę, po nim **dozór**; **CronJob, nie worker** | cechy |

Każdy z tych procesów ma **pętlę we własnym module**, a nie w komendzie powłoki.
`sh -c "while true; do ... || true; sleep N; done"` połykał każdy niezerowy wynik:
awaria nie restartowała kontenera i nie podnosiła licznika restartów, więc pod
stał „Running" i mógł nie robić nic od dni. Pętla w module łączy się z bazą **na
każdy cykl** (jedno połączenie na cały proces po restarcie bazy zrywa się raz
i już się nie naprawia) i **kończy proces po pięciu awariach pod rząd** — wpis
w logu nie jest widocznością, restart poda jest.

Trener jest wyjątkiem i pętli nie dostaje: trwa dziesiątki minut i musi dobiec
końca niezależnie od wdrożeń, dlatego jest zadaniem cyklicznym
(`templates/trainer-cron.yaml`).
| `news` | RSS → zapis; z kluczem także filtr → analiza, co 5 min | klucz dostawcy |
| `dozorca` | **ocena limitów konta NIEZALEŻNIE od silnika**, wyzwolenie i wznowienie kill-switcha, co 60 s | — |

## Niezmiennik danych rynkowych

**Do bazy trafiają wyłącznie świece ZAMKNIĘTE, a zapis je poprawia.**

`event_time` świecy to jej **początek**, więc warunek `event_time < now`
przepuszczał świecę trwającą — o 09:01 świeca 09:00 lądowała w bazie z zakresem
jednej minuty. Ponieważ wznawianie zaczynało od najnowszej posiadanej świecy,
a zapis pomijał konflikt, taka świeca nie była już nigdy odpytywana.

Zmierzone 2026-09-06 na BTCUSDC, 55 godzin z kompletem świec minutowych:
zamknięcie zgodne z faktycznym w **9 przypadkach**, zapisany zakres high-low
równy **39%** prawdziwego. Każda cecha z nich policzona — ATR, położenie
w zakresie, reżim zmienności — dziedziczyła to zaniżenie.

Trzy reguły, które to trzymają:

| reguła | gdzie |
|---|---|
| tylko świece domknięte: `event_time + step <= now` | `backfill.py`, `market_worker.py` |
| zapis **poprawia** istniejące, gdy wartości się różnią | `batched_insert`, `insert_values` |
| zakładka trzech świec przy wznawianiu | `backfill.py` |

Razem sprawiają, że potok **leczy się sam**: błędna świeca zostaje nadpisana
przy następnym przebiegu, a cechy z niej policzone — przy następnym liczeniu.
Tryb `backfill.py --napraw` pobiera zadane okno od nowa, gdy zakładka nie sięga
wystarczająco daleko wstecz.

## Egzekucja papierowa

Silnik nie składa zleceń na giełdzie — prowadzi **własny rachunek papierowy**
po realnych cenach. Zlecenia są jawnie oznaczone (`execution: internal_paper`,
`venue_fill_id: paper-*`), a koszt liczony z `services/engine/koszty.py` tym
samym wywołaniem co w symulacji walk-forward.

**Zlecenie z limitem jest planem, nie faktem.** Decyzja na zamknięciu świecy T
zapisuje `journal.planned_order` z ceną limitu i terminem ważności do końca
świecy T+1. Na początku kolejnego cyklu plan jest rozstrzygany regułą
`portfolio._wypelnione` — tą samą, której używa symulacja:

```
kupno   wypełnia się, gdy low  ≤ limit
sprzedaż                gdy high ≥ limit
```

Niewypełnienie jest **wynikiem, nie błędem**. Przy wyjściu oznacza, że pozycja
zostaje otwarta — najdroższy przypadek w całym modelu, bo dotyczy rynku, który
ucieka przeciwko nam.

Do 2026-09-06 pętla zapisywała wypełnienie od razu, przyjmując, że limit się
wypełnił. Błąd był systematyczny w jedną stronę: zlecenia, które się nie
wypełniają, to zwykle te, przy których rynek uciekł w naszą stronę, czyli
najbardziej zyskowne.

## Dostawca analizy wiadomości

Skaner wiadomości potrzebuje modelu do dwóch rzeczy: **triażu** (czy nagłówek
w ogóle może ruszyć ceną — model tani) i **analizy** (kierunek, siła, horyzont,
nowość — model mocniejszy). Dostawcę wybiera `services/ingest/llm.py` po tym,
który klucz jest ustawiony:

| kolejność | sekret w UI / zmienna | triaż | analiza |
|---|---|---|---|
| 1 | `anthropic_api_key` / `ANTHROPIC_API_KEY` | claude-haiku-4-5 | claude-opus-5 |
| 2 | `gemini_api_key` / `GEMINI_API_KEY` | gemini-3.5-flash-lite | gemini-3.6-flash |

Anthropic wygrywa, gdy ustawione są oba — jest dostawcą docelowym, a Gemini
wszedł jako ścieżka na czas oczekiwania na klucz. Klucz z bazy (wpisany
w Ustawieniach) bije zmienną środowiskową **tego samego** dostawcy; między
dostawcami rozstrzyga kolejność z tabeli, nie źródło klucza.

Zmierzony koszt analizy przez Gemini: **około 0,54 USD na dobę** przy 128
artykułach, z czego triaż to kilka procent — reszta idzie na artykuły, które
przez triaż przeszły. Limit dobowy (`LLM_DAILY_USD`, domyślnie 5 USD) zatrzymuje
potok twardo.

> **Zmiana dostawcy w trakcie nie jest neutralna.** Oceny dwóch różnych modeli
> nie są tym samym pomiarem, a cecha `news_sent` sklejona z obu ma w środku
> uskok, którego kalibracja wagi nie zakłada. Model zapisuje się przy każdym
> wierszu (`news.article_analysis.model`), a zmiana trybu jest wykrzyczana
> w logu — ale to jest wykrywalność, nie rozwiązanie.
>
> **Podpis to model z odpowiedzi, nie z konfiguracji** (od 2026-10): `modelVersion`
> u Gemini, `model` u Anthropica. Powód: Google wycofał `gemini-3.5-flash`
> i obsługuje zapytania o niego modelem `gemini-3.6-flash`, mówiąc o tym tylko
> w odpowiedzi. Wiersze sprzed tej zmiany podpisane `gemini-3.5-flash` mogą więc
> pochodzić z 3.6 — data przekierowania jest nieznana, więc w `news_sent` jest
> w tym okresie uskok, którego podpis nie pokazuje. Podmiana modelu przez
> dostawcę idzie do logu jako `[llm] … obsluzyl …`.

**Kiedy to wpłynie na model.** Nigdy wstecz. Analizy powstają od dnia wpięcia
klucza, więc `news_shock` jest martwy we wszystkich oknach historycznych. Wchodzi
do wag produkcyjnych dopiero po `MIN_OKIEN_ZYWEGO = 5` oknach, w których żyje —
przy kroku 30 dni to około pięciu miesięcy od dziś.

## Baza danych

| Schemat | Zawartość |
|---|---|
| `ref` | Giełdy, instrumenty, kalendarz sesji, akcje korporacyjne |
| `market` | Świece (hypertable), orderbook, funding. Godzinówki pobierane wprost
z giełdy; wyższe interwały liczone w locie — agregaty ciągłe usunięte w 0016 jako
drugie źródło tej samej prawdy |
| `news` | Kanały RSS, artykuły, analiza Claude, embeddingi (pgvector + HNSW) |
| `feat` | Wersjonowane definicje cech, wartości, reżim, funkcja `as_of()` |
| `sig` | Definicje i wartości sygnałów |
| `journal` | **Append-only** dziennik decyzji, rozbicie na wkłady, zlecenia planowane |
| `exec` | Zlecenia, wypełnienia, pozycje, krzywa kapitału |
| `learn` | Rejestr modeli, walk-forward, kalibracja, dryf, licznik hipotez |
| `risk` | Limity, wykorzystanie, naruszenia, kill-switch, heartbeat |
| `app` | Użytkownicy, TOTP, WebAuthn, sesje, zaszyfrowane klucze API, audyt |

Migracje są **niemutowalne** — runner porównuje sumy kontrolne i odmawia startu, jeśli
zastosowany plik został zmieniony. Poprawki idą jako nowy plik.

## Uwierzytelnianie

| Warstwa | Rozwiązanie |
|---|---|
| Hasło | Argon2id, 64 MiB / t=3 / p=4 (OWASP wymaga minimum 19 MiB / t=2 / p=1) |
| Drugi składnik | TOTP (RFC 6238) + 10 jednorazowych kodów zapasowych; włączenie wymaga hasła, wyłączenie hasła i kodu (TOTP lub zapasowego) |
| Sesja | 256 bitów z CSPRNG; w bazie wyłącznie SHA-256; cookie `HttpOnly` + `Secure` + `SameSite=Strict`; wygasa po 12 h albo po 30 min bezczynności |
| Session fixation | Nowa sesja przy logowaniu i po potwierdzeniu drugiego składnika |
| CSRF | Double-submit cookie na metodach zmieniających stan |
| Blokada | 15 minut po pięciu nieudanych próbach — wspólny licznik dla logowania, drugiego składnika i ponownego podania hasła (`/password`, `/totp/*`, `revoke-others`); identyczna odpowiedź i ta sama weryfikacja Argon2 (atrapa) dla nieistniejącego loginu |
| Webhook | tylko publiczne adresy po rozwiązaniu DNS, połączenie przypięte do sprawdzonego IP |
| Audyt | Każda próba logowania, zmiana hasła i operacja na 2FA w `app.audit_log` |

## Testy

### Testy jednostkowe

```bash
pip install -r services/api/requirements.txt -r requirements-dev.txt
python -B -m pytest
```

Zestaw liczy 3699 testów jednostkowych (reguły ryzyka, portfel, koszty, TOTP,
kryptografia), zmierzone 11.10.2026.

**Potrzebne są OBA pliki zależności.** `requirements-dev.txt` zawiera wyłącznie
to, czego obraz produkcyjny nie ma (sam `pytest`); testy importują `services/*`,
a te importują `psycopg`, `fastapi`, `httpx`, `yaml`, `pyotp` i `cryptography`.
Instalacja samego pliku deweloperskiego daje 59 błędów kolekcji, a komunikat
o brakującym module nie mówi, którego pliku brakuje.

`-B` wyłącza zapis bajtkodu i nie jest ozdobą: przy sprawdzaniu, czy test
naprawdę pilnuje tego, co obiecuje (wstawienie wady i sprawdzenie, że test
pada), mutacja pliku o niezmienionym rozmiarze potrafi trafić w stary `.pyc`
i dać wynik nie z tego kodu. Dokładnie te dwa polecenia uruchamia CI
(`.github/workflows/tests.yml`).

**Żaden z tych testów nie dotyka bazy ani giełdy.** Testy wymagające bazy
z zastosowanymi migracjami pomijają się same i mówią o tym wprost — żeby je
uruchomić, podaj `TEST_DB_URL` wskazujący na **jednorazową** bazę:

```bash
TEST_DB_URL=postgresql://... python -B -m pytest tests/test_role_bazodanowe.py
```

Liczba testów podana wyżej jest pilnowana testem
(`tests/test_dokumentacja_testow.py`): napis, który odjedzie od pomiaru, zapala
zestaw, zamiast cicho zostać nieprawdą — tak jak stało się z „123 testy
jednostkowe" przy faktycznych 1999.

### Test dymny interfejsu

```bash
BASE_URL=https://trader.renacode.com TEST_PASSWORD=... node tests/smoke.spec.mjs
```

Test dymny przechodzi pełny przepływ (odmowa bez sesji → logowanie → każda
zakładka → oba silniki → wylogowanie) i pilnuje, że każdy wykres renderuje się
w skali 1:1 względem swojego `viewBox`. Powstał, bo trzy regresje interfejsu
przeszły przez `npm run build` bez ostrzeżenia — build sprawdza kompilację,
nie to, czy cokolwiek widać.
