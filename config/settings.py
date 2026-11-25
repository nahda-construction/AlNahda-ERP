import os

from psycopg import IsolationLevel

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "apps.projects.apps.ProjectsConfig",
    "apps.contracts.apps.ContractsConfig",
    "apps.access_control.apps.AccessControlConfig",
    "apps.test_transactions.apps.TestTransactionsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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
