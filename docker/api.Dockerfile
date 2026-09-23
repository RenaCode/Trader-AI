# BAZA PRZYPIETA PO DIGESCIE, NIE PO TAGU.
#
# Obraz jest tagowany `trader-ai-api:sha-<commit>` (build-on-vps.sh), co
# stwierdza "to jest ten commit". Przy ruchomym tagu `3.12-slim` to nieprawda:
# dwa buildy tego samego commita, wykonane w odstepie tygodni, daja rozne
# obrazy pod ta sama nazwa. Na tym samym zalozeniu stoi sprzatanie, ktore
# zachowuje N ostatnich obrazow "do cofniecia sie w razie czego" - cofniecie
# przez przebudowanie starszego commita nie przywracalo stanu przetestowanego.
#
# Digest odczytany z registry-1.docker.io 2026-09-23 (python:3.12-slim,
# indeks wieloplatformowy, zawiera linux/amd64). Podbicie bazy = swiadoma
# zmiana tej linii, a nie skutek uboczny daty builda.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY services/api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ ./services/
COPY db/ ./db/
COPY scripts/ ./scripts/

# Nie root - kontener nie ma powodu miec uprawnien administracyjnych.
RUN useradd --system --uid 10001 trader && chown -R trader:trader /app
USER trader

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
