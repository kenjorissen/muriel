# Five-year synthetic staging performance — August 28, 2026

## Evidence identity

**Historical record:** These measurements, findings, and dispositions describe
only the August 28, 2026 deployment. The commit and Goal identifier below are
private pre-export provenance, not required public Git objects or links. They
do not establish performance, deployment readiness, or security of a new public
revision. Machine names are omitted; the measured host specifications remain.
Current operation belongs to [the staging guide](../staging.md), and current
solver limits to [Precepting solver operations](../precepting-solver.md). In
particular, the six-month samples below predate the three-month solve cap and
are not a supported current reproduction target.

- Deployed commit (private pre-export provenance): `02aacf97db768ff200144a53e3d2a67ef0a66132`
- Environment: isolated `STAGING — SYNTHETIC DATA` service on the historical staging host
- Service activation for this sample: 2026-08-28 17:35:10 UTC
- Scenario seed: `muriel-synthetic-v1`
- Fixed history baseline: July 1, 2020
- Scenario as-of date: August 28, 2026
- Precepting completed through: August 31, 2026
- Attention-visible future Precepting range: August 28, 2026 through January 29, 2027
- Probe range: August 1, 2021 through August 31, 2026
- Schedule Bot open range: September 2026 onward; one-year balance window
- Host: AMD Ryzen 7 8745H, 8 physical cores / 16 threads, 28 GiB RAM, NVMe storage,
  Ubuntu kernel 7.0.0-30-generic, PostgreSQL 18

The cold-enough sample followed a fresh staging deployment and service restart. Before it ran,
the service had received only its deployment health check and one unauthenticated sign-in-page
request. The warm sample immediately repeated the same accepted probe workload.

The PostgreSQL staging database occupied 33,224,383 bytes (31.7 MiB). Core retained volume was
19 Physicians, 3,476 Preceptor sessions, 6,480 current assignments plus 6,480 assignment-change
records, 307 Late Doc assignments plus 307 changes, and 321 published-week revisions. It also
contained 347 normal-rounding records, 140 interview shifts, 128 vacations, 27 academic-event
occurrences, and 63 holiday occurrences.

The future-open invariant held throughout the attention range: zero future Preceptor assignments,
Late Doc assignments, or published weeks. The post-probe maximum session date was February 26,
2027 because Schedule Bot preflight lazily initialized the six-month probe range; those sessions
contained no applied proposal assignments. Global Work Remaining projected 22 Precepting work
items containing 460 required actions and 39 advisory actions.

## Interactive HTTP workload

| Workflow | Cold wall time | Cold queries | Warm wall time | Warm queries |
| --- | ---: | ---: | ---: | ---: |
| Master Schedule, current | 0.047 s | 25 | 0.039 s | 25 |
| Master Schedule, five years prior | 0.043 s | 27 | 0.040 s | 27 |
| Work Remaining | 0.509 s | 984 | 0.515 s | 984 |
| Precepting calendar, current | 0.386 s | 739 | 0.380 s | 739 |
| Precepting list, current | 0.312 s | 612 | 0.311 s | 612 |
| Precepting calendar, five years prior | 0.364 s | 742 | 0.365 s | 742 |
| Precepting list, five years prior | 0.299 s | 595 | 0.301 s | 595 |
| Five-year Precepting report | 0.159 s | 92 | 0.167 s | 92 |
| Five-year Community Precepting report | 0.174 s | 93 | 0.173 s | 93 |
| Five-year Interviews report | 0.020 s | 6 | 0.027 s | 6 |
| Five-year Academic Events report | 0.019 s | 7 | 0.024 s | 7 |
| Five-year Normal Rounding report | 0.023 s | 8 | 0.023 s | 8 |
| Five-year Casual Rounding report | 0.018 s | 7 | 0.020 s | 7 |
| Five-year Vacation report | 0.021 s | 7 | 0.024 s | 7 |
| Physician detail | 0.031 s | 26 | 0.033 s | 26 |
| Physician history | 0.019 s | 8 | 0.017 s | 8 |

Every cold request returned the expected successful HTTP status. Ordinary page and report
latency was comfortably interactive; the slowest non-solver request was Work Remaining at
0.509 seconds.

The query counts on Work Remaining and the Precepting calendar/list are disproportionately
high even though the historical staging host served them quickly. They justify a separate
query-shaping pass before substantially larger datasets or slower hosted databases, but they
do not currently justify delaying use of the application.

## Schedule Bot

