FROM python:3.12-slim AS base
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
