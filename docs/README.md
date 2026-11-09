# Documentation map

This directory contains Muriel's maintained product, architecture, operations,
and historical-audit records. The repository [README](../README.md) is the
starting point for local development, demonstration data, accounts, and checks.

## Current product and architecture

- [Canonical product and domain design](design.md) records accepted behavior,
  deliberate deferrals, and open domain questions.
- [Scheduling service owners](scheduling-services.md) names the current model,
  form, view, service, and presentation boundaries.
- [Precepting solver operations](precepting-solver.md) describes the current
  human-reviewed Schedule Bot workflow and solver policy.
- [Synthetic staging process](staging.md) defines isolated staging operation
  and the opt-in representative performance probes.

## Production operation

- [Production process](production.md) defines the application-side runtime
  contract.
- [Ubuntu host bootstrap](host-bootstrap.md) prepares the commissioned host.
- [Deployment and service operation](deployment.md) covers routine releases and
  service diagnosis.
- [PostgreSQL backup and recovery](backup-recovery.md) covers backups, restore
  drills, and deliberate recovery.
- [Tailscale Funnel public edge](public-edge.md) covers public access and the
  private Django Admin route.
- [Password recovery and delivery](password-recovery.md) covers account
  recovery, invitations, and SMTP configuration.

## Current architecture health

- [Application architecture health roadmap](audits/application-architecture-health-roadmap.md)
  is the maintained current implementation reconciliation, including open
  findings, deliberate deferrals, and the disposition of dated source audits.
- [Domain and application ownership health audit](audits/domain-application-health.md)
  retains its dated source evidence for scheduling ownership and persistence,
  with a current reconciliation of findings since that audit.
- [Django web and presentation health audit](audits/django-web-presentation-health.md)
  retains dated source evidence for HTTP and presentation boundaries.

## Historical audits

These audits retain earlier evidence while labeling their disposition;
they are not alternative architecture specifications. Private pre-export
Goal/Task/revision identifiers are informative provenance, not required public
links or Git objects. Dated results do not verify a new public revision;
sanitized command examples and current reproduction guidance are labeled
separately from recorded results.

- [Default Check performance profile](audits/default-check-performance-profile.md)
  records dated warm phase timing, expensive-test ownership, and the subsequent
  placement of ordinary and explicit integration coverage, with portable
  guidance for new verification.
- [Vacation Calendar import readiness audit](audits/vacation-calendar-import-readiness.md)
  retains the August 28, 2026 inspected-workbook disposition, limitations, and
  verification evidence; it is not a current production-readiness certification.
- [Five-year synthetic staging performance](audits/five-year-staging-performance-2026-08-28.md)
  retains August 28, 2026 deployment measurements, including historical
  six-month solver diagnostics that predate the current solve cap.
- [Scheduling architecture roadmap](audits/architecture-roadmap.md) is a
  reconciled point-in-time roadmap.
- [Scheduling domain ownership audit](audits/scheduling-domain-ownership.md) is
  a reconciled point-in-time audit.
- [Presentation and workspace architecture audit](audits/presentation-workspace-architecture.md)
  is a completed point-in-time recommendation.
- [Calendar presentation variants audit](audits/calendar-presentation-variants.md)
  retains point-in-time presentation evidence.
