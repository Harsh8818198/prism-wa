# One image, two processes: the gateway (default) and the worker.
#   gateway:  docker run -p 8090:8090 --env-file .env prism-wa            (listens on $PORT, 8090 if unset; Render sets $PORT)
#   worker:   docker run --env-file .env prism-wa python -m prism_wa.worker
#   migrate:  docker run --env-file .env prism-wa python db/apply.py --pending     (Render runs this before each deploy goes live)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY prism_wa ./prism_wa
COPY db ./db

RUN useradd --system --no-create-home prismwa
USER prismwa

EXPOSE 8090
# exec: uvicorn becomes PID 1 and gets SIGTERM directly, so a deploy lets in-flight requests finish. The proxy flags make request logs and
# client addresses the real ones behind the host's load balancer.
CMD ["sh", "-c", "exec uvicorn prism_wa.main:create_app_from_env --factory --host 0.0.0.0 --port ${PORT:-8090} --proxy-headers --forwarded-allow-ips='*' --no-server-header --timeout-keep-alive 65 --timeout-graceful-shutdown 25"]