| Solve range | Cold wall | Setup | Solve | Persistence | Queries | Warm wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 month | 6.905 s | 0.682 s | 5.952 s | 0.252 s | 1,800 | 5.519 s |
| 3 months | 6.135 s | 0.728 s | 4.954 s | 0.433 s | 2,011 | 6.218 s |
| 6 months | 8.738 s | 0.792 s | 6.961 s | 0.963 s | 2,409 | 8.296 s |

Warm phase timing was 0.653 / 4.661 / 0.183 seconds for one month, 0.711 / 5.046 /
0.440 seconds for three months, and 0.757 / 6.563 / 0.950 seconds for six months
(setup / solve / persistence). Warm query counts were 1,800, 2,011, and 2,286,
respectively.

All six solver samples produced reviewable proposals and the probe removed every marked proposal.
The one-month warm request met the roughly six-second preference, three months was 0.22 seconds
over it, and six months was 8.30 seconds warm. The six-month request remained bounded and usable.
One month being slower than three months in the cold sample is a search-shape/seed outlier and
demonstrates why this record retains both samples rather than treating range length as a timing
predictor.

Earlier attempts on this same synthetic setup exposed false fairness failure and an unbounded
six-month search. Those defects led to Goal 131. The accepted implementation now distinguishes
time limits from infeasibility, preserves feasible lexicographic incumbents, and solves this
untouched workload without weakening eligibility or fairness.

## Report smoke

A one-time authenticated staging smoke requested the same August 1, 2021 through August 31,
2026 custom range as both HTML and generated PDF for every schedule-report family. Each HTML
view retained the selected dates, each workload table contained rows, and each PDF exposed its
expected title, range, and physician summary:

| Report | HTML rows | PDF pages | PDF bytes |
| --- | ---: | ---: | ---: |
| Precepting | 19 | 2 | 13,862 |
| Community Precepting | 3 | 1 | 12,112 |
| Interviews | 16 | 1 | 12,456 |
| Academic Events | 16 | 1 | 12,740 |
| Normal Rounding | 16 | 1 | 12,465 |
| Casual Rounding | 1 | 1 | 11,981 |
| Vacation | 19 | 1 | 12,962 |

The stipend smoke crossed both seeded rate transitions. It verified every itemized row's
`hours × hourly rate = amount`, populated physician totals and one overall total, and extracted
the same rate and report sections from each PDF:

| Report month | Rate | Itemized shifts | Physician totals | Overall amount |
| --- | ---: | ---: | ---: | ---: |
| Community Preceptor, December 2024 | $82.50 | 8 | 3 | $2,640.00 |
| Community Preceptor, January 2025 | $90.00 | 11 | 3 | $3,960.00 |
| Late Doc, June 2025 | $72.50 | 5 | 3 | $362.50 |
| Late Doc, July 2025 | $80.00 | 4 | 2 | $320.00 |

This is presentation and arithmetic evidence for the deterministic synthetic scenario, not an
assertion that the seeded rates represent real clinic compensation policy.

## Conclusion

The five-year synthetic data volume was comfortably usable on the measured host. All ordinary
interactive views completed in at most 0.515 seconds, the long-range HTML reports in at most
0.174 seconds, and every PDF/report arithmetic smoke check passed. No accepted-commit probe
request failed.

Schedule Bot is also usable at the intended human-triggered frequency: one month was 5.52 seconds
warm, three months 6.22 seconds, and six months 8.30 seconds. The six-month case misses the rough
six-second preference but no longer hangs or mislabels a bounded search as unfair. Further solver
optimization is optional rather than a release blocker.

A separate query-shaping Goal is justified by the 595–984 database queries used by Work Remaining
and the Precepting calendar/list despite their sub-second latency. That work should preserve the
current projections and findings exactly; this baseline does not justify caching away correctness,
weakening solver policy, or adding a permanent benchmark service or deployment gate.

## Collection method

The accepted `scripts/probe-staging-performance` command produced the cold and warm HTTP/query
samples. A one-time authenticated collector exercised the HTML and PDF report endpoints and
verified stipend arithmetic. A read-only staging-Django collector recorded PostgreSQL size,
model counts, lead-time policy, future-open invariants, and Work Remaining totals. Finally,
systemd/process inspection tied the running service to the exact release commit and recorded its
host context. The raw JSON outputs were checked before this durable summary was committed.
Those private collection artifacts are not required public inputs; this summary
retains the reported results rather than claiming to supply a replayable capture.
The collection description is not an instruction to deploy, reset, or probe a
live service during source cleanup. Any separately authorized new measurement
must follow the current staging guide and report its own revision and results.
