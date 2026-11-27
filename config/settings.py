import os

from django.core.exceptions import ImproperlyConfigured
from psycopg import IsolationLevel

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False


def host_names(name, default):
    hosts = tuple(
        host.strip().lower().rstrip(".")
        for host in os.environ.get(name, default).split(",") if host.strip()
    )
    if any(not host or host == "*" or host.startswith(".") for host in hosts):
        raise ImproperlyConfigured(f"{name} requires exact host names.")
    return hosts


ERP_INTERNAL_HOSTS = host_names("ERP_INTERNAL_HOSTS", "localhost,127.0.0.1")
ERP_OWNER_HOSTS = host_names("ERP_OWNER_HOSTS", "owner.localhost")
ALLOWED_HOSTS = list(dict.fromkeys((*ERP_INTERNAL_HOSTS, *ERP_OWNER_HOSTS)))
USE_X_FORWARDED_HOST = False
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
STATIC_URL = "/api/v1/docs/assets/"

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "ninja",
    "apps.projects.apps.ProjectsConfig",
    "apps.contracts.apps.ContractsConfig",
    "apps.access_control.apps.AccessControlConfig",
    "apps.test_transactions.apps.TestTransactionsConfig",
    "apps.inventory.apps.InventoryConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.host_routing.HostURLConfMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.test_transactions.views.TransactionRequestMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]
LOGIN_URL = "test_transactions:login"
LOGIN_REDIRECT_URL = "test_transactions:projects"
LOGOUT_REDIRECT_URL = "test_transactions:login"
ERP_PRIVATE_ROOT = os.environ.get("ERP_PRIVATE_ROOT")
FILE_UPLOAD_HANDLERS = ["apps.test_transactions.storage.RejectFileUploadHandler"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ["POSTGRES_HOST"],
        "PORT": "5432",
        "NAME": os.environ["POSTGRES_DB"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "OPTIONS": {
            "connect_timeout": 3,
            "isolation_level": IsolationLevel.READ_COMMITTED,
        },
    }
}

LANGUAGE_CODE = "ar"
TIME_ZONE = "Africa/Cairo"
USE_TZ = True
