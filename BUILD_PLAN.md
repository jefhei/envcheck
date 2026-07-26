# Build Plan: envcheck

## M1 — Core CLI + Scan (3 days)

| Task | Description | Depends On |
|------|-------------|------------|
| M1.1 | Project scaffolding: `pyproject.toml` (uv), Typer CLI entrypoint, `src/envcheck/` package structure | — |
| M1.2 | `.envcheck.yaml` schema design + config loader with validation (Pydantic or manual) | M1.1 |
| M1.3 | `.env` file scanner — parse `.env`, `.env.*`, `.env.example` files, extract key-value pairs | M1.2 |
| M1.4 | Docker config scanner — extract env blocks from `docker-compose.yml` and `Dockerfile` (ARG/ENV) | M1.2 |
| M1.5 | CI config scanner — parse GitHub Actions workflow YAML for `env:` blocks and secrets references | M1.2 |
| M1.6 | Basic environment profile builder — aggregate scanner results into `EnvironmentProfile` model | M1.3, M1.4, M1.5 |

## M2 — Diff Engine + Report (2 days)

| Task | Description | Depends On |
|------|-------------|------------|
| M2.1 | Var diff engine — compare env vars across profiles: missing, extra, value-changed, type-changed | M1.6 |
| M2.2 | Service version diff — detect version mismatches in Docker images (e.g. `postgres:16` vs `postgres:15`) | M1.4 |
| M2.3 | Rich terminal reporter — color-coded table (green=ok, yellow=diff, red=missing, gray=extra) | M2.1, M2.2 |
| M2.4 | JSON output mode — `--json` flag for CI/machine consumption | M2.3 |
| M2.5 | Exit codes — `0` = clean, `1` = drift detected, `2` = error; with `--strict` mode for CI | M2.4 |

## M3 — CI Integration (2 days)

| Task | Description | Depends On |
|------|-------------|------------|
| M3.1 | `envcheck init` — interactive command that auto-discovers `.env*` files and generates `.envcheck.yaml` | M1.2 |
| M3.2 | Pre-commit hook generator — `envcheck init --pre-commit` adds a hook script that runs on staged changes | M2.5 |
| M3.3 | GitHub Action — reusable action definition in `.github/actions/envcheck/action.yml` | M2.5 |
| M3.4 | Clean error handling — network timeouts, malformed configs, partial scan failures, graceful degradation | M3.1 |

## M4 — Polish & Docs (1 day)

| Task | Description | Depends On |
|------|-------------|------------|
| M4.1 | README with quickstart, config reference, example `.envcheck.yaml`, and screenshot of terminal output | M3.4 |
| M4.2 | Test suite — unit tests for scanners, diff engine, config loader; integration tests with fixture projects | M3.4 |
| M4.3 | Package & publish to PyPI — `pip install envcheck` with `uv build` + `uv publish` | M4.2 |

---

**Total: 18 tasks across 4 milestones, ~8 days for a solo developer.**
**Difficulty:** Beginner
