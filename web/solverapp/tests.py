"""Django test suite for the solve API.

Run via ``uv run --group web python web/manage.py test solverapp`` -- kept
here (not ``test_*.py``, and outside the repo-root ``tests/`` directory Django
never sees) so plain ``uv run pytest -q`` neither collects nor needs Django.
SimpleTestCase, not TestCase: there is no database to wrap tests in a
transaction for (see farmweb/settings.py's ``DATABASES = {}``).
"""

import json
import textwrap
from datetime import date

from django.test import SimpleTestCase

import main
import webapi
from crafting import Workshop
from crops import Farm
from exploring import Exploring
from farm_solver import GameState, Solution, TowerRequirements
from fishing import Fishing
from locksmith import Locksmith

from .cache import CACHE, _entries, _key_locks

# A small, self-contained paste fixture -- not mydata/ (real player data,
# gitignored is irrelevant here; the point is a Django test should not depend
# on a particular player's save). Mirrors tests/test_webapi.py's fixture.
MASTERY_TEXT = "Mastery In-ProgressCopy Mastery Data\nStop Tracking All\n"

INVENTORY_TEXT = textwrap.dedent(
    """\

    Items chevron_down

    Board
    The foundation of any good farm
    MAX ON HAND
    Mega Mastered
    49,420

    Iron
    A pressing need
    1,234
    """
)


class ApiSolveTests(SimpleTestCase):
    databases = set()  # no database in this project at all

    def setUp(self) -> None:
        # Every test gets a clean cache/lock table, so cache-hit assertions
        # aren't order-dependent on whatever an earlier test solved.
        _entries.clear()
        _key_locks.clear()

    def _post(self, params: dict, mastery: str = MASTERY_TEXT, inventory: str = INVENTORY_TEXT):
        return self.client.post(
            "/api/solve",
            data=json.dumps({
                "mastery_text": mastery,
                "inventory_text": inventory,
                "params": params,
            }),
            content_type="application/json",
        )

    def test_full_solve_returns_a_populated_frontier(self):
        response = self._post({"items": "Board:50000", "plans": 2})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["frontier"]["plans"])
        self.assertFalse(body["cached"])
        self.assertIn("solution_html", body)
        self.assertIn("Board", body["solution_html"])

    def test_identical_repost_is_a_cache_hit(self):
        params = {"items": "Board:50000", "plans": 2}
        first = self._post(params)
        self.assertFalse(first.json()["cached"])
        second = self._post(params)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["cached"])

    def test_max_gold_only_change_is_still_a_cache_hit(self):
        # max_gold is a REFILTER field: it changes which plans are shown, not
        # what gets solved, so it must not force a re-solve.
        self._post({"items": "Board:50000", "plans": 2})
        response = self._post({"items": "Board:50000", "plans": 2, "max_gold": "500"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["cached"])

    def test_current_stamina_change_is_a_cache_miss(self):
        # current_stamina is a RESOLVE field: it changes what gets solved.
        self._post({"items": "Board:50000", "plans": 2})
        response = self._post(
            {"items": "Board:50000", "plans": 2, "current_stamina": 500}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cached"])

    def test_garbage_paste_is_422(self):
        response = self._post(
            {"items": "Board:50000"}, mastery="not a valid paste at all", inventory=""
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())

    def test_bad_param_is_400_with_the_helpers_message(self):
        response = self._post({"target": "not-a-level"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("--target", response.json()["error"])

    def test_unknown_param_is_400(self):
        response = self._post({"bogus_field": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("bogus_field", response.json()["error"])

    def test_index_page_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_get_on_api_solve_is_not_allowed(self):
        response = self.client.get("/api/solve")
        self.assertEqual(response.status_code, 405)


class SessionCacheTests(SimpleTestCase):
    def setUp(self) -> None:
        _entries.clear()
        _key_locks.clear()

    def test_put_then_get_round_trips(self):
        inputs = main.SolveInputs(
            time_limit=None, state=GameState(), requirements=TowerRequirements(),
            farm=Farm(), explore=Exploring(), fish=Fishing(), workshop=Workshop(),
            monthly_grants=False, solution=Solution(), recipes={}, smith=Locksmith(),
            open_flows=[], demand={}, inventory={}, cookie_sets=0.0, prices=None,
            boost_offers=[],
            pj_gain={}, pj_price=0.0, pj_limit=None, model_calendar=False,
            start=date(2026, 1, 1),
        )
        fr = main.FrontierResult(
            plans=[], within=[], shown=[], offers=[], production=None,
            horizon=None,
        )
        ctx = main.PlanContextData(
            cheapest=0, display={}, max_passive_residuals={}, reasons=[],
            golds=None, sourceable_elsewhere=set(),
        )
        session = webapi.SolveSession(inputs=inputs, frontier=fr, ctx=ctx, cfg={})
        CACHE.put("k", session)
        self.assertIs(CACHE.get("k"), session)
        self.assertIsNone(CACHE.get("missing-key"))
