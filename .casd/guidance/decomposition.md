# Cassidy shared decomposition and execution-strategy guidance

> Cassidy-managed guidance. `casd upgrade` may replace this file. Put project-specific
> instructions in the repository-root `AGENTS.md`.

Architects and Planning Reviewers use these same rules. This file supplies planning
judgment only; it grants Planning Reviewers no Architect workflow, publication,
or disposition authority.

## Goal and Task decomposition

- Before publication, check every Goal and Task against canonical design's
  terminology, owners, invariants, relationships, and lifecycle decisions.
  Each Task must be sufficient for implementation and independent Review without
  relying on its Goal as an unstated contract. Cite the narrow normative design
  sections needed for implementation and Review, especially easy-to-miss
  requirements; do not force broad parent-document reconstruction.
- `Type: evidence-only` is the narrow exception for operational, verification,
  or commissioning work performed by the human and Architect without an
  Implementer or Git Candidate. Publish its objective and observable acceptance
  for explicit human completion; never use it to bypass ordinary implementation.
- A Goal defines one outcome and success conditions sufficient for human
  completion judgment. Tasks decompose delivery; they do not replace the outcome.
- A Task owns one independently reviewable behavioral outcome: state objective,
  observable acceptance, boundary, relevant owner/invariants, and necessary
  context/predecessor assumptions. It is one human Review/disposition unit, not
  a worker-turn estimate.
- Before publication, assess product behavior, not checklist length. Treat human
  "big swing" concerns as useful even without clear boundaries. Inspect distinct
  user-visible outcomes, state/lifetime owners, presentation modes, integrations,
  unresolved product choices, and consequential failure questions. Split-first
  test: which concerns can independently succeed/fail, have useful direct
  acceptance, and remain valid under successor extension? Multiple such outcomes
  or unmade contract decisions suggest a Goal: state that judgment, visible
  evidence, and uncertainty; sketch Goal/Task boundaries and ask one
  decision-bearing question without waiting for complete human vocabulary.
- Decompose into the smallest useful behavioral increments with enduring
  acceptance, not file or technical-layer assignments. For each proposed
  predecessor/successor boundary, describe what the human can use or directly
  observe after the predecessor, which guarantees and regression evidence remain
  valid after the planned successor, and what the successor adds without replacing
  those guarantees. Keep implementation, direct tests, and necessary documentation
  together; smaller assignments alone do not establish useful delivery.
- Mandatory behavior belongs in the Task contract. Optional advisory execution
  strategy may suggest owners, sequencing, and verification; it creates no
  requirements, lifecycle objects, or delivery boundaries.
- After establishing one coherent delivery boundary, assess whether avoidable
  discovery, reconstruction of settled context, or causal sequencing warrants
  an authored execution strategy. Include one before publication when it would
  materially reduce that burden; this is not a model-tier, length, file-count,
  or revision-count rule. Human review and publication remain authoritative.
- Conventionally put strategy under an unparsed `## Execution strategy` heading.
  It applies unchanged to every Attempt: never generate, augment, or specialize
  it at launch for a worker or model. Settled facts derive authority from the
  Task contract or cited design; sequencing is advisory and ordinary choices
  remain with the worker. Review must not reject a sound alternative path.
- Strategy cannot combine independently observable lifetime, concurrency,
  delivery, isolation, presentation, or platform contracts. Sharing an owner,
  mechanism, stream, or implementation sequence alone does not make one Task.
- Separately review each authored strategy before publication: the worker must
  not need broad archaeology, choose unresolved contract-relevant architecture,
  or reconstruct implicit precedence, ownership, lifetime, or errors. Require
  causal reasons for sequencing and verification, no hidden requirements or
  prescribed code recipe, and no checkpoints disguised as delivery outcomes.
  Acceptance must remain judgeable without requiring the recommended path.
- Normally assign one primary failure contract per Task. Presume separate Tasks
  for independently correct/broken risks or authority concerns with direct probes
  or partial public behavior, unless one outcome replaces/reinterprets another.
