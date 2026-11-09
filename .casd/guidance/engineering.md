# Cassidy engineering guidance

> Cassidy-managed guidance. `casd upgrade` may replace this file. Put
> project-specific instructions in the repository-root `AGENTS.md`.

Apply the shared core in `.casd/AGENTS.md` and project-owned instructions,
using the core's supplied-context rule for managed content.
This supplement is shared by Implementers and Reviewers, and consulted by the
Architect when technical or testing decisions require it. Apply instructions
within the current role: implementation completion and quality-command duties
belong to the Implementer; independent Review follows Cassidy's supplied Review
instructions. This file grants neither planning nor disposition authority.

## Framework-first development

- Default to deliberately adopted abstractions' conventional supported paths:
  frameworks, ORMs, templates, migrations, validation, date/time, browser tests,
  task runners, and similar layers. Be framework-first, not maximalist. Retained
  dependencies should buy portability, conventions, safety, maintainability, and
  less custom code; do not casually duplicate their responsibilities.
- Before planning or implementation, inventory relevant layers, libraries,
  conventions, adapters, generators, and extension points; name responsibility
  owners. Prefer mature, actively supported, widely accepted libraries for hard
  or cross-cutting work: timezone/calendar arithmetic, migrations/queries,
  serialization, authentication/authorization, cryptography, HTTP parsing and
  validation, browser automation, concurrency, pooling, retries, and test
  orchestration. Local code size alone does not justify hand-rolling these.
- Use layers' normal generators, typed APIs, query builders, model/entity and
  migration facilities, validation, rendering, dependency injection,
  configuration loading, lifecycle hooks, and testing facilities. Simpler,
  well-supported direct/lower-level paths are appropriate, but each escape hatch
  must be narrow, isolated, named as an adapter or boundary, and justified by a
  concrete limitation or requirement. Exclude technology-specific types from
  domain/policy code. Test escape hatches directly; use shared conformance tests
  across multiple adapters.
- Prove portability with the same behavioral conformance suite on every
  supported backend. Unsupported adapters/configuration must not silently do
  nothing. Where a chosen layer offers typed/standard solutions, avoid stringly
  typed internal interfaces, ad-hoc delimiter persistence, application-code raw
  markup, dispersed raw SQL, custom calendar arithmetic, and custom protocol clients.
- In greenfield work, establish boundaries/conventions early and fix foundational
  shortcuts rather than preserve premature compatibility. Stop and surface the
  architectural tradeoff if a request requires large backend-specific emulation,
  defeats a selected abstraction, or makes another explicitly supported backend
  impractical for normal production.
- Task contracts name the owning layer, conventional path, permitted escape
  hatches, and behavioral obligations at the boundary and supported adapters.
  Use existing owning evidence where sufficient. Do not split a coherent
  architectural decision across Tasks that must rediscover or violate it.
- Review deliberate low-level use separately from accidental bypass. Check for
  duplicated layers, lost portability/safety, technology leakage, ecosystem
  conventions, and total size across tests, failures, upgrades, and maintenance.
  Reject accidental bypass, unnecessary custom infrastructure, technology
  leakage, unproved portability, and custom solutions to difficult standard
  problems without documented evidence that established options are unsuitable.
- Prefer boring, stable, supported technology and project consistency over
  novelty. Verify framework conventions/library capabilities in current official
  documentation before asserting them. Apply contextually across ecosystems;
  no language's architecture is universal.

## Implementation discipline

- Build only the accepted slice; future possibilities are not requirements.
- The project owns its verification tools, supported platforms, infrastructure
  requirements, and runtime budgets. Use its supported runners and fixture
  facilities; prefer deterministic injection to sleeps and timing races. Keep
  platform-specific code narrow and prove behavior where support is promised.
- Follow the supplied implementation completion instructions for focused tests,
  the configured direct quality command, and required final verification.
  Local results are diagnostic; Cassidy's later Check on the exact Candidate
  remains authoritative alongside independent Review.
- When a contract changes, ask what assumption changed and where else it controls
  behavior. Trace it from producer through relevant consumers and observable
  outcomes, not only changed files. For example, several results rather than one
  can affect identity, deduplication, downstream work, presentation, and
  persistence feedback where those layers exist.
- A discovered defect is evidence of a potentially missed assumption. Investigate
  that assumption and its relevant consumers proportionally to guide repair of
  the owning mechanism, without expanding the Task for unrelated defects.
  Implementation and independent Review examine the repair's consequences,
  including its tests: consumers whose accepted contract requires multiple
  relevant results must not silently collapse or omit them after an identity or
  multiplicity change, and a timing repair must not add a timing-sensitive test.
  Use feasible examples and meaningful owning evidence, not blanket extra tests
  or a new end-to-end harness.

## Test quality

- Before adding an assertion, identify its intended lifetime: enduring product
  invariant, permanent removal or security boundary, or temporary delivery-stage
  condition. Every Task needs appropriate verification, but acceptance requires
  sufficient evidence, not automatically a committed permanent test. One-time
  verification and retained Attempt evidence can establish a temporary
  withdrawal or intermediate limitation.
- Do not freeze staging restrictions into regression contracts: absent controls,
  unavailable capabilities, temporary notices, an initial mode being the only
  mode, or today's catalogue size, unless the restriction is an accepted
  enduring requirement. When behavior is superseded, retire or adapt obsolete
  assertions; do not invert a positive assertion into a temporary absence
  assertion merely to preserve test count. A test's existence, wording,
  inventory, or implementation shape alone establishes no product requirement.
  These are generic, conditional illustrations, not requirements to implement
  unsupported behavior.
