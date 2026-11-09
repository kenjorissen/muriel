"""Scheduler-facing solve and review workflow for retained Precepting proposals."""

import re
from datetime import date, timedelta
from time import perf_counter

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import floatformat
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST

from ..academic_years import academic_year_for_date
from ..calendar_presentation import (
    AssignmentPresentation,
    AssignmentSlotPresentation,
    CalendarAction,
    CalendarDay,
    CalendarItem,
    CalendarLane,
    CalendarLattice,
    CalendarStatusPresentation,
    CalendarWeek,
    FindingPresentation,
    finding_tone,
    finding_tone_from_counts,
    slot_state_for_target,
)
from ..forms.solver import LateDocReviewForm, PreceptingSolveForm, ProposalAssignmentForm
from ..models import LateDocAssignment, PreceptorAssignment
from ..models.physicians import Physician
from ..models.solver import PreceptingSolverProposal
from ..services.holidays import holiday_closure_for
from ..services.operational_attention import is_operational_date, organization_today
from ..services.precepting_solver import (
    HardFindingConfirmationRequired,
    PreceptingSolverTimeout,
    SolveDateRange,
    apply_precepting_solver_proposal,
    balance_window_for_span,
    clip_solve_range_at_today,
    compare_fill_proposal,
    custom_solve_range,
    edit_proposed_assignment,
    effective_proposal_assignments,
    month_solve_range,
    new_solve_seed,
    preflight_precepting_solve,
    preview_proposal_reconciliation,
    produce_fill_proposal,
    produce_repair_proposal,
    proposal_assignment_context,
    quarter_solve_range,
    reconcile_precepting_solver_proposal,
    retain_fill_proposal,
    retain_proposal_comparison,
    review_proposed_late_doc,
    validate_precepting_solver_capacity,
    without_excluded_physicians,
)
from ..services.preceptors import (
    draft_finding_message,
    initialize_preceptor_session_range,
)
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableLink,
    DataTableRow,
)
from ._shared import _requested_month
from .preceptors import preceptor_workspace_navigation, week_calendar_lattice


@login_required
def solver_workspace(request):
    initial = _solve_initial()
    reused_proposal = None
    requested_month = _requested_month(request.GET.get("month"))
    if (
        request.method == "GET"
        and request.GET.get("range_kind") == PreceptingSolveForm.RANGE_MONTH
        and requested_month
    ):
        initial.update(range_kind=PreceptingSolveForm.RANGE_MONTH, month=requested_month)
    if request.method == "GET" and request.GET.get("reuse"):
        reused_proposal = get_object_or_404(
            PreceptingSolverProposal.objects.only("pk", "solve_settings"),
            pk=request.GET["reuse"],
        )
        initial.update(_reused_solve_initial(reused_proposal))
    form = PreceptingSolveForm(request.POST or None, initial=initial)
    preflight = None
    selected_solve_range = _initial_solve_range(initial)
    solve_range = clip_solve_range_at_today(selected_solve_range) or selected_solve_range
    balance_window = _initial_balance_window(initial)
    if request.method == "POST" and form.is_valid():
        try:
            timing = {} if _is_staging_performance_probe(request) else None
            started = perf_counter()
            selected_solve_range = _solve_range(form.cleaned_data)
            balance_window = balance_window_for_span(
                selected_solve_range,
                form.cleaned_data["balance_span"],
                custom_days=form.cleaned_data.get("balance_custom_days"),
            )
            solve_range = clip_solve_range_at_today(selected_solve_range)
            if solve_range is None:
                solve_range = selected_solve_range
                raise ValidationError("No current or future dates are selected.")
            validate_precepting_solver_capacity(solve_range, balance_window=balance_window)
            initialize_preceptor_session_range(solve_range.start_date, solve_range.end_date)
            preflight = preflight_precepting_solve(solve_range, balance_window=balance_window)
            if timing is not None:
                timing["setup"] = perf_counter() - started
            if preflight.blockers:
                form.add_error(
                    None, "Resolve the named schedule blockers before running Schedule Bot."
                )
            else:
                excluded_ids = tuple(
                    physician.pk for physician in form.cleaned_data["excluded_physician_ids"]
                )
                solver_input = without_excluded_physicians(preflight.solver_input, excluded_ids)
                solve_seed = new_solve_seed()
                started = perf_counter()
                fill_proposal = (
                    produce_repair_proposal(solver_input, solve_seed=solve_seed)
                    if form.cleaned_data["mode"] == PreceptingSolverProposal.Mode.REPAIR
                    else produce_fill_proposal(solver_input, solve_seed=solve_seed)
                )
                if timing is not None:
                    timing["solve"] = perf_counter() - started
                started = perf_counter()
                proposal = retain_fill_proposal(
                    solver_input=solver_input,
                    fill_proposal=fill_proposal,
                    scheduler=request.user,
                    policy_version="late-doc-band-v1",
                    mode=form.cleaned_data["mode"],
                    solve_settings={
                        **_solve_settings(form.cleaned_data),
                        "solve_seed": solve_seed,
                        **({"staging_performance_probe": True} if timing is not None else {}),
                    },
                )
                response = redirect("precepting-solver-review", proposal_id=proposal.pk)
                if timing is not None:
                    timing["persistence"] = perf_counter() - started
                    response["X-Muriel-Performance-Probe-Proposal-ID"] = str(proposal.pk)
                    response["Server-Timing"] = ", ".join(
                        f"{phase};dur={seconds * 1000:.3f}" for phase, seconds in timing.items()
                    )
                return response
        except (ValidationError, PreceptingSolverTimeout) as error:
            form.add_error(None, error)
    return render(
        request,
        "schedules/precepting_solver_workspace.html",
        {
            **_workspace_context("solve", request.user),
            "form": form,
            "preflight": preflight,
            "show_feedback": request.method == "POST"
            and bool(form.errors or (preflight and preflight.blockers)),
            "solve_range": solve_range,
            "balance_window": balance_window,
            "organization_today": organization_today(),
            **_proposal_listing_context(request),
            "reused_proposal": reused_proposal,
        },
    )


@login_required
@require_POST
def solver_performance_probe_cleanup(request, proposal_id):
    """Remove only the temporary proposal made by the fixed staging probe."""
    if not _is_staging_performance_probe(request):
        raise Http404
    proposal = get_object_or_404(PreceptingSolverProposal, pk=proposal_id)
    if (
        proposal.created_by_id != request.user.pk
        or proposal.applied_at is not None
        or not proposal.solve_settings.get("staging_performance_probe")
    ):
        return HttpResponseForbidden()
    proposal.delete()
    return HttpResponse(status=204)


def _is_staging_performance_probe(request):
    return (
        getattr(settings, "STAGING_ENVIRONMENT_MARKER", None) == "STAGING — SYNTHETIC DATA"
        and request.headers.get("X-Muriel-Performance-Probe") == "staging-performance-v1"
    )


@login_required
def solver_review(request, proposal_id):
    proposal = _proposal(proposal_id)
    current, stale_message = _proposal_currentness(proposal)
    reconciliation_preview = None
    if not current and proposal.applied_at is None:
        try:
            reconciliation_preview = preview_proposal_reconciliation(proposal)
        except ValidationError:
            pass
    if request.GET.get("conflict") == "revision":
        stale_message = (
            "This proposal changed while you were editing it. "
            "Your work was not saved; review the refreshed proposal."
        )
    calendar_view = request.GET.get("view", "proposed")
    if calendar_view not in {"changes", "proposed", "current"}:
        return HttpResponseBadRequest("Review view must be changes, proposed, or current.")
    return render(
        request,
        "schedules/precepting_solver_review.html",
        {
            **_workspace_context("solve", request.user),
            "proposal": proposal,
            "review_groups": _review_groups(proposal),
            **_finding_review_context(proposal),
            "current": current,
            "late_doc_ready": not proposal.late_doc_reviews.filter(decision="pending").exists(),
            "stale_message": stale_message,
            "reconciliation_preview": reconciliation_preview,
            "reconciliation": getattr(proposal, "reconciliation", None),
            "calendar_view": calendar_view,
            "calendar": _proposal_calendar(proposal, proposed=calendar_view == "proposed")
            if calendar_view in {"proposed", "current"}
            else None,
            "proposal_workload_table": _proposal_workload_table(proposal),
            **_comparison_context(proposal),
        },
    )


