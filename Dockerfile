# jobautomat — servicio de búsqueda de empleo (Python stdlib + patchright/Chromium).
#
# No incluye ni descarga el MCP server de LinkedIn ni uvx: el buscador
# (service/search/linkedin.py) habla con LinkedIn mediante patchright +
# Chromium headless.
#
# Build:  docker build -t jobautomat:latest .
# Run:    docker compose up -d --build

FROM python:3.12-slim-bookworm

# Entorno base. Sin comentarios dentro del bloque: Docker no los admite en
# una instrucción con continuación de línea.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/service \
    TZ=Europe/Madrid \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    PATCHRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    JOB_ENV_FILE=/run/jobautomat/.env \
    DATA_DIR=/data \
    DB_PATH=/data/state.db \
    PERFIL_PATH=/data/perfil.json \
    LINKEDIN_SESSION_PATH=/data/linkedin_session.json \
    CRON_HOUR=2 \
    CRON_MINUTE=0 \
    RUN_ON_START=false

# Utilidades del sistema. `patchright install --with-deps chromium` instala
# después, vía apt, las librerías que Chromium necesita en Debian slim
# (libnss3, libatk, fuentes...) y descarga el binario a /opt/pw-browsers.
# Todo ocurre en build: en runtime no se descarga nada.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        tzdata \
        ca-certificates \
        curl \
        procps \
    && pip install --no-cache-dir "patchright>=1.49,<2" \
    && patchright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# Usuario sin privilegios (uid fijo 10001: el host debe poder hacer
# `chown -R 10001:10001 data`).
RUN useradd --create-home --uid 10001 --shell /bin/bash app \
    && mkdir -p /data /ms-playwright /app/service /app/docker \
    && chown -R app:app /data /ms-playwright /app

# Código del servicio y scripts de arranque.
COPY --chown=app:app service/ /app/service/
COPY --chown=app:app docker/ /app/docker/
# assets/cv/*.md y assets/cover-letters/ son datos de entrada de generate.py.
COPY --chown=app:app assets/ /app/assets/

# Red de seguridad: cualquier ruta "../data/..." resuelta desde /app/service
# (perfil, sesión de LinkedIn) apunta al volumen /data.
RUN ln -s /data /app/data

# Dependencias Python del servicio (patchright + jobspy + reportlab).
# La imagen no descarga nada del MCP server ni ejecuta uvx.
RUN pip install --no-cache-dir -r /app/service/requirements.txt \
    && chmod +x /app/docker/*.sh

WORKDIR /app/service
USER app

# tini como PID 1: recoge zombies y propaga SIGTERM al entrypoint.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
# Sin argumentos arranca el scheduler nocturno (CRON_HOUR, por defecto 02:00).
CMD ["scheduler"]
