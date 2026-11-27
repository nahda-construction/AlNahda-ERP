FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-noto-core libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

RUN useradd --system --uid 10001 --no-create-home erp
RUN install -d -m 0700 -o erp -g erp /var/lib/erp/private /var/lib/erp/print
ARG ERP_PRINT_LOGO_SHA256
RUN --mount=type=bind,source=.,target=/build \
    --mount=type=secret,id=erp_print_logo \
    cp -R /build/manage.py /build/config /build/apps /build/tests /app/ \
    && if [ -n "$ERP_PRINT_LOGO_SHA256" ]; then \
        [ "${#ERP_PRINT_LOGO_SHA256}" -eq 64 ] \
        && printf '%s\n' "$ERP_PRINT_LOGO_SHA256" | grep -Eq '^[a-f0-9]{64}$' \
        && printf '%s  %s\n' "$ERP_PRINT_LOGO_SHA256" /run/secrets/erp_print_logo | sha256sum -c - \
        && install -m 0444 /run/secrets/erp_print_logo /app/apps/printing/assets/logo.png; \
    elif [ -e /run/secrets/erp_print_logo ]; then \
        echo 'Set ERP_PRINT_LOGO_SHA256 when supplying erp_print_logo.' >&2; exit 1; \
    fi \
    && chmod -R a+rX /app

USER erp
CMD ["gunicorn", "--no-control-socket", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "config.wsgi:application"]
