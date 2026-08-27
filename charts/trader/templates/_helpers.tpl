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
