"""A faster PuLP -> HiGHS hand-over: the solver backend :mod:`optimizer` uses.

PuLP's own HiGHS backend rebuilds the whole HiGHS model in Python on every
solve: it walks every constraint three times to collect the variables (sorting
them by name each time), then adds the columns and rows one call at a time, and
reads every value, dual and slack back the same way. On this project's small
LPs that hand-over cost more than the solving -- and they are solved hundreds of
times a run, the same LP (an ``optimizer._Base``) many times over with only its
objective, a few pinned rows and a time cap changing between solves.

:class:`FastHiGHS` gives HiGHS exactly the model PuLP's backend would -- columns
in PuLP's order (sorted by name), rows in constraint order with each row's
nonzero entries in order, the same bounds, costs, integrality and options, a
fresh ``Highs`` per solve -- so every solve, and every plan, is identical. It
just builds it faster: the rows are converted once and cached on the problem,
so a re-solve only converts the constraints added since the last solve (the
shared prefix is reused), and the whole model goes over in one ``passModel``.
Only the primal values are read back (nothing reads the duals or slacks).

**Warm starts** (:data:`WARM_START`): an LP solved right after another on the
same problem, differing only in its objective, column bounds and *added* rows --
``optimizer._solve_configured``'s refinement stages, which re-optimise with the
previous stage pinned -- continues on the previous solve's ``Highs`` (its basis
still loaded) instead of starting cold. The first solve of every call is cold
(the previous call's pins have been removed by then), as is any MIP, any solve
after a non-optimal one, and any change of options. A warm re-optimisation lands
on the same optimum; at most the last few digits of a value can differ.
"""

from __future__ import annotations

import highspy
import pulp
from pulp import constants

_INF = highspy.kHighsInf

# Continue an LP from the previous solve's basis where it can (see above).
WARM_START = True

# PuLP's HighsModelStatus -> (LpStatus, LpSolution) table (pulp.HiGHS's
# findSolutionValues), so a solve reports exactly what PuLP's backend would.
_STATUS = {
    highspy.HighsModelStatus.kOptimal: (
        constants.LpStatusOptimal, constants.LpSolutionOptimal,
    ),
    highspy.HighsModelStatus.kInfeasible: (
        constants.LpStatusInfeasible, constants.LpSolutionInfeasible,
    ),
    highspy.HighsModelStatus.kUnboundedOrInfeasible: (
        constants.LpStatusInfeasible, constants.LpSolutionInfeasible,
    ),
    highspy.HighsModelStatus.kUnbounded: (
        constants.LpStatusUnbounded, constants.LpSolutionUnbounded,
    ),
    **{
        status: (constants.LpStatusOptimal, constants.LpSolutionIntegerFeasible)
        for status in (
            highspy.HighsModelStatus.kObjectiveBound,
            highspy.HighsModelStatus.kObjectiveTarget,
            highspy.HighsModelStatus.kInterrupt,
            highspy.HighsModelStatus.kTimeLimit,
            highspy.HighsModelStatus.kIterationLimit,
        )
    },
}
_NOT_SOLVED = (constants.LpStatusNotSolved, constants.LpSolutionNoSolutionFound)
_LIMITS = (highspy.HighsModelStatus.kTimeLimit, highspy.HighsModelStatus.kIterationLimit)


class _Model:
    """The HiGHS form of one ``LpProblem``, cached on it between solves.

    ``columns`` is PuLP's column order (every registered variable, sorted by
    name) and ``index`` maps a variable to its column. ``rows`` are the
    constraint objects already converted, in order, as a row-wise matrix
    (``starts``/``indices``/``values``) with bounds (``lower``/``upper``).
    """

    __slots__ = (
        "columns",
        "index",
        "indices",
        "kept",
        "lower",
        "registered",
        "rows",
        "starts",
        "upper",
        "values",
    )

    def __init__(self, problem: pulp.LpProblem) -> None:
        self.registered = len(problem._variables)
        self.columns = sorted(problem._variables, key=lambda var: var.name)
        self.index = {var: i for i, var in enumerate(self.columns)}
        self.rows: list[pulp.LpConstraint] = []
        self.starts = [0]
        self.indices: list[int] = []
        self.values: list[float] = []
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.kept = 0  # rows the last sync left in place (the shared prefix)

    def sync_rows(self, constraints: list[pulp.LpConstraint]) -> None:
        """Bring the cached rows in line with ``constraints``: keep the prefix
        that is unchanged (same constraint objects, same order), convert the
        rest."""
        rows = self.rows
        shared = 0
        limit = min(len(rows), len(constraints))
        while shared < limit and rows[shared] is constraints[shared]:
            shared += 1
        self.kept = shared
        if shared < len(rows):
            end = self.starts[shared]
            del rows[shared:], self.starts[shared + 1:]
            del self.indices[end:], self.values[end:]
            del self.lower[shared:], self.upper[shared:]
        index, indices, values = self.index, self.indices, self.values
        for constraint in constraints[shared:]:
            for var, coef in constraint.items():
                if coef != 0:
                    indices.append(index[var])
                    values.append(coef)
            self.starts.append(len(indices))
            low, up = constraint.getLb(), constraint.getUb()
            self.lower.append(-_INF if low is None else low)
            self.upper.append(_INF if up is None else up)
            rows.append(constraint)


