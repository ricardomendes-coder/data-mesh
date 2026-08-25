FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/secrets \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# --workers gives the app more than one process, so requests stop serialising
# on a single core. Three is a moderate default for a small internal RDS; raise
# it if the container has the cores and the instance has the connections (each
# worker keeps its own pooled connections — see app/db.py).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/report", "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "3"]
