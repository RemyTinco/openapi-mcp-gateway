SECRET_KEY = "fixture-only-not-a-secret"
DEBUG = False
ALLOWED_HOSTS: list[str] = []
ROOT_URLCONF = "urls"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "orders_api",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PAGINATION_CLASS": "orders_api.pagination.OrderPagination",
    "PAGE_SIZE": 20,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Orders API",
    "DESCRIPTION": "Fixture API used to test the OpenAPI MCP Gateway.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

import os  # noqa: E402

SPECTACULAR_SETTINGS["OAS_VERSION"] = os.environ.get("OAS_VERSION", "3.0.3")
