# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Workflow - Critical Rules

**Important guidelines when working with this codebase:**

1. **Never swallow errors** - Do not write `except Exception: pass` or any bare exception handler that silences errors. If an exception can occur, let it propagate so it appears in logs and surfaces as a proper error response. Silent failure hides bugs and makes debugging extremely difficult (as demonstrated by the billing hook returning the wrong type and being invisible for an extended period).

2. **Plan before implementing** - Every non-trivial change requires a written plan first. The full workflow is:
   1. **Create a plan file** in `docs/plans/` (e.g., `docs/plans/my-feature.md`) before writing any code. Read existing plans for the expected structure.
   2. **Discuss all design decisions** with the user before finalising the plan. Claude suggests options with trade-offs; the user decides. Never take design or architecture decisions unilaterally.
   3. **Wait for the user to commit the plan** to git. The plan must be in the repository before implementation begins.
   4. **Implement in a separate session** — implementation only starts after the committed plan exists.
   5. **Move the plan to `docs/plans/done/`** when implementation is complete. The user commits the code changes and the plan move together in the same commit.

3. **DO NOT commit to git** - Never create git commits or push changes. The user will handle version control.