- Preserve lasting protection for authority, persistence, history, rollback,
  identity, provenance, security, and meaningful behavior successors must
  retain. A permanent removal can warrant lasting negative coverage when its
  reintroduction violates an accepted contract; judge the consequence and
  requirement, not the label "removal." Speculative future changes do not
  justify discarding protection for still-required behavior.
- Before adding tests, identify the required behavior and its existing
  protection. For each new or changed assertion, identify the requirement, a
  plausible detected defect, and a requirement-preserving change it must allow.
  Derive these from Task requirements, documented contracts, supported consumers,
  and regression history; observed output alone is insufficient. Choose the
  smallest sufficient evidence boundary. Reuse owning tests rather than
  automatically duplicating a behavior across unit, HTTP, and browser tests;
  additional tests should protect distinct consequential failures. Add lifecycle
  cross-products only when a concrete defect demonstrates the interaction matters.
- Assert requirement-constrained details. Exact values, order, multiplicity,
  text, and interactions are valid when a real contract requires them. Equivalent
  HTML, incidental date-description wording, or additions/reordering of supported
  definitions should not fail tests unless separately constrained. A current
  list, layout, private representation, or helper call sequence is not itself a
  contract. Nor does unimplemented future work alone justify forbidding its
  navigation entries. Name tests for protected behavior. Use focused snapshots
  and exact output comparisons selectively; normalize only irrelevant variation.
- UI assertions associate the correct record's value with the correct field and
  context, using semantic locators or stable IDs and distinguishable fixtures.
  Page-wide heading/value existence is insufficient. Protect visible information,
  actions, and promised transitions through real rendering/interaction where
  needed; matching hidden metadata alone cannot prove working output or wiring.
  Allow unrelated presentation changes without weakening checks to vacuous
  existence or substring assertions. CLI/TUI tests likewise protect outcomes,
  required diagnostics, transitions, and promised subsequent operations.
- Distinct scenarios explicitly establish their relevant preconditions. Split
  unrelated browser assertions that depend on accumulated navigation history or
  shared mutable state. Keep genuine end-to-end sequences when the sequence
  itself is the behavior under test. Fixtures and examples must be feasible
  under the implemented contract, not require unsupported product behavior.
- Derive expected answers independently from rules using clear examples or
  justified properties; do not copy production algorithms or call them to
  compute expectations. Test cohesive behavior, not every helper. Reuse suitable
  fixtures and mock external boundaries without mocking away the tested behavior.
- Before running new or materially changed tests, inspect their assertions for
  unjustified exactness, weak record/field association, copied implementation
  logic, and unrelated shared scenario state. This is ordinary implementation
  reasoning, not a checkpoint, durable artifact, or required essay beside each
  test. Record only non-obvious contract decisions that future maintainers need.
- After a failure, distinguish product defect, fixture/setup defect, obsolete
  expectation after an intentional contract change, and overspecification before
  changing code or expected output. Never bulk-accept changed snapshots or
  strings merely because output differs. Correct accidental constraints while
  preserving unique regression protection; do not weaken assertions, skip tests,
  or change production solely for green runs. Investigate ambiguous contracts;
  absent documentation is no deletion license.

### Timeout failures

- Treat a timeout as an unresolved failure. Earlier passes, machine load, or
  suspected flakiness do not establish its cause. A diagnostic rerun answers a
  specific question with attributable evidence; a later pass alone neither
  resolves the original failure nor proves a correction.
- Trace the timed operation and its owners through startup, readiness, work,
  cancellation, descendant exit, resource cleanup, and pipe closure to locate
  the deadline-exceeding phase. Separate demonstrated facts from hypotheses. If
  retained evidence cannot locate the delay, improve attribution rather than
  claim a retrospective diagnosis.
- Compose deadlines so an outer operation can await the inner work it promises,
  including sequential cleanup budgets and reasonable scheduling headroom under
  supported concurrency. Fix the owning mechanism. Increasing or scaling its
  timeout is valid when its responsibilities and supported conditions justify
  it; blind unrelated inflation, concealment retries, disabled checks, and
  abandoned cleanup are not corrections.
- Preserve finite failure, useful diagnostics, primary errors, ownership
  boundaries, and evidence that cleanup completed. Explain why verification
  addresses the failure: prefer focused deterministic delay/deadline evidence
  through existing seams, plus proportionate real execution under relevant
  conditions. Sleeps, stress matrices, and one green run are not causal
  evidence; deterministic reproduction of every historical timing failure is
  not required.

- Iterate with the narrowest supported rerun. If the project lacks a suitable
  focused entry point, report that limitation and use its supported verification
  path; do not bypass its runner, permissions, or resource ownership. Preserve
  required final verification and authoritative Candidate Check.
- For significant behavior, where practical confirm failure on prior broken
  behavior or a representative fault, then success on correct implementation;
  failure must come from the intended assertion. Passing tests, coverage, and
  mutation scores alone do not prove quality. Use existing tools and proportionate
  verification, reporting unexecuted checks and unresolved uncertainty accurately.

### Accepted TEXT-CONTRACT convention

- Mark intentionally stable production text with `TEXT-CONTRACT: <identifier>`
  beside the string only when an actual requirement or supported consumer needs
  its exact form. Reference the same ID in its exact-text test. Record ID, reason
  (who depends on wording), scope (punctuation, whitespace, capitalization,
  newlines), and requirement/protecting-test references.
- Preserve marked text unless the Task changes its contract. Justify exact-text
  assertions with existing requirements/consumers; never invent contracts or
  markers to defend brittle tests. Test dependence is not a product requirement.
- Protecting tests use independent expected literals/approved fixtures, not
  imported production constants as expected answers. Other tests may reuse those
  constants for different obligations. Linked production/test changes require
  Review; changing both proves nothing by itself. This is a searchable human
  comment convention, not a registry, parser, automatic validator, or framework.
