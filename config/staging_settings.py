import os

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import DomainNameValidator

from .settings import *


def _staging_host(name):
    value = os.environ.get(name, "").lower()
    try:
        DomainNameValidator(accept_idna=False)(value)
    except ValidationError:
        raise ImproperlyConfigured(f"{name} requires an exact DNS name.") from None
    if value.endswith("."):
        raise ImproperlyConfigured(f"{name} requires a DNS name without a trailing dot.")
    return value


ERP_INTERNAL_HOSTS = (_staging_host("ERP_STAGING_INTERNAL_HOST"),)
ERP_OWNER_HOSTS = (_staging_host("ERP_STAGING_OWNER_HOST"),)
if ERP_INTERNAL_HOSTS == ERP_OWNER_HOSTS:
    raise ImproperlyConfigured("Staging staff and owner hosts must be distinct.")

ALLOWED_HOSTS = [*ERP_INTERNAL_HOSTS, *ERP_OWNER_HOSTS]
USE_X_FORWARDED_HOST = False
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
CSRF_TRUSTED_ORIGINS = []