@login_required
def solver_reconcile(request, proposal_id):
    proposal = _proposal(proposal_id)
    current, stale_message = _proposal_currentness(proposal)
    if current or proposal.applied_at is not None:
        return redirect("precepting-solver-review", proposal_id=proposal.pk)
    try:
        preview = preview_proposal_reconciliation(proposal)
    except ValidationError as error:
        return render(
            request,
            "schedules/precepting_solver_review.html",
            _review_context(proposal, request.user, stale_message=str(error)),
        )
    conflicts = _present_reconciliation_conflicts(preview)
    if request.method == "POST":
        try:
            reconciled = reconcile_precepting_solver_proposal(
                proposal=proposal,
                scheduler=request.user,
                expected_revision=int(request.POST["revision"]),
                expected_current_input_fingerprint=request.POST["current_input_fingerprint"],
                choices={
                    conflict["choice_key"]: request.POST.get(conflict["choice_key"])
                    for conflict in conflicts
                },
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            stale_message = str(error)
        else:
            return redirect("precepting-solver-review", proposal_id=reconciled.pk)
    return render(
        request,
        "schedules/precepting_solver_reconcile.html",
        {
            **_workspace_context("solve", request.user),
            "proposal": proposal,
            "preview": preview,
            "conflicts": conflicts,
            "conflict_groups": _reconciliation_conflict_groups(conflicts),
            "stale_message": stale_message,
        },
    )


@login_required
def solver_proposal_week(request, proposal_id, on_date):
    proposal = _proposal(proposal_id)
    try:
        week_start = _week_start(on_date)
    except ValueError:
        raise Http404("Proposal week must use YYYY-MM-DD.") from None
    if not _proposal_contains_week(proposal, week_start):
        raise Http404("This week is not part of the proposal range.")
    current, stale_message = _proposal_currentness(proposal)
    if request.GET.get("conflict") == "revision":
        stale_message = (
            "This proposal changed while you were editing it. "
            "Your work was not saved; review the refreshed proposal."
        )
    return render(
        request,
        "schedules/precepting_solver_week.html",
        {
            **_workspace_context("solve", request.user),
            "proposal": proposal,
            "week_start": week_start,
            "week_end": week_start + timedelta(days=4),
            "current": current and proposal.applied_at is None,
            "stale_message": stale_message,
            "calendar": _proposal_week_calendar(
                proposal,
                week_start,
                editable=current and proposal.applied_at is None,
            ),
        },
    )


@login_required
def solver_late_doc_review(request, proposal_id, on_date):
    proposal = _proposal(proposal_id)
    try:
        review_date = date.fromisoformat(on_date)
    except ValueError:
        raise Http404("Late Doc date must be YYYY-MM-DD.") from None
    review = get_object_or_404(proposal.late_doc_reviews, date=review_date)
    try:
        candidates = _late_doc_candidates(proposal, review_date)
    except ValidationError:
        candidates = Physician.objects.filter(pk=review.proposed_physician_id)
    form = LateDocReviewForm(
        request.POST or None,
        candidates=candidates,
        initial={"decision": review.decision, "revision": proposal.revision},
    )
    if request.method == "POST" and form.is_valid():
        try:
            review_proposed_late_doc(
                proposal=proposal,
                on_date=review_date,
                decision=form.cleaned_data["decision"],
                replacement_physician=form.cleaned_data["replacement_physician"],
                scheduler=request.user,
                expected_revision=form.cleaned_data["revision"],
            )
        except ValidationError as error:
            if any("changed while you were editing" in message for message in error.messages):
                return redirect(_proposal_edit_return_url(request, proposal, conflict=True))
            form.add_error(None, error)
        else:
            return_url = _proposal_edit_return_url(request, proposal)
            review_url = reverse("precepting-solver-review", args=(proposal.pk,))
            return redirect(
                f"{return_url}#late-doc-review" if return_url == review_url else return_url
            )
    return render(
        request,
        "schedules/precepting_solver_late_doc_review.html",
        {
            **_workspace_context("solve", request.user),
            "proposal": proposal,
            "review": review,
            "form": form,
        },
    )


@login_required
def solver_assignment_edit(request, proposal_id, assignment_type, on_date, period_of_day, position):
    proposal = _proposal(proposal_id)
    try:
        edit_date = date.fromisoformat(on_date)
        context = proposal_assignment_context(
            proposal=proposal,
            assignment_type=assignment_type,
            on_date=edit_date,
            period_of_day="" if period_of_day == "late-doc" else period_of_day,
            position=position,
        )
    except ValidationError as error:
        return render(
            request,
            "schedules/precepting_solver_review.html",
            _review_context(proposal, request.user, stale_message=str(error)),
        )
    initial = {
        "revision": proposal.revision,
        "physician": context["value"].get("physician_id") if context["value"] else None,
    }
    form = ProposalAssignmentForm(
        request.POST or None,
        alternatives=context.get("alternatives"),
        candidates=context.get("candidates"),
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            edit_proposed_assignment(
                proposal=proposal,
                assignment_type=assignment_type,
                on_date=edit_date,
                period_of_day="" if period_of_day == "late-doc" else period_of_day,
                position=position,
                physician=form.cleaned_data["physician"],
                note=form.cleaned_data["note"],
                scheduler=request.user,
                expected_revision=form.cleaned_data["revision"],
            )
        except ValidationError as error:
            if any("changed while you were editing" in message for message in error.messages):
                return redirect(_proposal_edit_return_url(request, proposal, conflict=True))
            form.add_error(None, error)
        else:
            return redirect(_proposal_edit_return_url(request, proposal))
    return render(
        request,
        "schedules/precepting_solver_assignment_edit.html",
        {
            **_workspace_context("solve", request.user),
            "proposal": proposal,
            "form": form,
            "assignment_type": assignment_type,
            "on_date": edit_date,
            "period_of_day": period_of_day,
            "position": position,
            "position_label": _position_label(position),
            "current_value": context["value"],
            "alternatives": context.get("alternatives"),
            "return_url": _proposal_edit_return_url(request, proposal),
        },
    )


@login_required
def solver_compare(request, proposal_id):
    proposal = _proposal(proposal_id)
    if request.method != "POST":
        return redirect("precepting-solver-review", proposal_id=proposal.pk)
    try:
        assignment = _stored_assignment(proposal, request.POST)
        solver_input = _proposal_solver_input(proposal)
        fresh = produce_fill_proposal(
            solver_input, solve_seed=proposal.solve_settings.get("solve_seed")
        )
        selected = _matching_assignment(fresh, assignment)
        alternative_id = int(request.POST["alternative_physician_id"])
        comparison = compare_fill_proposal(
            solver_input, fresh, selected, alternative_physician_id=alternative_id
        )
        retain_proposal_comparison(proposal=proposal, comparison=comparison, scheduler=request.user)
    except (KeyError, TypeError, ValueError, ValidationError, PreceptingSolverTimeout) as error:
        # Preserve inspectability and show the current review rather than accepting a malformed choice.
        context = _review_context(proposal, request.user, stale_message=str(error))
        context["current"] = False
        return render(
            request,
            "schedules/precepting_solver_review.html",
            context,
        )
    return redirect("precepting-solver-review", proposal_id=proposal.pk)


@login_required
def solver_apply(request, proposal_id):
    proposal = _proposal(proposal_id)
    if request.method != "POST":
        return redirect("precepting-solver-review", proposal_id=proposal.pk)
    try:
        confirm_hard_findings = request.POST.get("confirm_hard_findings") == "1"
        expected_revision = request.POST.get("revision") if confirm_hard_findings else None
        if expected_revision is not None:
            expected_revision = int(expected_revision)
        apply_precepting_solver_proposal(
            proposal=proposal,
            scheduler=request.user,
            confirm_hard_findings=confirm_hard_findings,
            expected_revision=expected_revision,
            hard_finding_reason=request.POST.get("reason", ""),
            selected_hard_finding_keys=request.POST.getlist("acknowledge_finding"),
        )
    except HardFindingConfirmationRequired as error:
        return render(
            request,
            "schedules/precepting_solver_review.html",
            {
                **_review_context(proposal, request.user),
                "hard_finding_confirmation": {
                    "revision": proposal.revision,
                    "findings": _present_confirmation_findings(
                        proposal, error.findings, error.finding_keys
                    ),
                },
            },
        )
    except (TypeError, ValueError, ValidationError) as error:
        return render(
            request,
            "schedules/precepting_solver_review.html",
            _review_context(proposal, request.user, stale_message=str(error)),
        )
    return redirect(f"{reverse('monthly-workspace')}?month={proposal.solve_start_date:%Y-%m}")


def _solve_initial():
    today = organization_today()
    month = today.replace(day=1)
    return {
        "mode": "fill",
        "range_kind": "month",
        "month": month,
        "academic_year": academic_year_for_date(today),
        "quarter": ((today.month - 7) % 12) // 3 + 1,
        "balance_span": "6-months",
    }


def _reused_solve_initial(proposal):
    """Return only the stored setup controls; opening a proposal remains the stale check."""
    settings = proposal.solve_settings
    initial = {
        key: settings[key]
        for key in (
            "mode",
            "range_kind",
            "month",
            "academic_year",
            "quarter",
            "start_date",
            "end_date",
            "balance_span",
            "balance_custom_days",
            "excluded_physician_ids",
        )
        if key in settings and settings[key] is not None
    }
    for key in ("month", "start_date", "end_date"):
        if key in initial:
            initial[key] = date.fromisoformat(initial[key])
    return initial


def _solve_settings(values):
    """Keep the setup choices that led to this artifact, separately from canonical dates."""
    return {
        "mode": values["mode"],
        "range_kind": values["range_kind"],
        "month": values["month"].isoformat() if values.get("month") else None,
        "academic_year": values.get("academic_year"),
        "quarter": int(values["quarter"]) if values.get("quarter") else None,
        "start_date": values["start_date"].isoformat() if values.get("start_date") else None,
        "end_date": values["end_date"].isoformat() if values.get("end_date") else None,
        "balance_span": values["balance_span"],
        "balance_custom_days": values.get("balance_custom_days"),
        "excluded_physician_ids": [physician.pk for physician in values["excluded_physician_ids"]],
    }


def _initial_solve_range(initial):
    if initial["range_kind"] == "month":
        return month_solve_range(initial["month"])
    if initial["range_kind"] == "quarter":
        return quarter_solve_range(initial["academic_year"], int(initial["quarter"]))
    return custom_solve_range(initial["start_date"], initial["end_date"])


def _solve_range(values):
    if values["range_kind"] == "month":
        return month_solve_range(values["month"])
    if values["range_kind"] == "quarter":
        return quarter_solve_range(values["academic_year"], int(values["quarter"]))
    return custom_solve_range(values["start_date"], values["end_date"])


def _initial_balance_window(initial):
    return balance_window_for_span(
        _initial_solve_range(initial),
        initial["balance_span"],
        custom_days=initial.get("balance_custom_days"),
    )


def _finding_review_context(proposal):
    after_finding_counts = _finding_counts(proposal.after_findings)
    return {
        "before_finding_counts": _finding_counts(proposal.before_findings),
        "after_finding_counts": after_finding_counts,
        "unresolved_work_tone": finding_tone_from_counts(
            after_finding_counts["hard"], after_finding_counts["preference"]
        ),
        "unresolved_finding_count": len(proposal.before_findings) + len(proposal.after_findings),
        "before_findings": _present_findings(proposal.before_findings),
        "after_findings": _present_after_findings(proposal),
    }


def _finding_counts(findings):
    return {
        severity: sum(finding["severity"] == severity for finding in findings)
        for severity in ("hard", "preference")
    }


def _proposal(proposal_id):
    return get_object_or_404(
        PreceptingSolverProposal.objects.select_related(
            "created_by", "applied_by", "reconciliation", "reconciliation__source_proposal"
        ).prefetch_related(
            "late_doc_reviews__proposed_physician",
            "late_doc_reviews__replacement_physician",
            "edits__edited_by",
        ),
        pk=proposal_id,
    )


def _proposal_solver_input(proposal):
    result = preflight_precepting_solve(
        SolveDateRange(proposal.solve_start_date, proposal.solve_end_date),
        balance_window=SolveDateRange(proposal.balance_start_date, proposal.balance_end_date),
    )
    if result.blockers:
        raise ValidationError("The proposal input is no longer ready to solve.")
    return without_excluded_physicians(
        result.solver_input, proposal.solve_settings.get("excluded_physician_ids", ())
    )


def _proposal_currentness(proposal):
    if proposal.applied_at:
        return True, "Applied to the ordinary draft."
    try:
        solver_input = _proposal_solver_input(proposal)
    except ValidationError as error:
        return False, str(error)
    if solver_input.fingerprint != proposal.input_fingerprint:
        return False, "This proposal is stale. Reconcile it before applying."
    return True, ""


def _present_reconciliation_conflicts(preview):
    return tuple(
        {
            **conflict,
            "choice_key": ":".join(conflict["key"]),
            "position_label": (
                "Late Doc"
                if conflict["assignment_type"] == "late_doc"
                else _position_label(conflict["position"])
            ),
            "current_name": (conflict["current"] or {}).get(
                "physician_name",
                "Unfilled Late Doc" if conflict["assignment_type"] == "late_doc" else "Unfilled",
            ),
            "proposed_name": (conflict["proposed"] or {}).get(
                "physician_name",
                "Unfilled Late Doc" if conflict["assignment_type"] == "late_doc" else "Unfilled",
            ),
        }
        for conflict in preview.conflicts
    )


def _reconciliation_conflict_groups(conflicts):
    groups = {}
    for conflict in conflicts:
        groups.setdefault(_week_start(conflict["date"]), []).append(conflict)
    return tuple((week, tuple(rows)) for week, rows in sorted(groups.items()))


def _review_groups(proposal):
    if proposal.mode == PreceptingSolverProposal.Mode.REPAIR:
        return _repair_review_groups(proposal)
    groups = {}
    baseline = _baseline_slots(proposal)
    precepting_assignments, _ = effective_proposal_assignments(proposal)
    for assignment in precepting_assignments:
        current = baseline.get(
            (assignment["date"], assignment["period_of_day"], assignment["position"])
        )
        groups.setdefault(_week_start(assignment["date"]), []).append(
            {
                **assignment,
                "position_label": _position_label(assignment["position"]),
                "kind": _change_kind(current, assignment.get("physician_name")),
                "alternative_count": _alternative_count(assignment.get("explanation", {})),
                "current_physician_name": _display_assignment_name(
                    "precepting", assignment["position"], current
                ),
                "finding": None,
                "manual": bool(assignment.get("explanation", {}).get("manual")),
                "edit_url": _proposal_edit_url(
                    proposal,
                    "precepting",
                    assignment["date"],
                    assignment["period_of_day"],
                    assignment["position"],
                ),
            }
        )
    for assignment in _effective_late_doc_assignments(proposal):
        current = baseline.get((assignment["date"], "Late Doc", "late-doc"))
        groups.setdefault(_week_start(assignment["date"]), []).append(
            {
                **assignment,
                "kind": _change_kind(current, assignment.get("physician_name")),
                "alternative_count": _alternative_count(assignment.get("explanation", {})),
                "position": "Late Doc",
                "period_of_day": "Late Doc",
                "current_physician_name": _display_assignment_name("late_doc", "late-doc", current),
                "finding": None,
                "manual": bool(assignment.get("explanation", {}).get("manual")),
                "edit_url": _proposal_edit_url(
                    proposal, "late_doc", assignment["date"], "late-doc", "late-doc"
                ),
            }
        )
    for finding in _present_after_findings(proposal):
        groups.setdefault(_week_start(finding["date"].isoformat()), []).append(
            {
                "kind": "Finding",
                "date": finding["date"],
                "period_of_day": finding["period_of_day"] or "Week",
                "position": finding["position_label"] or "",
                "current_physician_name": "-",
                "physician_name": "-",
                "alternative_count": 0,
                "explanation": {},
                "finding": finding,
            }
        )
    return tuple((week, tuple(rows)) for week, rows in sorted(groups.items()))


def _change_kind(current, proposed):
    if current and not proposed:
        return "Removal"
    if current:
        return "Replacement"
    return "Addition"


def _display_assignment_name(assignment_type, position, physician_name):
    if physician_name:
        return physician_name
    if assignment_type == "late_doc":
        return "Unfilled Late Doc"
    return "Unfilled" if position.startswith("primary-") else "-"


def _repair_review_groups(proposal):
    """Show every in-range baseline assignment so retained repair work is auditable."""
    groups = {}
    changes = _effective_proposal_changes_for_review(proposal)
    for key, baseline in _repair_baseline_assignment_values(proposal).items():
        assignment_type, on_date, period_of_day, position = key
        proposed = changes.get(key, baseline)
        if baseline is None and proposed is None:
            continue
        current_name = _display_assignment_name(
            assignment_type, position, baseline.get("physician_name") if baseline else None
        )
        proposed_name = _display_assignment_name(
            assignment_type, position, proposed.get("physician_name") if proposed else None
        )
        kind = (
            "Retained"
            if _same_assignment_value(baseline, proposed)
            else "Removed"
            if proposed is None
            else "Added"
            if baseline is None
            else "Moved"
        )
        value = proposed or _removal_value(key, proposal)
        groups.setdefault(_week_start(on_date), []).append(
            {
                **value,
                "date": on_date,
                "period_of_day": "Late Doc" if assignment_type == "late_doc" else period_of_day,
                "position": "Late Doc" if assignment_type == "late_doc" else position,
                "position_label": "Late Doc"
                if assignment_type == "late_doc"
                else _position_label(position),
                "kind": kind,
                "alternative_count": _alternative_count(value.get("explanation", {})),
                "current_physician_name": current_name,
                "physician_name": proposed_name,
                "finding": None,
                "manual": False,
                "edit_url": (
                    ""
                    if baseline and baseline.get("is_locked")
                    else _proposal_edit_url(
                        proposal,
                        assignment_type,
                        on_date,
                        "late-doc" if assignment_type == "late_doc" else period_of_day,
                        "late-doc" if assignment_type == "late_doc" else position,
                    )
                ),
            }
        )
    for finding in _present_after_findings(proposal):
        groups.setdefault(_week_start(finding["date"].isoformat()), []).append(
            {
                "kind": "Finding",
                "date": finding["date"],
                "period_of_day": finding["period_of_day"] or "Week",
                "position": finding["position_label"] or "",
                "current_physician_name": "-",
                "physician_name": "-",
                "alternative_count": 0,
                "explanation": {},
                "finding": finding,
            }
        )
    return tuple((week, tuple(rows)) for week, rows in sorted(groups.items()))


def _effective_proposal_changes_for_review(proposal):
    changes = {}
    for assignment_type in ("precepting", "late_doc"):
        for assignment in proposal.proposed_assignments.get(assignment_type, ()):
            changes[
                (
                    assignment_type,
                    assignment["date"],
                    assignment.get("period_of_day", ""),
                    assignment.get("position", "late-doc"),
                )
            ] = assignment if assignment.get("physician_id") is not None else None
    for edit in proposal.edits.all():
        changes[
            (edit.assignment_type, edit.date.isoformat(), edit.period_of_day, edit.position)
        ] = edit.resulting_value
    return changes


def _repair_baseline_assignment_values(proposal):
    values = {
        ("precepting", session["date"], session["period_of_day"], position["key"]): position[
            "assignment"
        ]
        for session in proposal.input_snapshot["sessions"]
        for position in session["positions"]
    }
    values.update(
        {
            ("late_doc", item["date"], "", "late-doc"): item["assignment"]
            for item in proposal.input_snapshot["late_doc_positions"]
        }
    )
    return values


def _removal_value(key, proposal):
    assignment_type, on_date, period_of_day, position = key
    return next(
        (
            assignment
            for assignment in proposal.proposed_assignments.get(assignment_type, ())
            if assignment["date"] == on_date
            and assignment.get("period_of_day", "") == period_of_day
            and assignment.get("position", "late-doc") == position
        ),
        {"explanation": {}},
    )


def _same_assignment_value(left, right):
    return (left or {}).get("physician_id") == (right or {}).get("physician_id")


def _alternative_count(explanation):
    return sum(
        len(explanation.get(category, ()))
        for category in (
            "equally_good_alternatives",
            "other_normally_available_alternatives",
            "backup_only_alternatives",
        )
    )


def _week_start(value):
    on_date = date.fromisoformat(value)
    return on_date - timedelta(days=on_date.weekday())


def _late_doc_candidates(proposal, on_date):
    return proposal_assignment_context(
        proposal=proposal,
        assignment_type="late_doc",
        on_date=on_date,
        period_of_day="",
        position="late-doc",
    )["candidates"]


def _stored_assignment(proposal, values):
    assignments = proposal.proposed_assignments.get(values["assignment_type"], ())
    position = (
        "late-doc" if values.get("position") == "Late Doc" else values.get("position", "late-doc")
    )
    for assignment in assignments:
        if (
            assignment["date"] == values["date"]
            and assignment.get("period_of_day", "") == values.get("period_of_day", "")
            and assignment.get("position", "late-doc") == position
        ):
            return {**assignment, "position": position}
    raise ValidationError("Choose an assignment from this proposal.")


def _matching_assignment(proposal, stored):
    for assignment in proposal.assignments + proposal.late_doc_assignments:
        if (
            assignment.date.isoformat() == stored["date"]
            and getattr(assignment, "period_of_day", None) == stored.get("period_of_day")
            and getattr(assignment, "position", "late-doc") == stored.get("position", "late-doc")
        ):
            return assignment
    raise ValidationError("The selected assignment is no longer part of this proposal.")


def _proposal_calendar(proposal, *, proposed):
    slots = (
        _proposal_effective_slots(proposal) if proposed else _baseline_assignment_values(proposal)
    )
    slots = {**_calendar_context_slots(proposal), **slots}
    proposed_slots = {
        key: assignment["physician_name"]
        for key, assignment in slots.items()
        if assignment and assignment.get("physician_name")
    }
    changes = _proposal_changes(proposal)
    changed_slots = {
        (on_date, period_of_day, position)
        for assignment_type, on_date, period_of_day, position in changes
        if assignment_type == "precepting"
    }.union(
        {
            (on_date, "Late Doc", "late-doc")
            for assignment_type, on_date, _, _ in changes
            if assignment_type == "late_doc"
        }
    )
    start = proposal.solve_start_date - timedelta(days=proposal.solve_start_date.weekday())
    end = proposal.solve_end_date + timedelta(days=4 - proposal.solve_end_date.weekday())
    findings_by_target = _calendar_findings_by_target(
        proposal.after_findings if proposed else proposal.before_findings,
        {
            key: assignment
            for key, assignment in proposed_slots.items()
            if proposal.solve_start_date <= date.fromisoformat(key[0]) <= proposal.solve_end_date
        },
        changed_slots=changed_slots,
        proposed=proposed,
    )
    weeks = []
    week = start
    while week <= end:
        week_days = tuple((offset, week + timedelta(days=offset)) for offset in range(5))
        days = tuple(
            CalendarDay(
                on_date.isoformat(),
                f"{on_date:%b} {on_date.day}",
                proposal.solve_start_date <= on_date <= proposal.solve_end_date,
            )
            for offset, on_date in week_days
        )
        lanes = tuple(
            CalendarLane(
                lane,
                tuple(
                    CalendarItem(
                        offset + 1,
                        1,
                        "schedules/calendar_solver_item.html",
                        _calendar_slot_content(
                            proposed_slots,
                            on_date.isoformat(),
                            lane,
                            changed_slots=changed_slots,
                            proposed=proposed,
                            proposal=proposal,
                            findings_by_target=findings_by_target,
                            actionable=proposal.solve_start_date
                            <= on_date
                            <= proposal.solve_end_date,
                        ),
                    )
                    for offset, on_date in week_days
                ),
                variant="compact" if lane == "Late Doc" else "",
                period="PM" if lane == "Late Doc" else lane,
            )
            for lane in ("AM", "PM", "Late Doc")
        )
        weeks.append(
            CalendarWeek(
                f"Week of {week:%B} {week.day}, {week.year}",
                days,
                lanes,
                action=(
                    CalendarAction(
                        _proposal_week_url(proposal, week),
                        f"Open proposed week from {week:%B} {week.day} to "
                        f"{week + timedelta(days=4):%B} {(week + timedelta(days=4)).day}, "
                        f"{(week + timedelta(days=4)).year}",
                        "Open week",
                        identifier=f"proposal-week-open-{week:%Y-%m-%d}",
                    )
                    if proposed
                    else None
                ),
                target=_week_target(week),
            )
        )
        week += timedelta(days=7)
    return CalendarLattice(
        label=("Proposed" if proposed else "Current") + " Precepting schedule",
        columns=("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
        variant="five-column",
        has_gutter=True,
        weeks=tuple(weeks),
        legend=(
            (
                CalendarStatusPresentation(
                    "proposal-change", "Delta", "Proposal change", "A proposed assignment change."
                ),
            )
            if proposed
            else ()
        ),
    )


def _proposal_week_calendar(proposal, week_start, *, editable):
    slots = {**_calendar_context_slots(proposal), **_proposal_effective_slots(proposal)}
    changed_slots = _proposal_changes(proposal)
    late_doc_dates = {item["date"] for item in proposal.input_snapshot["late_doc_positions"]}
    findings_by_target = _calendar_findings_by_target(
        proposal.after_findings,
        {
            key: assignment
            for key, assignment in slots.items()
            if proposal.solve_start_date <= date.fromisoformat(key[0]) <= proposal.solve_end_date
        },
        changed_slots={
            (on_date, period_of_day, position)
            for assignment_type, on_date, period_of_day, position in changed_slots
            if assignment_type == "precepting"
        }.union(
            {
                (on_date, "Late Doc", "late-doc")
                for assignment_type, on_date, _, _ in changed_slots
                if assignment_type == "late_doc"
            }
        ),
        proposed=True,
    )
    days = tuple(
        {
            "date": day,
            "actionable": proposal.solve_start_date <= day <= proposal.solve_end_date,
        }
        for offset in range(5)
        for day in (week_start + timedelta(days=offset),)
    )

    def assignment_content(day, session_index):
        on_date = day["date"].isoformat()
        actionable = proposal.solve_start_date <= day["date"] <= proposal.solve_end_date
        period = ("AM", "PM")[session_index]
        closed = holiday_closure_for(day["date"], period)
        session_target = _session_target(on_date, period)
        session_findings = tuple(findings_by_target.get(session_target, ())) if actionable else ()
        positions_for_session = ("primary-1", "primary-2", "secondary")
        if (
            actionable
            and closed
            and all(
                slots.get((on_date, period, position)) is None
                or ("precepting", on_date, period, position) in changed_slots
                for position in positions_for_session
            )
        ):
            return AssignmentPresentation(
                period=period,
                date_label=f"{day['date']:%A}, {day['date']:%b} {day['date'].day}",
                slots=(
                    AssignmentSlotPresentation(
                        "Closed",
                        "holiday-closed",
                        f"{period} assignments for {day['date']:%A, %B} {day['date'].day}, "
                        f"{day['date'].year}: Closed for {closed.name}, {closed.coverage_label}.",
                    ),
                ),
                faculty_coverage_conflict=False,
                findings=tuple(
                    FindingPresentation(finding["message"], finding["severity"])
                    for finding in session_findings
                ),
            )
        positions = []
        for position, label in (
            ("primary-1", "Preceptor 1"),
            ("primary-2", "Preceptor 2"),
            ("secondary", "Secondary"),
        ):
            target = _position_target(on_date, period, position)
            findings = tuple(findings_by_target.get(target, ())) if actionable else ()
            position_finding_tone = finding_tone(findings)
            assignment = slots.get((on_date, period, position))
            value = assignment.get("physician_name") if assignment else None
            changed = actionable and ("precepting", on_date, period, position) in changed_slots
            if actionable and closed and (value is None or changed):
                display, state = "Closed", "optional-empty"
            else:
                display = value or ("Unfilled" if position.startswith("primary-") else "-")
                state = (
                    "gap"
                    if actionable and value is None and position.startswith("primary-")
                    else _finding_state(findings)
                )
            if not actionable:
                state = "optional-empty"
            elif value is None and position == "secondary" and not closed:
                state = "optional-empty"
            changed = changed and not closed
            action = "Edit" if value else ("Assign" if state == "gap" else "Add")
            positions.append(
                AssignmentSlotPresentation(
                    _slot_display(display, state),
                    state,
                    f"{label}: "
                    f"{_proposal_delta_label(proposal) + ', ' if changed else ''}"
                    f"{'Hard finding: ' if position_finding_tone == 'hard' and state == 'gap' else ''}"
                    f"{_slot_display(display, state)}",
                    (
                        _proposal_edit_url(
                            proposal,
                            "precepting",
                            on_date,
                            period,
                            position,
                            week_start=week_start,
                        )
                        if editable and actionable and not closed
                        else ""
                    ),
                    (
                        f"{action} {label} for {period} on {day['date']:%B} "
                        f"{day['date'].day}, {day['date'].year}"
                        if editable and actionable and not closed
                        else ""
                    ),
                    changed=changed,
                    target=target,
                    delta_label=_proposal_delta_label(proposal),
                    finding_tone=position_finding_tone,
                )
            )
        return AssignmentPresentation(
            period=period,
            date_label=f"{day['date']:%A}, {day['date']:%b} {day['date'].day}",
            slots=tuple(positions),
            faculty_coverage_conflict=any(
                finding["code"] == "insufficient_faculty_equivalent_coverage"
                for finding in session_findings
            ),
            findings=tuple(
                FindingPresentation(finding["message"], finding["severity"])
                for finding in (
                    *session_findings,
                    *_holiday_position_findings(findings_by_target, on_date, period),
                )
            ),
        )

    def late_doc_content(day):
        on_date = day["date"].isoformat()
        actionable = proposal.solve_start_date <= day["date"] <= proposal.solve_end_date
        target = _position_target(on_date, "Late Doc", "late-doc")
        assignment = slots.get((on_date, "Late Doc", "late-doc"))
        changed = actionable and ("late_doc", on_date, "", "late-doc") in changed_slots
        closed = day["date"].weekday() == 0 and holiday_closure_for(day["date"], "PM")
        if actionable and closed and (assignment is None or changed):
            return {
                "display": "Closed",
                "state": "optional-empty",
                "accessible_label": (
                    f"Late Doc Monday 5-6 PM: Closed for {closed.name}, {closed.coverage_label}."
                ),
                "url": "",
                "action_label": "",
                "changed": False,
                "target": target,
                "delta_label": _proposal_delta_label(proposal),
            }
        if on_date not in late_doc_dates and assignment is None:
            return None
        findings = tuple(findings_by_target.get(target, ())) if actionable else ()
        late_doc_finding_tone = finding_tone(findings)
        value = assignment.get("physician_name") if assignment else None
        display = value or "Unfilled"
        state = "gap" if actionable and value is None else _finding_state(findings)
        if not actionable:
            state = "optional-empty"
        action = "Edit" if value else "Assign"
        return {
            "display": _slot_display(display, state),
            "state": state,
            "finding_tone": late_doc_finding_tone,
            "accessible_label": (
                f"Late Doc Monday 5-6 PM: "
                f"{_proposal_delta_label(proposal) + ', ' if changed else ''}"
                f"{'Hard finding: ' if late_doc_finding_tone == 'hard' and state == 'gap' else ''}"
                f"{_slot_display(display, state)}"
            ),
            "url": (
                _proposal_edit_url(
                    proposal,
                    "late_doc",
                    on_date,
                    "late-doc",
                    "late-doc",
                    week_start=week_start,
                )
                if editable and actionable and not closed
                else ""
            ),
            "action_label": (
                f"{action} Late Doc for Monday {day['date']:%B} "
                f"{day['date'].day}, {day['date'].year}"
                if editable and actionable and not closed
                else ""
            ),
            "changed": changed and not closed,
            "target": target,
            "delta_label": _proposal_delta_label(proposal),
            "findings": findings,
        }

    return week_calendar_lattice(
        week_start,
        days,
        assignment_content=assignment_content,
        late_doc_content=late_doc_content,
        legend=(
            CalendarStatusPresentation(
                "proposal-change", "Delta", "Proposal change", "A proposed assignment change."
            ),
        ),
        **(
            {
                "columns": tuple(f"{day['date']:%A}" for day in days),
                "header_variant": "single-row",
            }
            if len(days) < 5
            else {}
        ),
    )


def _effective_late_doc_assignments(proposal):
    """Overlay the required review decision without changing the retained solver result."""
    reviews = {review.date.isoformat(): review for review in proposal.late_doc_reviews.all()}
    assignments = []
    _, effective_assignments = effective_proposal_assignments(proposal)
    for assignment in effective_assignments:
        review = reviews.get(assignment["date"])
        if review and review.decision == "replaced" and review.replacement_physician:
            assignments.append(
                {
                    **assignment,
                    "physician_id": review.replacement_physician_id,
                    "physician_name": review.replacement_physician.display_name,
                }
            )
        else:
            assignments.append(assignment)
    return tuple(assignments)


def _proposal_changes(proposal):
    changes = {
        (
            assignment_type,
            assignment["date"],
            assignment.get("period_of_day", ""),
            assignment.get("position", "late-doc"),
        )
        for assignment_type in ("precepting", "late_doc")
        for assignment in proposal.proposed_assignments.get(assignment_type, ())
    }
    changes.update(
        (edit.assignment_type, edit.date.isoformat(), edit.period_of_day, edit.position)
        for edit in proposal.edits.all()
    )
    return changes


def _proposal_delta_label(proposal):
    return "Filled" if proposal.mode == PreceptingSolverProposal.Mode.FILL else "Changed"


def _proposal_edit_url(
    proposal, assignment_type, on_date, period_of_day, position, *, week_start=None
):
    url = reverse(
        "precepting-solver-assignment-edit",
        args=(proposal.pk, assignment_type, on_date, period_of_day, position),
    )
    return f"{url}?week={week_start.isoformat()}" if week_start else url


def _proposal_week_url(proposal, week_start):
    return reverse("precepting-solver-week", args=(proposal.pk, week_start.isoformat()))


def _proposal_contains_week(proposal, week_start):
    return (
        week_start <= proposal.solve_end_date
        and week_start + timedelta(days=4) >= proposal.solve_start_date
    )


def _proposal_edit_return_url(request, proposal, *, conflict=False):
    try:
        week_start = _week_start(request.GET["week"])
    except KeyError, ValueError:
        url = reverse("precepting-solver-review", args=(proposal.pk,))
    else:
        url = (
            _proposal_week_url(proposal, week_start)
            if _proposal_contains_week(proposal, week_start)
            else reverse("precepting-solver-review", args=(proposal.pk,))
        )
    return f"{url}?conflict=revision" if conflict else url


def _baseline_slots(proposal):
    slots = {}
    for session in proposal.input_snapshot["sessions"]:
        for position in session["positions"]:
            assignment = position["assignment"]
            if assignment:
                slots[(session["date"], session["period_of_day"], position["key"])] = assignment[
                    "physician_name"
                ]
    for late_doc in proposal.input_snapshot["late_doc_positions"]:
        if late_doc["assignment"]:
            slots[(late_doc["date"], "Late Doc", "late-doc")] = late_doc["assignment"][
                "physician_name"
            ]
    return slots


def _baseline_assignment_values(proposal):
    slots = {}
    for session in proposal.input_snapshot["sessions"]:
        for position in session["positions"]:
            if assignment := position["assignment"]:
                slots[(session["date"], session["period_of_day"], position["key"])] = assignment
    for late_doc in proposal.input_snapshot["late_doc_positions"]:
        if assignment := late_doc["assignment"]:
            slots[(late_doc["date"], "Late Doc", "late-doc")] = assignment
    return slots


def _proposal_effective_slots(proposal):
    slots = _baseline_assignment_values(proposal)
    for assignment_type, period in (("precepting", None), ("late_doc", "Late Doc")):
        for assignment in proposal.proposed_assignments.get(assignment_type, ()):
            key = (
                assignment["date"],
                assignment.get("period_of_day") if period is None else period,
                assignment.get("position", "late-doc"),
            )
            if assignment.get("physician_name"):
                slots[key] = assignment
            else:
                slots.pop(key, None)
    for edit in proposal.edits.all():
        key = (
            edit.date.isoformat(),
            edit.period_of_day if edit.assignment_type == "precepting" else "Late Doc",
            edit.position,
        )
        if edit.resulting_value:
            slots[key] = edit.resulting_value
        else:
            slots.pop(key, None)
    for review in proposal.late_doc_reviews.all():
        if review.decision == "replaced" and review.replacement_physician:
            slots[(review.date.isoformat(), "Late Doc", "late-doc")] = {
                "physician_id": review.replacement_physician_id,
                "physician_name": review.replacement_physician.display_name,
            }
    return slots


def _calendar_slot_content(
    slots, on_date, lane, *, changed_slots, proposed, proposal, findings_by_target, actionable
):
    if lane == "Late Doc":
        target = _position_target(on_date, lane, "late-doc")
        findings = tuple(findings_by_target.get(target, ())) if actionable else ()
        assignment = slots.get((on_date, lane, "late-doc"))
        changed = actionable and proposed and (on_date, lane, "late-doc") in changed_slots
        if (
            actionable
            and (assignment is None or changed)
            and date.fromisoformat(on_date).weekday() == 0
            and holiday_closure_for(date.fromisoformat(on_date), "PM")
        ):
            value, state = "Closed", "optional-empty"
        else:
            value, state = (
                assignment or "Unfilled",
                slot_state_for_target(findings_by_target, target)
                if actionable
                else "optional-empty",
            )
        return {
            "value": value,
            "display": value,
            "state": state,
            "finding_tone": finding_tone(findings),
            "target": target,
            "findings": findings,
            "changed": changed and value != "Closed",
            "delta_label": _proposal_delta_label(proposal),
            "edit_url": None,
            "accessible_label": _calendar_slot_accessible_label(
                "Late Doc", value, state, changed, proposal, finding_tone(findings)
            ),
        }
    positions = (
        ("primary-1", "Preceptor 1"),
        ("primary-2", "Preceptor 2"),
        ("secondary", "Secondary"),
    )
    session_target = _session_target(on_date, lane)
    session_findings = tuple(findings_by_target.get(session_target, ()))
    closed = holiday_closure_for(date.fromisoformat(on_date), lane)
    positions = (
        ("primary-1", "Preceptor 1"),
        ("primary-2", "Preceptor 2"),
        ("secondary", "Secondary"),
    )
    if (
        actionable
        and closed
        and all(
            slots.get((on_date, lane, position)) is None
            or (proposed and (on_date, lane, position) in changed_slots)
            for position, _ in positions
        )
    ):
        on_date_value = date.fromisoformat(on_date)
        return {
            "period": lane,
            "date": on_date,
            "target": session_target,
            "findings": session_findings,
            "faculty_coverage_conflict": False,
            "positions": (),
            "display": "Closed",
            "state": "holiday-closed",
            "finding_tone": "",
            "changed": False,
            "edit_url": None,
            "accessible_label": (
                f"{lane} assignments for {on_date_value:%A, %B} {on_date_value.day}, "
                f"{on_date_value.year}: Closed for {closed.name}, {closed.coverage_label}."
            ),
        }
    return {
        "period": lane,
        "date": on_date,
        "target": session_target,
        "findings": (
            *session_findings,
            *_holiday_position_findings(findings_by_target, on_date, lane),
        ),
        "faculty_coverage_conflict": any(
            finding["code"] == "insufficient_faculty_equivalent_coverage"
            for finding in session_findings
        ),
        "positions": tuple(
            _calendar_position_content(
                slots,
                on_date,
                lane,
                position,
                label,
                changed_slots=changed_slots,
                proposed=proposed,
                proposal=proposal,
                findings_by_target=findings_by_target,
                actionable=actionable,
            )
            for position, label in positions
        ),
    }


def _calendar_position_content(
    slots,
    on_date,
    lane,
    position,
    label,
    *,
    changed_slots,
    proposed,
    proposal,
    findings_by_target,
    actionable,
):
    target = _position_target(on_date, lane, position)
    findings = tuple(findings_by_target.get(target, ())) if actionable else ()
    assignment = slots.get((on_date, lane, position))
    changed = actionable and proposed and (on_date, lane, position) in changed_slots
    if (
        actionable
        and (assignment is None or changed)
        and holiday_closure_for(date.fromisoformat(on_date), lane)
    ):
        value, state = "Closed", "optional-empty"
    else:
        value = assignment or ("Unfilled" if position.startswith("primary-") else "-")
        state = (
            "gap"
            if actionable and value == "Unfilled"
            else slot_state_for_target(findings_by_target, target)
            if actionable
            else "optional-empty"
        )
    return {
        "label": label,
        "value": value,
        "display": value,
        "state": state,
        "finding_tone": finding_tone(findings),
        "target": target,
        "changed": changed and value != "Closed",
        "delta_label": _proposal_delta_label(proposal),
        "edit_url": None,
        "accessible_label": _calendar_slot_accessible_label(
            label, value, state, changed, proposal, finding_tone(findings)
        ),
    }


def _calendar_context_slots(proposal):
    """Project only the non-mutable edge-week assignments from current schedule records."""
    start = proposal.solve_start_date - timedelta(days=proposal.solve_start_date.weekday())
    end = proposal.solve_end_date + timedelta(days=4 - proposal.solve_end_date.weekday())
    slots = {}
    for assignment in PreceptorAssignment.objects.filter(
        session__date__range=(start, end)
    ).select_related("session", "physician"):
        on_date = assignment.session.date
        if proposal.solve_start_date <= on_date <= proposal.solve_end_date:
            continue
        position = (
            f"primary-{assignment.primary_position}"
            if assignment.responsibility == PreceptorAssignment.Responsibility.PRIMARY
            else "secondary"
        )
        slots[(on_date.isoformat(), assignment.session.period_of_day, position)] = {
            "physician_id": assignment.physician_id,
            "physician_name": assignment.physician.display_name,
            "is_locked": assignment.is_locked,
        }
    for assignment in LateDocAssignment.objects.filter(date__range=(start, end)).select_related(
        "physician"
    ):
        if proposal.solve_start_date <= assignment.date <= proposal.solve_end_date:
            continue
        slots[(assignment.date.isoformat(), "Late Doc", "late-doc")] = {
            "physician_id": assignment.physician_id,
            "physician_name": assignment.physician.display_name,
        }
    return slots


def _calendar_slot_accessible_label(label, value, state, changed, proposal, finding_tone_value=""):
    state_label = {
        "conflict": "Hard conflict: ",
        "preference": "Non-blocking finding: ",
    }.get(state, "")
    if finding_tone_value == "hard" and not state_label:
        state_label = "Hard finding: "
    elif finding_tone_value == "preference" and not state_label:
        state_label = "Non-blocking finding: "
    return (
        f"{label}: {_proposal_delta_label(proposal) + ', ' if changed else ''}{state_label}{value}"
    )


def _finding_state(findings):
    """Keep focused week presentation on the shared calendar-state mapping."""
    return slot_state_for_target({"slot": findings}, "slot")


def _slot_display(value, state):
    if state == "conflict":
        return f"Conflict: {value}"
    if state == "preference":
        return f"Check: {value}"
    return value


def _present_findings(findings):
    return tuple(_present_finding(finding) for finding in findings)


def _calendar_findings_by_target(findings, slots, *, changed_slots, proposed):
    presented = list(_present_findings(findings))
    for (on_date, period, position), assignment in slots.items():
        if (
            not assignment
            or (proposed and (on_date, period, position) in changed_slots)
            or not is_operational_date(date.fromisoformat(on_date))
            or not holiday_closure_for(
                date.fromisoformat(on_date), "PM" if period == "Late Doc" else period
            )
        ):
            continue
        presented.append(
            _present_finding(
                {
                    "code": (
                        "holiday_closed_late_doc_assignment"
                        if position == "late-doc"
                        else "holiday_closed_assignment"
                    ),
                    "severity": "hard",
                    "date": on_date,
                    "period_of_day": "PM" if position == "late-doc" else period,
                    "explanation": {"position": position},
                }
            )
        )
    findings_by_target = {}
    for finding in presented:
        target_findings = findings_by_target.setdefault(finding["target"], [])
        if not any(existing["code"] == finding["code"] for existing in target_findings):
            target_findings.append(finding)
    return findings_by_target


def _holiday_position_findings(findings_by_target, on_date, period):
    return tuple(
        finding
        for position in ("primary-1", "primary-2", "secondary")
        for finding in findings_by_target.get(_position_target(on_date, period, position), ())
        if finding["code"] == "holiday_closed_assignment"
    )


def _present_after_findings(proposal):
    return _present_findings_with_provenance(proposal, proposal.after_findings)


def _present_confirmation_findings(proposal, findings, finding_keys):
    return tuple(
        {**finding, "selection_key": finding_key}
        for finding, finding_key in zip(
            _present_findings_with_provenance(proposal, findings), finding_keys, strict=True
        )
    )


def _present_findings_with_provenance(proposal, findings):
    return tuple(
        {
            **_present_finding(finding),
            "provenance": "Existing"
            if _finding_existed_before_proposal(finding, proposal.before_findings)
            else "Proposed",
            "provenance_detail": "Existed before this proposal."
            if _finding_existed_before_proposal(finding, proposal.before_findings)
            else "Introduced by this proposal.",
        }
        for finding in findings
    )


def _finding_existed_before_proposal(after_finding, before_findings):
    after_identity = _finding_identity(after_finding)
    for before_finding in before_findings:
        if _finding_identity(before_finding) == after_identity:
            return True
        if (
            after_finding["code"] == "missing_primary_coverage"
            and _finding_scope(before_finding) == _finding_scope(after_finding)
            and set(_missing_primary_positions(after_finding))
            <= set(_missing_primary_positions(before_finding))
        ):
            return True
    return False


def _finding_identity(finding):
    return (*_finding_scope(finding), _missing_primary_positions(finding))


def _finding_scope(finding):
    explanation = finding.get("explanation", {})
    return (
        finding["code"],
        finding["date"],
        finding.get("period_of_day"),
        explanation.get("position"),
        ()
        if finding["code"]
        in {
            "missing_primary_coverage",
            "insufficient_faculty_equivalent_coverage",
            "missing_normal_rounder",
        }
        else tuple(sorted(finding.get("physician_ids", ()))),
    )


def _missing_primary_positions(finding):
    return tuple(finding.get("explanation", {}).get("missing_primary_positions", ()))


def _present_finding(finding):
    on_date = date.fromisoformat(finding["date"])
    period = finding.get("period_of_day")
    position = finding.get("explanation", {}).get("position")
    target = (
        _position_target(
            finding["date"], "Late Doc" if position == "late-doc" else period, position
        )
        if position
        else _session_target(finding["date"], period)
        if period
        else _week_target(on_date)
    )
    return {
        **finding,
        "date": on_date,
        "period_of_day": period,
        "position_label": {
            "primary-1": "Preceptor 1",
            "primary-2": "Preceptor 2",
            "secondary": "Secondary",
            "late-doc": "Late Doc",
        }.get(position, ""),
        "message": draft_finding_message(finding["code"], finding.get("explanation")),
        "target": target,
        "target_url": f"?view=proposed#{target}",
    }


def _week_target(week):
    return f"proposal-week-{week:%Y-%m-%d}"


def _session_target(on_date, period):
    return f"proposal-session-{on_date}-{period}"


def _position_target(on_date, period, position):
    return f"proposal-position-{on_date}-{period}-{position}"


def _workspace_context(current, user):
    month = organization_today().replace(day=1)
    return {
        "schedule_workspace_page_title": "Schedule Bot",
        "schedule_workspace_label": "Preceptor Schedule",
        "schedule_workspace_navigation": preceptor_workspace_navigation(
            current=current, month=month, user=user
        ),
    }


def _proposal_listing_context(request):
    cutoff = timezone.now() - timedelta(days=30)
    proposals = list(
        PreceptingSolverProposal.objects.filter(applied_at__isnull=True, updated_at__gte=cutoff)
        .select_related("created_by", "last_edited_by")
        .order_by("-pk")
    )
    proposals.sort(
        key=lambda proposal: proposal.updated_at if proposal.revision > 1 else proposal.created_at,
        reverse=True,
    )
    for proposal in proposals:
        if request.GET.get("reuse") or len(proposal.input_fingerprint) != 64:
            proposal.is_current, proposal.status_message = True, "Current"
        else:
            proposal.is_current, proposal.status_message = _proposal_currentness(proposal)
    older_proposals = ()
    if request.GET.get("older"):
        older_proposals = list(
            PreceptingSolverProposal.objects.filter(applied_at__isnull=True, updated_at__lt=cutoff)
            .select_related("created_by", "last_edited_by")
            .order_by("-pk")
        )
        older_proposals.sort(
            key=lambda proposal: (
                proposal.updated_at if proposal.revision > 1 else proposal.created_at
            ),
            reverse=True,
        )
        for proposal in older_proposals:
            proposal.is_current, proposal.status_message = _proposal_currentness(proposal)
        history_proposals = list(
            PreceptingSolverProposal.objects.filter(applied_at__isnull=False)
            .select_related("created_by", "applied_by")
            .order_by("-applied_at", "-pk")
        )
    else:
        history_proposals = ()
    return {
        "recent_proposals": proposals[:5],
        "additional_recent_proposals": proposals[5:],
        "older_proposals": older_proposals,
        "history_proposals": history_proposals,
        "recent_proposals_table": _recent_proposals_table(proposals),
    }


def _comparison_context(proposal):
    comparison = proposal.comparisons.select_related("created_by").last()
    return {
        "comparison": comparison,
        "comparison_result": _present_comparison_result(comparison.result) if comparison else None,
        "comparison_current": comparison is not None
        and comparison.proposal_revision == proposal.revision,
        "comparison_workload_table": _comparison_workload_table(comparison.result)
        if comparison
        else None,
    }


def _recent_proposals_table(proposals):
    return DataTable(
        identity="recent-proposals-table",
        label="Recent unapplied proposals",
        width="wide",
        columns=tuple(
            DataTableColumn(label)
            for label in ("Solve range", "Mode", "Created", "Updated", "Status", "Actions")
        ),
        rows=tuple(
            DataTableRow(
                cells=(
                    DataTableCell(
                        f"{proposal.solve_start_date:%b} {proposal.solve_start_date.day}, "
                        f"{proposal.solve_start_date:%Y} through "
                        f"{proposal.solve_end_date:%b} {proposal.solve_end_date.day}, "
                        f"{proposal.solve_end_date:%Y}",
                        row_header=True,
                    ),
                    DataTableCell(proposal.get_mode_display()),
                    _proposal_timestamp_cell(proposal.created_by, proposal.created_at),
                    _proposal_timestamp_cell(
                        proposal.last_edited_by or proposal.created_by, proposal.updated_at
                    ),
                    DataTableCell(
                        f"{'Current' if proposal.is_current else 'Stale'}. Unapplied", kind="status"
                    ),
                    DataTableCell(
                        "",
                        kind="links",
                        links=(
                            DataTableLink(
                                "Continue review",
                                reverse("precepting-solver-review", args=(proposal.pk,)),
                                emphasized=True,
                            ),
                            DataTableLink(
                                "Reuse settings",
                                f"{reverse('precepting-solver-workspace')}?reuse={proposal.pk}",
                            ),
                        ),
                    ),
                )
            )
            for proposal in proposals
        ),
        empty_state="No recent unapplied proposals.",
    )


def _proposal_timestamp_cell(account, timestamp):
    return DataTableCell(
        str(account),
        kind="metadata",
        detail=date_format(timezone.localtime(timestamp), "M j, Y, P"),
        machine_value=timestamp.isoformat(),
    )


def _proposal_workload_table(proposal):
    return _workload_table(
        identity="proposal-workload-effects",
        label="Faculty workload effects",
        items=proposal.workload_effects.get("faculty", ()),
        changed_label="Proposed",
        empty_state="No Faculty workload effects.",
    )


def _comparison_workload_table(result):
    return _workload_table(
        identity="comparison-workload-effects",
        label="Faculty workload effect",
        items=result["workload_effect"]["faculty_workloads"],
        changed_label="Compared",
        empty_state="No Faculty workload change.",
    )


def _workload_table(*, identity, label, items, changed_label, empty_state):
    return DataTable(
        identity=identity,
        label=label,
        width="compact",
        columns=tuple(
            DataTableColumn(column) for column in ("Faculty", "Current", changed_label, "Target")
        ),
        rows=tuple(
            DataTableRow(
                cells=(
                    DataTableCell(item["physician_name"], row_header=True),
                    DataTableCell(str(item["current_total"])),
                    DataTableCell(str(item["proposed_total"])),
                    DataTableCell(floatformat(item["target_total"], 1)),
                )
            )
            for item in items
        ),
        empty_state=empty_state,
    )


def _present_manual_edits(proposal):
    return tuple(
        {
            "edited_at": edit.edited_at,
            "edited_by": edit.edited_by,
            "date": edit.date,
            "period_of_day": edit.period_of_day,
            "position_label": _position_label(edit.position),
            "previous_value": edit.previous_value,
            "resulting_value": edit.resulting_value,
            "note": edit.note,
        }
        for edit in proposal.edits.all()
    )


def _position_label(position):
    if position.startswith("primary-"):
        return f"Preceptor {position.removeprefix('primary-')}"
    return position.replace("-", " ").title()


def _present_comparison_result(result):
    return {
        **result,
        "forced_or_excluded": _present_position_tokens(result["forced_or_excluded"]),
        "additional_changes": tuple(
            _present_position_tokens(change) for change in result["additional_changes"]
        ),
        "compromises": tuple(
            _present_position_tokens(compromise) for compromise in result["compromises"]
        ),
    }


def _present_position_tokens(value):
    return re.sub(r"\bprimary-(\d+)\b", r"Preceptor \1", value)


def _review_context(proposal, user, *, stale_message=""):
    current, current_message = _proposal_currentness(proposal)
    return {
        **_workspace_context("solve", user),
        "proposal": proposal,
        "review_groups": _review_groups(proposal),
        **_finding_review_context(proposal),
        "current": current,
        "late_doc_ready": not proposal.late_doc_reviews.filter(decision="pending").exists(),
        "stale_message": stale_message or current_message,
        "reconciliation_preview": None,
        "reconciliation": getattr(proposal, "reconciliation", None),
        "calendar_view": "proposed",
        "calendar": _proposal_calendar(proposal, proposed=True),
        "manual_edits": _present_manual_edits(proposal),
        "proposal_workload_table": _proposal_workload_table(proposal),
        **_comparison_context(proposal),
    }
