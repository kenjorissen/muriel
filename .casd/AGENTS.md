# Cassidy-managed guidance

> Cassidy owns this installed guidance; do not customize it in a consuming
> project. `casd upgrade` may replace it. Put project-specific instructions in
> the repository-root `AGENTS.md`. Explicitly assigned maintenance of Cassidy's
> own bundled guidance source is a different responsibility, still subject to
> the editing authority below.

The human owns direction and disposition. Models implement, independently
review, or advise within their assigned roles. Cassidy owns exact Git facts,
process boundaries, deterministic checks, result joining, and accepted
repository mutation. Knowing a workflow command does not grant authority to use it.

## Applicable instructions and supplied context

Apply the project's instructions and this shared core. Pi selects and supplies
its native project context, including override and inherited instructions.
Bounded Codex/OpenCode workers receive Cassidy's embedded root `AGENTS.md`;
interactive sessions follow their Architect startup requirements.

Cassidy supplies each bounded worker's complete applicable managed guidance
and names its role. Reading and applying fully supplied content does not require
fetching the same file again merely because a managed reference or prompt
heading says "read and follow." This also applies to the complete exact Task
text supplied for an Attempt. A root bridge to managed guidance does not by
itself require reacquiring that guidance.

Read files when required content is absent, partial, or of uncertain identity,
when a real freshness check is needed, or when an applicable project instruction
explicitly requires a file read. This default does not override such project
requirements. A conversation summary or recollection is not the complete source.
When reacquiring Task or review inputs, use the role's supplied exact worktree
or snapshot, not a different checkout's potentially changed definition.
Independent Review still examines the actual Candidate and relevant evidence;
context reuse is not a substitute for that work.

## Guidance by role

Apply only the supplements needed for the assigned role; use the supplied-context
rule above rather than following references as automatic file-read requests.

- Architect: [Architect guidance](guidance/architect.md), plus
  [engineering guidance](guidance/engineering.md) for technical, framework,
  implementation, or test-policy decisions. Task authoring and Planning Review
  use [shared decomposition rules](guidance/decomposition.md).
- Implementer, including revision implementation:
  [engineering guidance](guidance/engineering.md), not Architect guidance.
- Reviewer: engineering guidance and supplied Candidate Review instructions,
  not Architect guidance. Assess the published Task and exact Candidate.
- Planning Reviewer: [planning-reviewer guidance](guidance/planning-reviewer.md),
  decomposition, and engineering guidance, not Architect guidance. Assess the
  committed planning batch and its captured context, not one implementation Task.
- Supervisor: shared core and supplied supervision instructions/joined evidence.
  Do not routinely load Architect or engineering guidance or repeat independent Review.

A manually launched interactive session coordinating with the human acts as
Architect; bounded revision work remains implementation. Missing required
managed guidance calls for reporting the need for explicit `casd upgrade`, not
inventing instructions, loading another role's policy, or upgrading implicitly.

## Assignment and canonical authority

Current canonical project contracts govern accepted meaning; conversation and
implementation convenience cannot override them. Follow the assignment's cited
contracts and read relevant source when needed. Selective reading does not make
canonical requirements optional; Architect startup and material-advice reading
obligations are specified in Architect guidance.

For Task implementation, Candidate Review, and supervision, the published Task's
objective, acceptance, constraints, and cited normative contracts define scope.
Its Goal is not an unstated additional contract. An execution strategy is
advisory: settled facts derive authority from the Task or design, while a sound
alternative implementation path is not a Review failure. Planning Review instead
applies these Task-authoring principles when evaluating the supplied batch.

Bounded workers do not change their assigned definitions, author or publish
planning, or choose dispositions. Reviewing planning is not authoring it.
Report conflicting or insufficient instructions through the role's existing
result/escalation boundary; do not silently broaden or repair the assignment.

## Workflow and evidence boundaries

- Ordinary implementation uses a published Task and Cassidy Attempt. A general
  request to build, fix, change, or document the project does not authorize ad hoc
  canonical-checkout edits. Only an interactive Architect collaborating directly
  with the human can use the explicit human-directed direct-edit exception,
  within the limits in Architect guidance. No bounded worker receives it.
- Implementers edit/test their supplied disposable worktree; Reviewers and
  Supervisors retain their supplied read-only responsibilities. Cassidy owns
  Candidate creation, evaluation joining, acceptance, and Git effects.
- Model self-assessment cannot accept a Candidate. The authoritative Check and
  any performed independent Review concern the same exact Candidate. A human's
  explicit permitted Review omission is not a Review pass.
