## Cassidy

In an initialized Cassidy checkout, before Cassidy planning or workflow work,
read and follow `.casd/AGENTS.md` and its applicable role guidance. This remains
mandatory in the current initialized repository. Missing required managed
guidance in an initialized checkout is a problem to report for explicit
`casd upgrade`, not permission to bypass Cassidy or invent replacement guidance.

Fresh source deliberately exported without the old `.casd/` planning graph has
no `.casd/AGENTS.md` to read until Cassidy is initialized. Cassidy workflow use
there requires separately authorized fresh initialization; do not copy private
old planning or Attempt evidence, or invent replacement managed guidance. The
fresh-history direction does not authorize removing or ignoring Cassidy in the
current initialized repository, exporting source, initializing a new repository,
or publishing remotely.

## Project development

The project development and helper instructions below apply with or without
Cassidy initialization and require no private planning history. They grant no
extra workflow, editing, or disposition authority.

Use the shared main-repository virtualenv at
`<main-repository>/.venv/bin/python` for project commands, including from a
disposable worktree. Use `python3` only as an explicit fallback when that
virtualenv is unavailable; do not assume a `python` shim exists.

## Project helpers

Prefer these fixed-purpose helpers over rebuilding their behavior with ad hoc
shell commands:

- `./scripts/check` runs formatting, lint, Django checks, and the parallel test
  suite without modifying source files.
- `./scripts/quality-check` runs fast, non-mutating Ruff lint and format
  verification plus Django system and migration-drift checks; use it rather
  than reconstructing those checks ad hoc.
- `./scripts/format` applies Ruff fixes and formatting; it modifies source
  files.
- `./scripts/start-dev` migrates and starts the development server from the
  canonical main worktree, binding to `127.0.0.1:8000` by default. Set
  `PRECEPTOR_BIND_ADDRESS` to override the bind address (port stays 8000).
  Nondefault access also needs an appropriate `PRECEPTOR_ALLOWED_HOSTS` list;
  the bind override does not change trusted hosts.
- `./scripts/start-preview WORKTREE` validates a worktree from this repository,
  copies the main database into isolated temporary storage, migrates that copy,
  and serves the worktree at `127.0.0.1:8001` until stopped. Set
  `PRECEPTOR_BIND_ADDRESS` to override the bind address (port stays 8001).
  Remote access also needs an appropriate `PRECEPTOR_ALLOWED_HOSTS` list;
  the bind override does not change trusted hosts.
- `./scripts/reset-demo` replaces application data with demo data from the
  canonical main worktree. Treat it as destructive and run it only with
  explicit human direction.
- `./scripts/inspect-processes PID [PID ...]` performs read-only host process
  inspection. It accepts only positive numeric process IDs.
- `./scripts/read-lines FILE [START_LINE [END_LINE]]` reads a repository file
  or a validated line range without modifying it. Paths must resolve inside
  the repository and line numbers must be positive integers.

Development settings trust only `localhost` and `127.0.0.1` by default.
`PRECEPTOR_ALLOWED_HOSTS` replaces that list with comma-separated hosts, trimming
whitespace and ignoring blank entries; empty lists and wildcard (`*`) entries
are refused. This is separate from `PRECEPTOR_BIND_ADDRESS`, which changes only
where the development/preview server listens, not which Host headers it trusts.

## Main-worktree helpers during Cassidy work

Avoid changing the main worktree while a Cassidy Attempt is active unless the
human explicitly requests an immediately useful helper. When an agent-owned
helper must be added during an Attempt:

1. Record and verify the exact agent-owned dirty paths. Never include unrelated
   or human-owned changes.
2. Before accepting the Candidate, stash only those exact paths with
   `git stash push --include-untracked -- <paths>` and record the created stash
   entry.
3. Accept the Cassidy Candidate.
4. Verify and pop that exact stash entry.
5. Commit the helper changes separately before starting the next Cassidy Task.

Never stash the entire dirty worktree as a shortcut.

## Cassidy

Before Cassidy planning or workflow work, read and follow `.casd/AGENTS.md`.
