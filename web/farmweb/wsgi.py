"""WSGI config for the farm-solver web app.

Used by gunicorn in production (``gunicorn farmweb.wsgi:application``) and by
Django's own dev server indirectly. Unlike ``manage.py``, a WSGI server
imports this module directly -- never through ``manage.py`` -- so the repo
root (for ``main``/``webapi``/``optimizer``/...) has to go on ``sys.path``
here too.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "farmweb.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
