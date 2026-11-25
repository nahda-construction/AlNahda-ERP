FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

RUN useradd --system --uid 10001 --no-create-home erp
COPY manage.py ./
COPY config/ ./config/
COPY tests/ ./tests/

USER erp
CMD ["gunicorn", "--no-control-socket", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "config.wsgi:application"]