def _model(problem: pulp.LpProblem) -> _Model:
    """``problem``'s cached HiGHS form, brought up to date."""
    # Register the objective's variables, as LpProblem.variables() would (the
    # constraints' are registered as they are added).
    known = problem._variable_ids
    for var in problem.objective:
        if var.hash not in known:
            problem._register_variable(var)
    model = getattr(problem, "_highs_model", None)
    if model is None or model.registered != len(problem._variables):
        model = _Model(problem)  # a new column changes every index: start over
        problem._highs_model = model
    model.sync_rows(list(problem._constraints.values()))
    return model


class FastHiGHS(pulp.HiGHS):
    """PuLP's in-process HiGHS backend with a cached, one-call model hand-over
    and warm-started refinements (see the module docstring). A drop-in
    ``pulp.HiGHS``."""

    def actualSolve(self, lp, callback=None):
        model = _model(lp)
        columns = model.columns
        integer = self.mip and any(v.cat == constants.LpInteger for v in columns)
        cost = _costs(lp, model)
        warm = getattr(lp, "_highs_warm", None)
        lp._highs_warm = None
        if (
            WARM_START and not integer and warm is not None
            and warm[1] is model and model.kept >= warm[2]
            and warm[3] == self.optionsDict
        ):
            highs = warm[0]
            _extend(highs, model, warm[2], cost)
            lp.solverModel = highs
        else:
            self.createAndConfigureSolver(lp)
            highs = lp.solverModel
            highs.passModel(_whole(model, cost, integer))
        self.callSolver(lp)

        status = highs.getModelStatus()
        values = highs.getSolution().col_value
        for i, var in enumerate(columns):
            var.varValue = values[i]
        if highs.getObjectiveValue() == float("inf") and status in _LIMITS:
            outcome = _NOT_SOLVED
        else:
            outcome = _STATUS.get(status, _NOT_SOLVED)
        lp.assignStatus(*outcome)
        if WARM_START and not integer and status == highspy.HighsModelStatus.kOptimal:
            lp._highs_warm = (highs, model, len(model.rows), dict(self.optionsDict))
        return outcome[0]


def _costs(problem: pulp.LpProblem, model: _Model) -> list[float]:
    """The objective as a cost per column (PuLP folds a maximisation into the
    costs, and so negates a zero too)."""
    sign = -1 if problem.sense == constants.LpMaximize else 1
    cost = [sign * 0.0] * len(model.columns)
    index = model.index
    for var, coef in problem.objective.items():
        cost[index[var]] = sign * coef
    return cost


def _bounds(model: _Model) -> tuple[list[float], list[float]]:
    columns = model.columns
    return (
        [-_INF if v.lowBound is None else v.lowBound for v in columns],
        [_INF if v.upBound is None else v.upBound for v in columns],
    )


def _whole(model: _Model, cost: list[float], integer: bool) -> highspy.HighsLp:
    """The whole model, for a cold solve."""
    columns = model.columns
    n = len(columns)
    hlp = highspy.HighsLp()
    hlp.num_col_ = n
    hlp.num_row_ = len(model.rows)
    hlp.col_cost_ = cost
    hlp.col_lower_, hlp.col_upper_ = _bounds(model)
    hlp.row_lower_ = model.lower
    hlp.row_upper_ = model.upper
    matrix = hlp.a_matrix_
    matrix.format_ = highspy.MatrixFormat.kRowwise
    matrix.num_col_ = n
    matrix.num_row_ = len(model.rows)
    matrix.start_ = model.starts
    matrix.index_ = model.indices
    matrix.value_ = model.values
    hlp.a_matrix_ = matrix
    if integer:
        hlp.integrality_ = [
            highspy.HighsVarType.kInteger
            if v.cat == constants.LpInteger
            else highspy.HighsVarType.kContinuous
            for v in columns
        ]
    return hlp


def _extend(
    highs: highspy.Highs, model: _Model, loaded: int, cost: list[float]
) -> None:
    """Bring a warm ``highs`` -- holding ``model``'s first ``loaded`` rows from the
    previous solve -- up to date: the rows added since, the objective, the column
    bounds. Its basis stays loaded for the next run."""
    n = len(model.columns)
    added = len(model.rows) - loaded
    if added:
        first = model.starts[loaded]
        highs.addRows(
            added, model.lower[loaded:], model.upper[loaded:],
            len(model.indices) - first,
            [start - first for start in model.starts[loaded:-1]],
            model.indices[first:], model.values[first:],
        )
    every = list(range(n))
    highs.changeColsCost(n, every, cost)
    lower, upper = _bounds(model)
    highs.changeColsBounds(n, every, lower, upper)
