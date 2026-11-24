# The server as Cloud Run runs it. Built by `./deploy/cloud.sh deploy` (Cloud Build), so
# Docker does not need to be installed on the Mac.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# Only the shared rules and the pipeline; see .gcloudignore for what is uploaded
COPY src/ src/
COPY server/ server/

RUN useradd --create-home app && chown -R app /app
USER app

# A bank sync runs the pipeline in a subprocess of this process, so one worker is right
CMD exec uvicorn server.app:create_app --factory --host 0.0.0.0 --port "$PORT" --workers 1
