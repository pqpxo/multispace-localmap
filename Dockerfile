FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .
COPY public/ public/

# Ensure a default logo exists if none was provided
RUN test -f public/logo.png || python3 -c "\
import base64; \
open('public/logo.png','wb').write(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='))"

# Data persists via volume mount
VOLUME /app/data

EXPOSE 8001

RUN useradd --create-home --shell /bin/bash localmap \
    && mkdir -p /app/data \
    && chown -R localmap:localmap /app
USER localmap

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/', timeout=3)" || exit 1

CMD ["python3", "server.py"]