- Before publication, name every Task's state/lifetime, concurrency, transport,
  isolation, dependencies/coordination, presentation, and platform concerns.
  Before combining independently failing concerns, state the narrower
  predecessor's observable acceptance, preserved invariants, and stable contract
  consumed by its successor. Only then compare reasoning and coordination costs.
- Independent acceptance need not be a complete user feature: a directly
  observable resource lifetime, authoritative event source, or complete default
  mode may remain valid when extended. Editing the same owner can extend a
  predecessor; replacing, weakening, or reinterpreting its enduring behavior,
  or discarding evidence for its accepted invariants, cannot. A predecessor must
  remain coherent and adequately verified at delivery, while a successor may
  retire evidence specific to a temporary staging condition. Session/protocol
  behavior, event production, asynchronous delivery/order, backpressure/failure
  isolation, presentation, raw integration, and platform adaptation are separate
  candidates when independently observable. A complete pure policy, query, or
  review capability can also precede persistence or UI integration: technical
  layer alone neither establishes nor rules out a Task. Public visibility of a
  helper is insufficient; require a concrete use, meaningful direct behavioral
  acceptance, and a stable contract whose guarantees and meaningful regression
  protection survive successor extension. Do not demand an already-integrated
  authoritative writer just to accept an otherwise complete review/query
  capability.
- For a changed representation or contract, ask what assumption changed and
  identify every relevant current reader, writer, command, UI, test, and other
  semantic consumer. Trace it from producer through observable outcomes; for
  example, several results rather than one can affect identity, deduplication,
  downstream work, presentation, and persistence feedback where those layers
  exist. Each consumer must remain coherent and usable immediately after
  delivery, with authoritative Check passing without ignored, disabled, or
  postponed tests awaiting a successor. Representation-consumer coherence applies
  when the representation is introduced or changed; it does not absorb independently
  useful prerequisite behavior leaving current consumers coherent and immediately
  usable. Replace consumers together or stop their dependency before removing a
  concept's representation. A successor extends a valid predecessor; it must not
  repair one broken by the split. Separating
  persistence from presentation fails this test when current commands or UI
  cannot use the new representation. This is human judgment, not a parsed
  checklist, score, manifest, dependency graph, or validator.
- Before combining substantial concerns, test a concrete narrower delivery, not
  merely an alternative such as "types first" or "persistence alone." Reject it
  only by identifying a specific usefulness, authority, consistency, or verification
  problem. Shared machinery and a desire to deliver the complete feature are not
  sufficient reasons to combine independently useful outcomes. Do not invent
  temporary interfaces or product restrictions just to manufacture smaller Tasks.
  Before rejecting a complete policy/query/review predecessor as scaffolding,
  examine its actual outcome, direct acceptance, and what its successor adds
  without replacing, weakening, or reinterpreting accepted behavior. Name the
  concrete problem rather than relying on the combined feature's label.
- Scaffolding has no independently observable acceptance without temporary
  implementation detail, or its outcome is replaced by a successor. Types,
  hooks, and plumbing stay with their first consumer when their meaning depends
  on it. Intermediate states and test checkpoints may be strategy steps; make
  them Tasks only with independent behavioral Review/disposition acceptance.
  A durable, inspectable resource lifetime or a complete explicit path may be
  useful before planned automation or additional consumers; demonstrate its
  enduring acceptance rather than accepting or rejecting that boundary by label.
- For resource creation, cancellation, or cleanup, name owned resource classes
  and terminal cleanup owner. Check ordinary failure, cancellation, and
  completion-race ownership without prescribing a lifecycle cross-product.
- Evaluate each predecessor's acceptance and proposed regression assertions
  against the already-planned successor behavior. An assertion that would reject
  that behavior protects a temporary limitation, not an enduring requirement:
  reframe it around lasting behavior, use appropriate one-time delivery evidence,
  or reconsider the boundary. Acceptance requires sufficient evidence, not
  automatically a committed test. Do not require permanent tests for temporary
  conditions or treat retirement of their one-time evidence as lost protection.
  Tests need not remain textually unchanged: legitimate fixture or interface
  adaptation may preserve the same protected behavior. Distinguish that from
  replacing assertions that forbid an already-planned capability. This is
  authoring judgment, not a demand for testing commentary from every worker.
