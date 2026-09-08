{{- define "trader.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "trader.fullname" -}}
{{- printf "%s" (default .Chart.Name .Values.nameOverride) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "trader.labels" -}}
app.kubernetes.io/name: {{ include "trader.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* Połączenie WŁAŚCICIELA schematów. Wyłącznie do migracji i konserwacji. */}}
{{- define "trader.databaseUrl" -}}
postgresql://{{ .Values.db.username }}:$(POSTGRES_PASSWORD)@{{ include "trader.fullname" . }}-db:5432/{{ .Values.db.database }}
{{- end -}}

{{/*
Połączenie APLIKACJI i procesów roboczych — rola o minimalnych uprawnieniach.

Nie jest właścicielem żadnej tabeli, więc nie może wyłączyć wyzwalacza
chroniącego niezmienność dziennika decyzji ani zmienić struktury bazy —
niezależnie od tego, co zdołałby wykonać błąd w kodzie albo wstrzyknięcie SQL.
*/}}
{{- define "trader.appDatabaseUrl" -}}
postgresql://{{ .Values.db.appUsername }}:$(APP_DB_PASSWORD)@{{ include "trader.fullname" . }}-db:5432/{{ .Values.db.database }}
{{- end -}}

{{/* Zmienna z hasłem roli aplikacji — wstrzykiwana wszędzie, gdzie użyto appDatabaseUrl. */}}
{{- define "trader.appDbPasswordEnv" -}}
- name: APP_DB_PASSWORD
  valueFrom:
    secretKeyRef: { name: {{ .Values.secretName }}, key: APP_DB_PASSWORD }
{{- end -}}

{{/*
POŚWIADCZENIA PER PROCES (migracja 0034).

Do tej pory silnik, dozorca ryzyka, czujka żywotności i próba pożarowa łączyły
się do bazy JEDNĄ rolą `trader_app`. Nic nie WYMUSZAŁO, że silnik nie zwolni
sobie kill-switcha — nie robił tego, bo nie miał takiego kodu, a nie dlatego,
że nie mógł.

Rozdział uprawnień jest wart tyle, ile rozdział poświadczeń: gdyby wszystkie
role miały to samo hasło, proces o węższych nadaniach po prostu połączyłby się
tą rolą, która ma szersze. Dlatego każda rola ma własny klucz w sekrecie.

Wywołanie:  {{`{{- include "trader.rolaDbEnv" (dict "ctx" $ "rola" "silnik") | nindent 12 }}`}}

Nazwa roli MUSI istnieć w .Values.db.role — literówka ma zatrzymać `helm
template`, a nie po cichu wygenerować pusty login, na którym pod wstanie
i połączy się jako nikt.
*/}}
{{- define "trader.rolaDbEnv" -}}
{{- $ := .ctx -}}
{{- $r := index $.Values.db.role .rola -}}
{{- if not $r }}{{ fail (printf "Nie ma roli bazodanowej %q w .Values.db.role" .rola) }}{{ end }}
- name: ROLA_DB_PASSWORD
  valueFrom:
    secretKeyRef: { name: {{ $.Values.secretName }}, key: {{ $r.secretKey }} }
- name: DATABASE_URL
  value: "postgresql://{{ $r.user }}:$(ROLA_DB_PASSWORD)@{{ include "trader.fullname" $ }}-db:5432/{{ $.Values.db.database }}"
{{- end -}}

{{/*
Hasła WSZYSTKICH ról procesów — wyłącznie dla kontenera migracji.

Migrator chodzi rolą właściciela i ustawia nimi hasła pozostałych ról (patrz
db/migrate.py). Jest jedynym miejscem, które widzi je wszystkie naraz; gdyby
robił to ktoś inny, hasło roli dozorcy musiałoby trafić do poda, który nie jest
dozorcą.
*/}}
{{- define "trader.rolaHaslaEnv" -}}
{{- range $nazwa, $r := .Values.db.role }}
- name: {{ $r.secretKey }}
  valueFrom:
    secretKeyRef: { name: {{ $.Values.secretName }}, key: {{ $r.secretKey }} }
{{- end }}
{{- end -}}
