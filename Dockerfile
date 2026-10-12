# syntax=docker/dockerfile:1

# Container image for the Django web app (web/), sized for Google Cloud Run.
#
# The app is stateless -- settings.py sets DATABASES = {} and holds no
# server-side player data (each browser keeps its own pasted state in
# localStorage), so there is nothing to mount, migrate or back up: the image is
# the whole deployment. See README.md's "Deploying it" for the plain-gunicorn
# recipe this packages, and deploy/cloudrun.sh for the Cloud Run wiring.

# ---------------------------------------------------------------- builder ---
FROM python:3.14-slim AS builder

# Resolve from the checked-in uv.lock so the image gets byte-identical versions
# to a local `uv sync`. Pinned to the uv that produced the lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies only, in their own cached layer. pyproject.toml has no
# [build-system], so this is a *virtual* project: `uv sync` installs the
# dependency tree and never the project itself -- which is why the source is
# copied in separately below, and why farmweb/wsgi.py puts the repo root on
# sys.path by hand instead of importing an installed package.
# --no-dev drops the dev group (pytest/ipython); --group web adds
# django/gunicorn/whitenoise/markdown.
#
# Deliberately no `--mount=type=cache` for uv's download cache: Cloud Build's
# default builder (gcr.io/cloud-builders/docker) is the legacy engine, which
# rejects mounts as BuildKit-only -- and it gives every build a fresh VM, so a
# cache mount would always start empty there anyway. Layer caching on
# pyproject.toml + uv.lock is what actually makes rebuilds cheap.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group web

# ---------------------------------------------------------------- runtime ---
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=farmweb.settings \
    # settings.py defaults DEBUG to *true* for local use. Pin it off here so
    # the image is production-safe even if the runtime env forgets to: with
    # DEBUG on, views.index renders the "CLI command" popup and
    # webapi.capture_debug_state writes every visitor's pasted mastery and
    # inventory text to the container's temp dir.
    DJANGO_DEBUG=false \
    # Django reads a leading dot as a subdomain wildcard, so this matches the
    # service's own *.run.app URL without a chicken-and-egg first deploy.
    # deploy/cloudrun.sh narrows it to the exact hostname once that URL exists.
    DJANGO_ALLOWED_HOSTS=.run.app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# The solver reads its game data from files sitting next to main.py, resolved
# at import time via Path(__file__).with_name(...): crops.csv, exploring.csv,
# fishing.csv, pets.csv, seed_drops.csv, openables.csv, sugar_cane_mill.csv, tower.csv,
# unmodelled.csv, items.csv, workshop.txt, data/mastery_items.txt -- and webapi.py reads
# web_default_config.toml (WEB_CONFIG_PATH) the same way. So the root layer
# needs the data files, not just the modules. data/ also holds the goal
# pickers' item catalogues, which collectstatic picks up via STATICFILES_DIRS.
COPY *.py *.csv *.txt *.toml ./
COPY data/ ./data/
COPY deploy/ ./deploy/
COPY web/ ./web/

# Fail the *build* rather than the first request if the HiGHS wheel is unusable
# on this architecture: solverapp.apps.SolverappConfig.ready() refuses to start
# the app without it, and PuLP's CBC fallback (used by optimizer._assert_optimal
# when HiGHS mis-flags a status) has no bundled binary on arm64.
RUN python -c "import optimizer; optimizer.set_solver_backend('highs')"

# Bake the static files in so whitenoise serves app.js/style.css straight out of
# web/staticfiles/ (STATIC_ROOT) with none of Django's per-request staticfiles
# machinery, then let Django's own checks fail the build on a broken setting.
RUN python web/manage.py collectstatic --noinput \
 && python web/manage.py check

# Precompile to .pyc now: main.py, optimizer.py and report.py are ~435 KB of
# source between them, and a scale-to-zero service pays that compile on every
# cold start otherwise.
RUN python -m compileall -q /app

RUN useradd --system --create-home --uid 1001 app && chown -R app:app /app
USER app

# Cloud Run injects $PORT; deploy/gunicorn.conf.py reads it, defaulting to 8080.
EXPOSE 8080
CMD ["gunicorn", "--chdir", "web", "-c", "/app/deploy/gunicorn.conf.py", \
     "farmweb.wsgi:application"]
