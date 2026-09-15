"""Django settings for the farm-solver web app.

Deliberately minimal: **no database, no accounts** (see the plan's design
notes -- browser localStorage holds each user's pasted state and parameter
choices, there is nothing server-side to persist per user), so
``INSTALLED_APPS``/``MIDDLEWARE`` skip ``django.contrib.auth``/``admin``/
``sessions``/``messages`` entirely. ``DEBUG``/``SECRET_KEY``/``ALLOWED_HOSTS``
are env-driven for deployment (see README.md's web section); the checked-in
defaults are dev-only and unsafe for production.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-insecure-secret-key-change-via-DJANGO_SECRET_KEY",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "true").strip().lower() in ("1", "true", "yes")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")
    if host.strip()
] or (["*"] if DEBUG else [])

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "solverapp.apps.SolverappConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "farmweb.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "farmweb.wsgi.application"

# No player data is stored server-side, so there is no database to configure.
DATABASES = {}

# No auth/sessions/i18n machinery, so these middleware-adjacent settings are
# left at Django's defaults except where a bare install would otherwise warn.
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# The item catalogues (mastery_items.txt, items.txt) live in the repo-root
# data/ directory, shared with the solver, and are served under the app's own
# static prefix -- /static/solverapp/items.txt -- which catalogue.js fetches.
STATICFILES_DIRS = [("solverapp", BASE_DIR.parent / "data")]
