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

    def test_impact_graph_is_served_as_raw_svg(self):
        # The graph section is an <svg> fragment; it must be served raw, not run
        # through the Markdown converter (which wraps the opening tag as
        # `<p><svg></p>` and breaks the graphic -- only its text nodes then show).
        response = self._post(
            {"items": "Board:50000", "plans": 5, "spread": "time", "graph": "auto"}
        )
        self.assertEqual(response.status_code, 200)
        html = response.json()["graph"]["html"].strip()
        self.assertTrue(html.startswith("<svg"), html[:60])
        self.assertIn("</svg>", html)
        self.assertIn('class="pt"', html)
        self.assertNotIn("<p>", html)  # not Markdown-wrapped

    def test_no_graph_section_when_not_requested(self):
        response = self._post({"items": "Board:50000", "plans": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["graph"]["html"], "")

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


class ApiMasteryCostsTests(SimpleTestCase):
    # No `databases` attribute: SimpleTestCase already defaults to none, and
    # this project has no database at all (see farmweb/settings.py).

    def setUp(self) -> None:
        _entries.clear()
        _key_locks.clear()

    def _post(self, params: dict, mastery: str = MASTERY_TEXT, inventory: str = INVENTORY_TEXT):
        return self.client.post(
            "/api/mastery-costs",
            data=json.dumps({
                "mastery_text": mastery,
                "inventory_text": inventory,
                "params": params,
            }),
            content_type="application/json",
        )

    def test_returns_ranked_rows_and_unmasterable_chains(self):
        response = self._post({})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["cached"])
        self.assertTrue(body["rows"])
        row = body["rows"][0]
        self.assertEqual(
            set(row), {"item", "tier", "time", "time_seconds", "gold"}
        )
        self.assertIn(row["tier"], {"M", "GM", "MM"})
        self.assertIsInstance(body["unmasterable"], list)

    def test_identical_repost_is_a_cache_hit(self):
        self.assertFalse(self._post({}).json()["cached"])
        second = self._post({})
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["cached"])

    def test_a_goal_only_change_is_still_a_cache_hit(self):
        # The goal fields never reach solve_mastery_costs, so they are not part
        # of this endpoint's key (webapi.mastery_costs_fingerprint): editing the
        # solver page's goal must not throw the table away.
        self._post({})
        response = self._post({"items": "Board:50000", "target": "150"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["cached"])

    def test_self_sourced_change_is_a_cache_miss(self):
        self._post({})
        response = self._post({"self_sourced": True})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cached"])

    def test_a_solve_session_key_is_never_served_here(self):
        # Both endpoints share one cache; the "mc:" key prefix keeps them apart.
        self.client.post(
            "/api/solve",
            data=json.dumps({
                "mastery_text": MASTERY_TEXT,
                "inventory_text": INVENTORY_TEXT,
                "params": {"items": "Board:50000", "plans": 2},
            }),
            content_type="application/json",
        )
        self.assertEqual([key.startswith("mc:") for key in _entries], [False])
        # ... so the mastery-costs request still misses, and lands beside it.
        self.assertFalse(self._post({}).json()["cached"])
        self.assertEqual(
            sorted(key.startswith("mc:") for key in _entries), [False, True]
        )

    def test_garbage_paste_is_422(self):
        response = self._post({}, mastery="not a valid paste at all", inventory="")
        self.assertEqual(response.status_code, 422)
        self.assertIn("error", response.json())

    def test_bad_param_is_400(self):
        response = self._post({"bogus_field": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("bogus_field", response.json()["error"])

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get("/api/mastery-costs").status_code, 405)

    def test_masteries_page_renders(self):
        response = self.client.get("/masteries")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "masteries.js")


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
