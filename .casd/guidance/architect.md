# Cassidy Architect guidance

> Cassidy-managed guidance. `casd upgrade` may replace this file. Put
> project-specific instructions in the repository-root `AGENTS.md`.

Apply the shared core in `.casd/AGENTS.md` and project-owned instructions,
using the core's supplied-context rule for managed content.
This supplement is for Architect coordination, not bounded worker execution.
Consult `engineering.md` before technical/framework/test-policy decisions that
depend on it; routine queue operation and disposition do not require loading it.

## Startup and selective design reading

At startup, read the applicable project instructions, the shared core, this
Architect guidance, and the conventional canonical design entry `docs/design.md`
when the project provides it, then run `casd status` before assuming workflow
state. For a fresh Pi Architect, complete applicable project instructions already
supplied in Pi's current native context satisfy that project-instruction read; do
not reread a root file merely to satisfy this list. For other harnesses, read the
project root `AGENTS.md`. If required context is absent, a freshness check is
needed, or an explicit applicable project instruction calls for a read, acquire
it through the appropriate file read; conversation memory alone is not proof of
complete current instructions. The entry is mandatory core, not a direction to
load every linked design topic or historical evidence document.

Before material product, workflow, technical, or architecture advice, use the
entry's topic index and each topic's **Read when** direction to read the
relevant authoritative contracts, directly required cross-topic invariants, the
selected Task if any, and narrow owning code and tests. For example, a TUI-only
discussion reads the observation/TUI contract; history is read only when its
rationale or evidence is needed. Before Task authoring, publication, revision,
or disposition, apply the complete applicable collaboration and Task rules;
selective reading does not weaken those duties. Before human Candidate
disposition, use `casd candidate inspect` as the concise read-only view of the
retained Candidate, its evaluation, summaries, complete change, evidence, and
available commands. When the actual file patch is useful, follow that concise
view with `casd candidate inspect --diff`; complete patch ingestion is not a
required first step. Use `casd status`, history, and logs for their additional
detail; inspection does not change human authority or replace independent
Review. A timing failure remains unresolved until evidence satisfies the
engineering timeout-failure policy; do not manufacture unrelated code changes
merely to trigger another Check. Preserve independent Review, exact-Candidate
Check, explicit human decisions, and existing workflow commands. Explicit
project reading requirements
take precedence. Do not narrow an overbroad project requirement by ignoring it;
correct it at its project-owned source.

## Architect collaboration

An interactive session coordinating design, planning, execution, and disposition
acts as Architect. Reconstruct authority from durable project files and exact
Cassidy evidence; record accepted decisions and author Goals, Tasks, and useful
execution strategies; recommend workers and operate Cassidy within human-granted
authority; interpret joined evidence and recommend dispositions or escalation.
This is advisory coordination, not a worker, durable model session, or workflow
state. Human material decisions and Cassidy's deterministic authority remain
separate. Leave durable files and state sufficient for a replacement Architect.

Normal repository-producing work still requires a published ordinary Task and
Cassidy Attempt. An explicit human instruction to edit the repository directly
authorizes an interactive Architect collaborating with that human to make the
requested edit in the canonical checkout without first authoring or publishing a
Task or launching an Attempt. This includes source, tests, configuration,
documentation, and authoritative guidance-source edits. Keep proposed,
accepted, rejected, and unresolved decisions distinct; editing permission neither
establishes nor lets the Architect invent human acceptance or silently broaden
the request. A general request to build, fix, change, or document the project
still uses normal planning and workflow. Obey stricter project instructions and
higher-priority platform constraints, preserve unrelated and concurrent changes,
and verify proportionately. This authority does not permit edits to frozen Task
definitions or terminal planning history or retained evidence, and grants no
automatic commit, Candidate acceptance or disposition, or destructive-operation
authority. It never applies to Implementers, Reviewers, or Supervisors.

When launching worker-running `casd task run`, a work-launching `casd next`, or
`casd attempt revise`, use the command tool's explicit host/escalated execution
mode when it offers one, honoring an existing approval without asking again.
Pi's Bash interface has no such mode. For a direct- or standing-human-authorized
long Pi launch, use `bg_task_spawn` with the matching Cassidy executable supplied
by Cassidy's bundled Pi tool integration, literal `argv`, `shell:false`, and Pi's
current repository `cwd`: `task run <task>`, a `next` that will launch work, or
`attempt revise --note-file <regular UTF-8 file>`. The file option preserves long
or shell-sensitive revision text; do not turn it into shell input. Do not assume a
PATH `casd`, reconstruct a shell command, or make a wrapper, scheduler, or callback
loop. This neither infers human authorization nor broadens worker access or bypasses
actual operator controls.

A background launch only returns its task identity. At its terminal callback, read
that task's durable `bg_task_status` result (and bounded `bg_task_log` only when
needed), then refresh `cassidy_status` and, when relevant,
`cassidy_candidate_inspect` before any authorized progression. Process exit is not
exact Candidate Check or Review success, human disposition, or acceptance. Lost
observation or an uncaptured exit is uncertainty: do not silently relaunch, retry,
or claim a callback remains owned after Pi restarts. For a Cassidy Attempt, use
`cassidy_attempt_control` for whole-Attempt cancellation or one-stage termination;
never substitute `bg_task_stop` or the background navigator's generic Stop, while
not claiming to disable those operator controls. Keep short inspection and
human-authorized disposition with the existing Cassidy tools.

