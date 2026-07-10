from django.apps import AppConfig

import optimizer


class SolverappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "solverapp"

    def ready(self) -> None:
        # Pin the LP backend once, here, at process startup -- never per
        # request. optimizer._backend is a module global (see
        # ARCHITECTURE.md's "Dependency" section); setting it from a request
        # handler under gunicorn's threads would race. HiGHS solves
        # in-process (no ~8ms subprocess spawn per solve like the bundled CBC
        # fallback), which matters here: a re-solve runs the frontier's ~80
        # sequential LPs.
        try:
            optimizer.set_solver_backend("highs")
        except ValueError as exc:
            raise RuntimeError(
                "farm-solver web requires the HiGHS backend (see pyproject.toml's "
                "highspy dependency); refusing to start without it"
            ) from exc
