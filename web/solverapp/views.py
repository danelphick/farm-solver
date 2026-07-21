"""HTTP views: the index page and the JSON solve API.

All solving logic lives in :mod:`webapi` (framework-free, plain-pytest
tested); this module is purely the HTTP <-> webapi bridge -- decoding the
request, mapping :mod:`webapi`'s exceptions to status codes, rendering each
markdown section to HTML, and shaping the JSON response.
"""

from __future__ import annotations

import json
import time

import markdown
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

import webapi

from .cache import CACHE, SOLVE_SEMAPHORE, key_lock

# Solve-semaphore acquire timeout: long enough to queue behind one in-flight
# solve (budget ~1-3s per ARCHITECTURE.md's server-hardware note) without
# hanging a request indefinitely if something is stuck.
_SEMAPHORE_TIMEOUT_SECONDS = 20


def index(request: HttpRequest) -> HttpResponse:
    # Bundled config.toml values, so the preference panels can show each as a
    # placeholder ("blank = use default"). Rendered into a <script> tag via
    # Django's json_script (DjangoJSONEncoder serialises any date to ISO).
    # `debug` gates the CLI-command debug button (see api_solve's debug_command).
    return render(
        request,
        "solverapp/index.html",
        {"config_defaults": webapi.config_defaults(), "debug": settings.DEBUG},
    )


def _section_html(text: str) -> str:
    """Render one SectionedReporter markdown section to HTML."""
    return markdown.markdown(text, extensions=["tables"])


def _debug_command(args, mastery_text: str, inventory_text: str) -> str | None:
    """The debug popup's CLI command, or None outside DEBUG.

    In DEBUG we capture the pasted mastery/inventory to a local temp directory
    (webapi.capture_debug_state) and point the command's --mastery-file/
    --inventory-file at them, so running it reproduces the exact browser solve.
    """
    if not settings.DEBUG:
        return None
    mastery_file, inventory_file = webapi.capture_debug_state(
        mastery_text, inventory_text
    )
    return webapi.args_to_command(
        args, mastery_file=mastery_file, inventory_file=inventory_file
    )


@csrf_exempt  # a JSON POST API with no session/cookie auth -- nothing to forge
@require_POST
def api_solve(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "malformed JSON body"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "request body must be a JSON object"}, status=400)

    mastery_text = payload.get("mastery_text", "")
    inventory_text = payload.get("inventory_text", "")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return JsonResponse({"error": "params must be a JSON object"}, status=400)

    try:
        args = webapi.params_to_args(params)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    key = webapi.fingerprint(mastery_text, inventory_text, args)
    started = time.monotonic()

    with key_lock(key):
        session = CACHE.get(key)
        cached = session is not None
        if not SOLVE_SEMAPHORE.acquire(timeout=_SEMAPHORE_TIMEOUT_SECONDS):
            return JsonResponse({"error": "solver busy, try again"}, status=503)
        try:
            if session is None:
                session = webapi.solve(mastery_text, inventory_text, args)
            result, session = webapi.render(session, args)
        except ValueError as exc:
            # Only reachable from webapi.solve's parsing of mastery_text/
            # inventory_text (params were already validated above), so this
            # is always an unparseable paste.
            return JsonResponse({"error": str(exc)}, status=422)
        finally:
            SOLVE_SEMAPHORE.release()
        CACHE.put(key, session)

    solved_ms = round((time.monotonic() - started) * 1000)

    return JsonResponse({
        "session": key,
        "cached": cached and not result.resolved,
        "solved_ms": solved_ms,
        # The tower's top level, for the UI's "Tower (highest reachable)" label
        # (constant -- not the level a specific --level solve targeted).
        "tower_max_level": result.tower_max_level,
        "solution_html": _section_html(result.sections["solution"]),
        "notes": result.notes,
        "warnings": result.warnings,
        "frontier": {
            "html": _section_html(result.sections["frontier"]),
            "plans": result.frontier_rows,
        },
        # The impact graph (--graph's web counterpart): a "graph" section only
        # exists in SectionedReporter.docs when the request asked for one (see
        # report.SectionedReporter.frontier); absent -> no graph was requested,
        # so the client gets an empty html string and leaves #graph-section as-is.
        # It is already a self-contained <svg> fragment -- served raw, NOT through
        # the Markdown converter, which would wrap the opening tag as `<p><svg></p>`
        # and break the graphic (leaving only its stray text nodes visible).
        "graph": {
            "html": (
                result.sections["graph"]
                if result.sections.get("graph", "").strip()
                else ""
            ),
        },
        "plan": {
            "index": result.selected,
            "html": _section_html(result.sections["plan"]),
        },
        # Rendered into its own collapsible pane after the plan; empty when
        # the request didn't ask for the breakdown.
        "crafts": {
            "html": (
                _section_html(result.sections["crafts"])
                if result.sections.get("crafts", "").strip()
                else ""
            ),
        },
        # DEBUG only: the equivalent `python main.py` command, for the debug
        # button's popup. None in production so the button never has content.
        # The pasted mastery/inventory are dumped to a local temp directory so
        # the command reproduces the exact solve (see webapi.capture_debug_state).
        "debug_command": _debug_command(args, mastery_text, inventory_text),
    })
