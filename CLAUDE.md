# CLAUDE.md

**Read [ARCHITECTURE.md](ARCHITECTURE.md) first** — it's the code map (module
ownership, key functions by name, core types, and the critical-path / frontier
decision logic). Use it to find code instead of re-reading files; the README is
user-facing behaviour/config.

**Keep the map current:** after a commit that changes structure — a new/renamed
module or public function, a change to the LP/frontier or critical-path logic, a
new data type or invariant — update ARCHITECTURE.md in the same commit. Reference
things by name, not line number.

## Workflow

- Tests: `uv run pytest -q`. Lint: `uvx ruff check .`. Both must pass before commit.
- Run it: `python main.py` (`-i Item:N`, `-m Item:N`, `--plan K`, `--plans N`).
- Commit directly to `main`; end commit messages with the Co-Authored-By trailer.