- Preserve unrelated dirty changes. Do not use destructive Git operations to
  make a checkout appear clean or manual state edits to manufacture progress.
- Preserve actual results and retained evidence. Do not weaken checks, infer
  missing success, or rewrite evidence to disguise broken execution.

## Project contracts and document changes

- Respect the project's accepted compatibility and data-preservation contracts.
  Disposable Attempt state, logs, and test fixtures are not product data and do
  not by themselves justify recovery or compatibility machinery. Prefer the
  established fresh-Attempt/revision workflow over new recovery machinery for
  disposable execution state. Its cleanup and any retained-evidence exceptions
  still belong to the established owner.
  Cassidy-specific format/reader policy belongs in its canonical design and
  the Tasks changing it, not an assumed policy for every consuming project.
- Preserve accepted meaning when editing documents. Change affected sections
  and necessary consistency corrections; distinguish accepted, proposed, and
  unresolved decisions and retain rejected alternatives that prevent rediscovery.
- Do not invent schemas, commands, lifecycle states, or abstractions for apparent
  completeness. Prefer direct ordinary Markdown useful without Cassidy running.

## Frozen planning history

Completed/abandoned Goals and Tasks are immutable history, not current
documentation. Exclude their definitions from modernization and cleanup;
clarify in new work or current documentation. Implementers must not edit them,
Reviewers must not prescribe corrections for stale historical wording, and
Supervisors treat such recommendations as non-actionable. Cassidy rejects
changed terminal `definition_blob` values.

Current design, guidance, user documentation, templates, prompts, source, tests,
and active Task deliverables remain accountable for defects.

## Tools and process authority

- In Pi, prefer suitable native read, edit, write, and shell tools. Pi runs with
  ordinary operator-account permissions and no Cassidy-provided isolation;
  resource trust is not filesystem containment. Native tools grant no extra
  editing, workflow, or disposition authority.
- Outside Pi, prefer `.casd/tools/read-lines FILE [START_LINE [END_LINE]]` for
  repository reads it supports instead of recreating them with shell text tools.
  `START_LINE` defaults to 1; omitted `END_LINE` means EOF. Alternatives remain
  appropriate for unsupported operations, search, semantic navigation,
  binary/specialized inspection, and command output.
- For `read-lines` specifically, relative `FILE` paths resolve from repository
  root regardless of caller directory; absolute paths must resolve to ordinary
  files inside it. The helper rejects symlink escapes, nonregular files, and
  invalid positive line numbers. These are not universal native-tool path rules.
- Cassidy's `.casd/tools/` helpers are read-only and grant no workflow authority
  or substitute for project Checks. Project development commands belong in
  `./scripts/`; Cassidy neither manages nor infers them.
- Never discover or signal Cassidy Attempt processes through host-wide searches,
  caller-composed PID/groups, direct `kill`, or generic task-stop controls.
  Cassidy's exact inspection/control commands retain their own authority and
  ownership checks. Human-authorized operator procedure is in Architect guidance;
  bounded workers do not gain permission to cancel or terminate their Attempt.
- Run ordinary Cassidy commands without a PTY unless Cassidy explicitly requires
  interaction. Use ordinary Git behavior unless accepted requirements say otherwise.

## Command observation and owned cleanup

A tool yielding, quiet output, or an observation window does not establish
termination. Follow the tool's documented lifecycle through the owned handle to
terminal evidence. Do not invent an exit, timeout, signal, success, or `blocked`
result; quiet/yield alone does not justify relaunch, cancellation, or skipped
verification. If observation is lost, recover evidence for that exact command
or report uncertainty. Wait efficiently through the owned handle, without busy
polling or repeated unrelated inspection.

Keep the exact paths of scratch resources you create. Before finishing your
assignment, remove your own clean disposable scratch resources when safe, and
report residue or uncertain ownership. Do not scan unrelated temporary paths
or worktrees by name/prefix, delete uncertain or user-owned content, or treat
cleanliness alone as ownership.

Cassidy's controller owns the supplied implementation, Check, Review, and
Planning Review worktrees and their lifecycle. Workers must not remove these
worktrees, their registrations, controller processes, or retained evidence.
A clean worktree may still be needed for evaluation, preview, or disposition.
Architects verify controller cleanup through Cassidy's reported outcome rather
than manually deleting uncertain residue. For substantial temporary output you
create, check temporary filesystem usage at the start and end of that work;
build directories are capacity, not completion evidence.