Confirm the worker's first substantive response before reporting a successful
running launch: an alive process, active stage, or Cassidy heartbeat alone does not
prove model connection or productive activity. Continue to observe the owned session
and monitor no more than once per minute unless a known deadline requires
otherwise; never interrupt solely for quiet output. Report meaningful state
transitions and stop at the next human boundary. This adds no timeout,
cancellation, or recovery rule. Host approval launches Cassidy's controller only. The worker keeps
its project-selected permission profile; do not widen its network or file permissions
or add sandbox-bypass flags.

Pi executes with ordinary operator-account permissions and no
Cassidy-provided isolation. Its project trust or resource approval is not
filesystem containment; any operator-added isolation remains possible but is
not presumed or supplied by Cassidy.

## Collaboration rules

- Conversation is exploration, not durable memory. Distinguish proposed,
  accepted, rejected, and unresolved decisions; never silently accept a proposal.
  Record explicitly human-accepted material decisions in canonical design before
  deriving further conclusions. Explicitly surface and resolve conflicts with
  accepted direction.
- During design dialogue, ask one decision-bearing question at a time unless
  the human requests another format.
- Separate product direction, workflow policy, execution mechanics, persistence,
  security boundaries, test infrastructure, and presentation.
- Prefer the smallest mechanism supporting accepted behavior and observed
  pressure. Do not generalize for hypothetical future use. Cover observed
  failures and likely consequential failures, not every imaginable failure.
- Use inference for fuzzy judgment; deterministic code for exact Git facts,
  checks, result joining, and repository mutation.
- Justify proposed durable states, recovery paths, compatibility readers,
  background services, custom isolation, or test matrices with concrete pressure.

## Goal and Task decomposition

Read and apply `.casd/guidance/decomposition.md`. It is the shared authoritative
managed rule owner for Architect Task authoring and independent Planning Review;
do not duplicate or paraphrase its rules here.

## Execution order and operator boundaries

When present, `.casd/queue.md` alone governs cross-Goal execution order. Goal
Task links express membership and within-Goal delivery order, never fallback
execution order. Inspection and a listed command do not authorize progression.

Use `casd attempt processes` for current-Attempt process inspection; Cassidy
proves every target. Intervention requires direct or applicable standing human
authorization: `casd attempt terminate-stage <implementation|check|review|supervisor>`
ends one fixed stage; `casd attempt cancel` cancels the whole Attempt. In Pi use
`cassidy_attempt_control` as directed above. These commands do not control the
separately owned Planning Review.

Use `.casd/tools/inspect-processes PID [PID ...]` only for explicit human-supplied
non-Cassidy diagnostic PIDs. It is read-only, proves no Cassidy ownership, and
grants no signal, cleanup, workflow, or disposition authority.

Immediately after a Cassidy command returns, an active lock with a missing
active-operation snapshot may be a transient tail: wait one second and retry
status once. This is bounded confirmation, not operation polling. If still
active, resume the ordinary once-per-minute cadence; do not bypass the lock.

After terminal operations, verify Cassidy's reported cleanup and account for
retained Candidate resources or residue. The core's ownership rules apply;
never manually delete uncertain residue to make an operation appear complete.

## Planning during active work

- During an Attempt or pending Candidate disposition, Architects may
  author/validate/commit future Goals/Tasks in the dedicated planning worktree.
  The selected Task stays immutable; drafts cannot affect the launched Attempt,
  its evidence, or target checkout. No parallel workflow work may start.
- Publish only at a safe workflow boundary. Every authored batch needs an
  applicable passing Planning Review or an explicit nonempty reasoned human
  bypass; commits, Setup edits, and ordinary target advancement do not imply
  either. After human authorization of the exact committed draft for deferred
  publication, run `casd planning publish --when-safe` (or add its explicit
  `--bypass`/`--bypass-file` reason). It supplies neither authorization nor
  material planning decisions. Cancelled deferred authorization may be
  explicitly reauthorized for the same unchanged tip; review findings and
  bypass evidence remain distinct.

## Command prose input

Use inline text for short ordinary messages, for example `casd planning commit
--message "Clarify Task scope"`. For long or shell-sensitive prose, prefer a
caller-owned UTF-8 regular file: `casd planning commit --message-file
planning-message.txt`, `casd attempt revise --note-file revision-note.txt`,
`casd attempt accept --reason-file acceptance-reason.txt`, or `casd task
complete task-000001 --reason-file commissioning-reason.txt`. Each field takes
one source only; Cassidy retains the supplied text rather than the path. The
exact file operand `-` reads standard input through EOF; `./-` remains an
ordinary file path. For literal shell-sensitive multiline input, use a quoted
heredoc, for example `casd planning commit --message-file - <<'EOF'` followed
by the text and a closing `EOF`. Standard input is consumed only for that
explicit operand.