- Author verification direction around behavioral obligations, existing owning
  evidence, and important allowed variations when useful, not a unit/HTTP/browser
  inventory. For example, where supported: "Role choices come from supported
  definitions even without existing grants. Adding or reordering supported roles,
  or changing equivalent HTML presentation, must not break these tests." Examples
  and fixtures must be feasible under the implemented contract; do not demand
  unsupported product behavior to supply a test case. Anticipated examples are
  not automatically requirements. Apply engineering guidance when authoring this
  direction; leave ordinary test design to implementation and sufficiency to
  independent Review.
- Treat implementer fit and revision churn as observed evidence, not a promise
  from Task size. Before recommending smaller Tasks or a stronger model, use
  actual Task and Attempt evidence to distinguish excessive scope from incomplete
  consumer tracing, inadequate tests or Review, tooling failures, and workflow
  mistakes. Repeated context reconstruction, boundary confusion, unrelated
  revisions, sequencing failures, or test accretion warrant reassessing contract,
  context, strategy, and tools. Subdivide only at independently reviewable
  boundaries; improve context, tools, or worker fit for coherent hard work rather
  than manufacturing dependent scaffolding.
- State real predecessor assumptions explicitly; authored order does not create
  a dependency graph. Open Goals may gain Tasks as evidence reveals boundaries.
  Investigate unknown causes/owners before publishing speculative delivery
  Tasks. Diagnosis is planning input unless it delivers an independently
  reviewable repository outcome.
- Failed/interrupted or declined Attempts are execution evidence, not Task
  redefinitions or proof that a combined Task is impossible. They motivate
  reassessment, not automatic splitting. If the contract fits, use the accepted
  fresh Attempt/revision workflow.
  Reassess decomposition when revisions expose independently failing boundaries.
- At every human revision/disposition boundary, reassess semantic scope using
  exact Task-evolution chronology, joined Candidate evidence, human revision
  notes, and repository context. A discovered defect calls for proportional
  inspection of other consumers of its changed assumption, not a fix only at the
  reported location. Passing Candidates revised for product/visual judgment
  count; pass/fail ratios and revision counts do not. Repeated paths are clues;
  distinct Review themes, discovered behaviors, and independently failing
  contracts are stronger evidence.
- Multiple revisions can reflect ordinary difficulty within one coherent hard
  boundary. If remaining work exceeds one disposition unit, recommend the
  applicable existing discard, new Goal with independently reviewable Tasks, or
  abandonment path. Explain preservation of independently useful outcomes, not
  just smaller work. Without human authority, never accept partial work, change
  active/frozen Tasks, reparent Tasks, or change planning. This is Architect
  judgment, not a numeric threshold, complexity score, prose parser, mandatory
  schema, automatic classifier, new model invocation, planning lifecycle,
  Supervisor-policy expansion, or workflow action.
- Before publishing a Task set, repeatedly apply every decomposition rule in
  this section to every Task. Each complete pass's entire output, including
  rewritten/new Tasks, is fresh input to the next complete pass. Visibly record
  initial and every post-pass count, e.g. `4 -> 7 -> 12 -> 12`. Continue until
  two consecutive completed passes have equal post-pass counts: a full pass
  reconsiders every Task under every rule and starts/ends with `N` Tasks.
- Count stability never excuses rule violations; every final Task must satisfy
  every rule. Stop and report oscillation, failure to stabilize, or a material
  product decision to the human; never choose an arbitrary Task set. This is
  fixed-point authoring judgment, not durable planning state, a planning
  lifecycle, or runtime enforcement. Preserve existing human planning/publication
  authority and structural-only validation.
- Planning validation proves structural consistency; human pre-publication
  review owns semantic sufficiency.

