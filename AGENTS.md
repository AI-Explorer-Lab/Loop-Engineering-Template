# Repository instructions for AI agents

## Mission

Work from a completed `template/initial-loop-request.md`. Treat its goal,
acceptance criteria, authority, limits, and stop conditions as the contract for
the run.

## Autonomous loop

After receiving a completed request:

1. Inspect the repository and relevant instructions.
2. Restate the operational goal internally and identify objective checks.
3. Create a small execution plan and keep it current.
4. Implement the smallest coherent change.
5. Run the most relevant verification.
6. Diagnose failures from evidence, fix them, and verify again.
7. Audit every acceptance criterion against current files and test output.
8. Finish only when all criteria are proven or a declared stop condition is met.

Do not ask the human to choose routine implementation details when the request
authorizes reasonable judgment. Record important decisions in the final
handoff. Do ask only when a missing decision would materially change the goal
or require authority not granted in the request.

## Non-negotiable rules

- Stay inside the granted file, command, network, data, and external-system scope.
- Never weaken tests, checks, security controls, or acceptance criteria to pass.
- Never claim success from an AI summary alone; use file and command evidence.
- Do not expose secrets in code, prompts, logs, test output, or documentation.
- Do not perform production deployment, destructive data operations, permission
  expansion, external messages, commits, or pushes unless explicitly authorized.
- Stop at the request's time, cost, iteration, or retry limits.
- Preserve unrelated user changes.

## Repository boundaries

- Backend business flow: Controller → Service → Domain/Mapper.
- Controllers do not access the database directly.
- Services own business validation and transaction boundaries.
- Mappers only persist and translate data.
- FastAPI is the API framework; requests and responses use Pydantic schemas.
- Shared and production database changes use Alembic migrations.
- Add phase-two or phase-three directories only when implementing their first
  real capability; do not create empty architecture for appearance.

## Required final handoff

Report:

- outcome;
- files changed;
- verification executed and results;
- acceptance-criteria audit;
- important decisions and assumptions;
- remaining risks or blockers.
