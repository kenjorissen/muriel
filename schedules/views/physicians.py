"""Physician directory, identity, role, and source-rule HTTP workflows."""

from datetime import date
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
from django.db.models.functions import Lower
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.formats import date_format

from ..forms.community_preceptors import CommunityPreceptorRemovalForm, CommunityPreceptorRuleForm
from ..forms.physicians import (
    CustomAvailabilityCategoryAssignmentForm,
    CustomAvailabilityCategoryDeactivateForm,
    CustomAvailabilityCategoryForm,
    EffectiveRoleForm,
    EndEffectiveFactForm,
    FacultyAvailabilityTemplateForm,
    FacultyPreceptingCapacityOverrideForm,
    MonthlyPhysicianAvailabilityRuleDeactivateForm,
    MonthlyPhysicianAvailabilityRuleForm,
    PhysicianAccountForm,
    PhysicianForm,
)
from ..forms.rounding import CasualRounderRuleForm, RoundingRemovalForm
from ..models.availability_categories import (
    CustomAvailabilityCategory,
    CustomAvailabilityCategoryAssignment,
    CustomAvailabilityCategoryChange,
)
from ..models.community_preceptors import CommunityPreceptorRule
from ..models.physicians import (
    EffectiveRole,
    FacultyAvailabilityTemplate,
    MonthlyPhysicianAvailabilityRule,
    Physician,
    PhysicianChange,
    float_schedule_input_mode,
)
from ..models.preceptors import PreceptorSession
from ..models.rounding import CasualRounderRule
from ..monthly_rule_presentation import monthly_rule_summary, monthly_rule_summary_values
from ..services._mutation import require_scheduling_administrator
from ..services.accounts import create_physician_with_invitation
from ..services.availability import effective_preceptor_availability_on
from ..services.availability_categories import (
    correct_custom_availability_category,
    create_custom_availability_category,
    deactivate_custom_availability_category,
    set_custom_availability_category_physicians,
)
from ..services.community import (
    correct_community_preceptor_rule,
    create_community_preceptor_rule,
    remove_community_preceptor_rule,
)
from ..services.operational_attention import organization_today
from ..services.physicians import (
    add_role,
    correct_faculty_availability_template,
    correct_monthly_physician_availability_rule,
    correct_physician_identity,
    correct_role,
    create_faculty_availability_template,
    create_monthly_physician_availability_rule,
    create_physician,
    deactivate_monthly_physician_availability_rule,
    end_role,
    faculty_precepting_capacities_for_week,
    faculty_precepting_capacity_for_week,
    link_physician_user,
    physicians_status_on,
    set_faculty_precepting_capacity_override,
    start_later_faculty_availability_template,
    unlink_physician_user,
)
from ..services.rounding import (
    correct_casual_rounder_rule,
    create_casual_rounder_rule,
    remove_casual_rounder_rule,
)
from ..table_presentation import (
    DataTable,
    DataTableCell,
    DataTableColumn,
    DataTableLink,
    DataTableRow,
    history_timestamp,
    retained_value,
)
from ._invitations import deliver_invitation_response
from ._shared import _requested_date


def scheduling_administration_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        require_scheduling_administrator(request.user)
        return view(request, *args, **kwargs)

    return wrapped


def _physicians_with_current_role(role, on_date):
    return (
        Physician.objects.filter(
            roles__role=role,
            roles__effective_start__lte=on_date,
        )
        .filter(Q(roles__effective_end__isnull=True) | Q(roles__effective_end__gte=on_date))
        .order_by(Lower("display_name"), "display_name", "pk")
        .distinct()
    )


def _physician_workspace_navigation(selected_view):
    directory_url = reverse("physicians-directory")
    return tuple(
        {
            "label": label,
            "url": url,
            "current": selected_view == view,
        }
        for view, label, url in (
            ("all", "All", directory_url),
            ("faculty", "Faculty", f"{directory_url}?view=faculty"),
            (
                "community-rounders",
                "Community Rounders",
                f"{directory_url}?view=community-rounders",
            ),
            (
                "community-preceptors",
                "Community Preceptors",
                f"{directory_url}?view=community-preceptors",
            ),
            ("casual-rounders", "Casual Rounders", f"{directory_url}?view=casual-rounders"),
            (
                "external-patient-care",
                "External Patient Care",
                f"{directory_url}?view=external-patient-care",
            ),
            ("inactive", "Inactive", f"{directory_url}?view=inactive"),
            (
                "schedule-options",
                "Schedule Options",
                reverse("custom-availability-category-workspace"),
            ),
        )
    )


def _recurrence_rule_form_context(
    *, form, rule_model, role, current_rule=None, fixed_physician=None, **context
):
    if fixed_physician or current_rule:
        physician = fixed_physician or current_rule.physician
        recurrence_rules = rule_model.objects.filter(physician=physician).select_related(
            "physician"
        )
    else:
        physicians = _physicians_with_current_role(role, organization_today())
        form.fields["physician"].queryset = physicians
        recurrence_rules = rule_model.objects.filter(physician__in=physicians).select_related(
            "physician"
        )
    recurrence_rules = tuple(recurrence_rules)
    return {
        "form": form,
        "current_rule": current_rule,
        "fixed_physician": fixed_physician,
        "recurrence_rules": recurrence_rules,
        "recurrence_rules_show_coverage": rule_model is CommunityPreceptorRule,
        "show_recurrence_rule_context": True,
        "recurrence_rule_context_table": _recurrence_rule_context_table(
            recurrence_rules,
            current_rule=current_rule,
            fixed_physician=fixed_physician,
            show_coverage=rule_model is CommunityPreceptorRule,
        ),
        **context,
    }


@login_required
def physicians_directory(request):
    today = organization_today()
    views = (
        ("all", "All", "No physicians have been recorded."),
        ("faculty", "Faculty", "No physicians currently have a Faculty role."),
        (
            "community-rounders",
            "Community Rounders",
            "No physicians currently have a Community Rounder role.",
        ),
        (
            "community-preceptors",
            "Community Preceptors",
            "No physicians currently have a Community Preceptor role.",
        ),
        (
            "casual-rounders",
            "Casual Rounders",
            "No physicians currently have a Casual Rounder role.",
        ),
        (
            "external-patient-care",
            "External Patient Care",
            "No physicians currently have an External Patient Care role.",
        ),
        ("inactive", "Inactive", "No physicians are currently inactive."),
    )
    selected_view = request.GET.get("view", "all")
    view_details = next((view for view in views if view[0] == selected_view), views[0])
    selected_view = view_details[0]
    current_roles = EffectiveRole.objects.filter(effective_start__lte=today).filter(
        Q(effective_end__isnull=True) | Q(effective_end__gte=today)
    )
    current_templates = FacultyAvailabilityTemplate.objects.filter(
        Q(effective_end__isnull=True) | Q(effective_end__gte=today)
    )
    current_monthly_rules = MonthlyPhysicianAvailabilityRule.objects.filter(is_active=True).filter(
        Q(effective_end__isnull=True) | Q(effective_end__gte=today)
    )
    current_community_rules = CommunityPreceptorRule.objects.filter(
        Q(effective_start__isnull=True) | Q(effective_start__lte=today)
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=today))
    current_casual_rules = CasualRounderRule.objects.filter(
        Q(effective_start__isnull=True) | Q(effective_start__lte=today)
    ).filter(Q(effective_end__isnull=True) | Q(effective_end__gte=today))
    physicians = Physician.objects.order_by(Lower("display_name"), "display_name", "pk")
    role_views = {
        "faculty": EffectiveRole.Role.FACULTY,
        "community-rounders": EffectiveRole.Role.COMMUNITY_ROUNDER,
        "community-preceptors": EffectiveRole.Role.COMMUNITY_PRECEPTOR,
        "casual-rounders": EffectiveRole.Role.CASUAL_ROUNDER,
        "external-patient-care": EffectiveRole.Role.EXTERNAL_PATIENT_CARE,
    }
    if selected_view in role_views:
        physicians = physicians.filter(
            pk__in=current_roles.filter(role=role_views[selected_view]).values("physician_id")
        )
    elif selected_view == "inactive":
        physicians = physicians.exclude(pk__in=current_roles.values("physician_id"))
    physicians = list(
        physicians.prefetch_related(
            Prefetch("roles", queryset=current_roles, to_attr="current_roles"),
            Prefetch(
                "faculty_availability_templates",
                queryset=current_templates,
                to_attr="current_or_future_faculty_templates",
            ),
            Prefetch(
                "monthly_availability_rules",
                queryset=current_monthly_rules,
                to_attr="current_or_future_monthly_rules",
            ),
            Prefetch(
                "community_preceptor_rules",
                queryset=current_community_rules,
                to_attr="current_community_rules",
            ),
            Prefetch(
                "casual_rounder_rules",
                queryset=current_casual_rules,
                to_attr="current_casual_rules",
            ),
        )
    )
    role_labels = {
        EffectiveRole.Role.FACULTY: "Faculty",
        EffectiveRole.Role.COMMUNITY_ROUNDER: "Community Rounder",
        EffectiveRole.Role.COMMUNITY_PRECEPTOR: "Community Preceptor",
        EffectiveRole.Role.CASUAL_ROUNDER: "Casual Rounder",
        EffectiveRole.Role.FLOAT_PRECEPTOR: "Float Preceptor",
        EffectiveRole.Role.EXTERNAL_PATIENT_CARE: "External Patient Care",
    }
    leadership_tag_labels = {
        EffectiveRole.FacultyLeadership.PROGRAM_DIRECTOR: "PD",
        EffectiveRole.FacultyLeadership.ASSOCIATE_PROGRAM_DIRECTOR: "APD",
    }
    role_order = {role: index for index, role in enumerate(role_labels)}
    for physician in physicians:
        directory_role_tags = []
        for role in sorted(physician.current_roles, key=lambda role: role_order[role.role]):
            directory_role_tags.append(role_labels[role.role])
            if role.faculty_leadership:
                directory_role_tags.append(leadership_tag_labels[role.faculty_leadership])
            if role.faculty_equivalent:
                directory_role_tags.append("Faculty-equivalent")
        physician.directory_role_tags = tuple(directory_role_tags)
        current_role_names = {role.role for role in physician.current_roles}
        physician.source_configuration = []
        for role, label, sources in (
            (
                EffectiveRole.Role.FACULTY,
                "Faculty",
                physician.current_or_future_faculty_templates,
            ),
            (
                EffectiveRole.Role.COMMUNITY_PRECEPTOR,
                "Community",
                physician.current_community_rules,
            ),
            (
                EffectiveRole.Role.CASUAL_ROUNDER,
                "Casual",
                physician.current_casual_rules,
            ),
        ):
            if role in current_role_names:
                count = len(sources)
                if role == EffectiveRole.Role.FACULTY:
                    monthly_rule_count = len(physician.current_or_future_monthly_rules)
                    detail = f"{count} template{'' if count == 1 else 's'}"
                    if monthly_rule_count:
                        detail += (
                            f", {monthly_rule_count} rule{'s' if monthly_rule_count != 1 else ''}"
                        )
                else:
                    detail = (
                        f"{count} rule{'s' if count != 1 else ''}" if count else "not configured"
                    )
                physician.source_configuration.append(
                    {
                        "label": label,
                        "detail": detail,
                    }
                )
    computed_capacities = faculty_precepting_capacities_for_week(physicians, today)
    for physician in physicians:
        has_current_faculty_role = any(
            role.role == EffectiveRole.Role.FACULTY for role in physician.current_roles
        )
        if not has_current_faculty_role:
            physician.directory_capacity = "—"
            physician.directory_capacity_accessible_label = "Faculty capacity not applicable because this physician has no current Faculty role."
            continue
        computed_capacity = computed_capacities[physician.pk]
        if physician.faculty_precepting_capacity_override is None:
            physician.directory_capacity = str(computed_capacity)
            physician.directory_capacity_accessible_label = (
                f"Schedule-derived Faculty capacity {computed_capacity} weekly shifts."
            )
            continue
        # TEXT-CONTRACT: physician-weekly-capacity-compact —
        # Physician-directory users see custom(schedule-derived) weekly capacity
        # without intervening spaces, distinguishing the override from its base. Parentheses,
        # punctuation, and whitespace are exact; capitalization and newlines do not
        # apply. Protected by schedules/tests/test_views.py and
        # schedules/tests/test_table_presentation_browser.py.
        physician.directory_capacity = (
            f"{physician.faculty_precepting_capacity_override}({computed_capacity})"
        )
        physician.directory_capacity_accessible_label = (
            f"Custom Faculty capacity {physician.faculty_precepting_capacity_override} weekly shifts; "
            f"schedule-derived capacity {computed_capacity} weekly shifts."
        )
    active_physicians = ()
    inactive_physicians = ()
    if selected_view == "all":
        active_physicians = tuple(physician for physician in physicians if physician.current_roles)
        inactive_physicians = tuple(
            physician for physician in physicians if not physician.current_roles
        )
    directory_table = _physicians_directory_table(
        identity=f"physicians-{selected_view}",
        label=f"{view_details[1]} physicians",
        physicians=physicians,
        empty_state=view_details[2],
    )
    active_physicians_table = _physicians_directory_table(
        identity="physicians-active",
        label="Active physicians",
        physicians=active_physicians,
        empty_state="No physicians currently have an active role.",
    )
    inactive_physicians_table = _physicians_directory_table(
        identity="physicians-inactive",
        label="Inactive physicians",
        physicians=inactive_physicians,
        empty_state="No physicians are currently inactive.",
    )
    return render(
        request,
        "schedules/physicians_directory.html",
        {
            "physicians": physicians,
            "today": today,
            "selected_view": selected_view,
            "physician_workspace_navigation": _physician_workspace_navigation(selected_view),
            "selected_view_label": view_details[1],
            "empty_message": view_details[2],
            "active_physicians": active_physicians,
            "inactive_physicians": inactive_physicians,
            "directory_table": directory_table,
            "active_physicians_table": active_physicians_table,
            "inactive_physicians_table": inactive_physicians_table,
        },
    )


def _physicians_directory_table(*, identity, label, physicians, empty_state):
    return DataTable(
        identity=identity,
        label=label,
        columns=tuple(
            DataTableColumn(column)
            for column in (
                "Physician",
                "Current roles and attributes",
                "Source schedules",
                "Capacity",
                "Action",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        physician.display_name,
                        kind="link",
                        url=reverse("physician-detail", args=(physician.pk,)),
                        row_header=True,
                    ),
                    DataTableCell(
                        "" if physician.directory_role_tags else "No current role",
                        kind="tags" if physician.directory_role_tags else "text",
                        tags=physician.directory_role_tags,
                    ),
                    DataTableCell(
                        "" if physician.source_configuration else "No current source schedules",
                        kind="tags" if physician.source_configuration else "text",
                        tags=tuple(
                            f"{source['label']}: {source['detail']}"
                            for source in physician.source_configuration
                        ),
                    ),
                    DataTableCell(
                        physician.directory_capacity,
                        kind="capacity",
                        accessible_label=physician.directory_capacity_accessible_label,
                    ),
                    DataTableCell(
                        "View details",
                        kind="link",
                        url=reverse("physician-detail", args=(physician.pk,)),
                    ),
                )
            )
            for physician in physicians
        ),
        empty_state=empty_state,
    )


@login_required
@scheduling_administration_required
def physician_create(request):
    form = PhysicianForm(
        request.POST or None,
        include_initial_role=True,
        initial={"effective_start": organization_today()},
    )
    if request.method == "POST" and form.is_valid():
        cleaned_data = dict(form.cleaned_data)
        create_login_and_invitation = cleaned_data.pop("create_login_and_invitation")
        official_email = cleaned_data.pop("official_email")
        role = cleaned_data.pop("role")
        effective_start = cleaned_data.pop("effective_start")
        effective_end = cleaned_data.pop("effective_end")
        faculty_equivalent = cleaned_data.pop("faculty_equivalent")
        faculty_leadership = cleaned_data.pop("faculty_leadership")
        try:
            values = {
                "scheduler": request.user,
                "roles": (
                    [
                        {
                            "role": role,
                            "effective_start": effective_start,
                            "effective_end": effective_end,
                            "faculty_equivalent": faculty_equivalent,
                            "faculty_leadership": faculty_leadership,
                        }
                    ]
                    if role
                    else None
                ),
                **cleaned_data,
            }
            if create_login_and_invitation:
                physician, invitation, token = create_physician_with_invitation(
                    email=official_email, **values
                )
            else:
                physician = create_physician(**values)
        except ValidationError as error:
            if hasattr(error, "message_dict"):
                errors = {
                    ("official_email" if field in {"email", "username"} else field): messages
                    for field, messages in error.message_dict.items()
                }
                form.add_error(None, ValidationError(errors))
            else:
                form.add_error(None, error)
        else:
            if create_login_and_invitation:
                return deliver_invitation_response(request, invitation, token, "Invitation")
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request, "schedules/physician_form.html", {"form": form, "title": "Add physician"}
    )


@login_required
def physician_detail(request, physician_id):
    physician = get_object_or_404(Physician.objects.select_related("user"), pk=physician_id)
    if request.method == "POST":
        require_scheduling_administrator(request.user)
    action = request.POST.get("action") if request.method == "POST" else None
    if request.method == "POST" and action not in {"account", "identity", "capacity_override"}:
        return HttpResponseBadRequest("Action must be account, identity, or capacity_override.")
    identity_form = PhysicianForm(
        request.POST if action == "identity" else None,
        auto_id="id_identity_%s",
        initial={"display_name": physician.display_name},
    )
    account_form = PhysicianAccountForm(
        request.POST if action == "account" else None,
        auto_id="id_account_%s",
        physician=physician,
        initial={"user": physician.user_id},
    )
    capacity_override_form = FacultyPreceptingCapacityOverrideForm(
        request.POST if action == "capacity_override" else None,
        auto_id="id_capacity_override_%s",
        initial={
            "use_override": physician.faculty_precepting_capacity_override is not None,
            "capacity_override": physician.faculty_precepting_capacity_override,
        },
    )
    if request.method == "POST" and action == "account" and account_form.is_valid():
        try:
            if account_form.cleaned_data["user"]:
                link_physician_user(
                    physician=physician,
                    user=account_form.cleaned_data["user"],
                    scheduler=request.user,
                    reason=account_form.cleaned_data["reason"],
                )
            else:
                unlink_physician_user(
                    physician=physician,
                    scheduler=request.user,
                    reason=account_form.cleaned_data["reason"],
                )
        except ValidationError as error:
            account_form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    if request.method == "POST" and action == "identity" and identity_form.is_valid():
        correct_physician_identity(
            physician=physician,
            scheduler=request.user,
            display_name=identity_form.cleaned_data["display_name"],
            reason=identity_form.cleaned_data["reason"],
        )
        return redirect("physician-detail", physician_id=physician.pk)
    if (
        request.method == "POST"
        and action == "capacity_override"
        and capacity_override_form.is_valid()
    ):
        set_faculty_precepting_capacity_override(
            physician=physician,
            capacity_override=capacity_override_form.cleaned_data["capacity_override"],
            scheduler=request.user,
            reason=capacity_override_form.cleaned_data["reason"],
        )
        return redirect("physician-detail", physician_id=physician.pk)
    selected_date = _requested_date(request.GET.get("date"))
    if selected_date is None:
        return HttpResponseBadRequest("Date must use YYYY-MM-DD.")
    status = physicians_status_on(physician, selected_date)
    roles = tuple(physician.roles.all())
    today = organization_today()
    schedule_input_mode = float_schedule_input_mode(physician, on_date=today)
    has_future_faculty_role = any(
        role.role == EffectiveRole.Role.FACULTY and role.effective_start > today for role in roles
    )
    availability_templates = tuple(physician.faculty_availability_templates.all())
    monthly_availability_rules = tuple(physician.monthly_availability_rules.all())
    community_preceptor_rules = tuple(physician.community_preceptor_rules.all())
    casual_rounder_rules = tuple(physician.casual_rounder_rules.all())
    can_manage = request.user.has_perm("schedules.manage_scheduling_administration")
    return render(
        request,
        "schedules/physician_detail.html",
        {
            "physician": physician,
            "identity_form": identity_form,
            "account_form": account_form,
            "capacity_override_form": capacity_override_form,
            "schedule_derived_faculty_capacity": faculty_precepting_capacity_for_week(
                physician, selected_date
            ),
            "effective_faculty_capacity": (
                physician.faculty_precepting_capacity_override
                if physician.faculty_precepting_capacity_override is not None
                else faculty_precepting_capacity_for_week(physician, selected_date)
            ),
            "selected_date": selected_date,
            "status": status,
            "effective_role_labels": tuple(
                EffectiveRole.Role(role).label for role in status.effective_roles
            ),
            "effective_availability": tuple(
                effective_preceptor_availability_on(physician, selected_date, period_of_day)
                for period_of_day in PreceptorSession.PeriodOfDay.values
            ),
            "roles": roles,
            "has_faculty_role": any(role.role == EffectiveRole.Role.FACULTY for role in roles),
            "has_schedule_inputs": schedule_input_mode is not None or has_future_faculty_role,
            "has_schedule_input_history": bool(
                availability_templates or monthly_availability_rules
            ),
            "community_float_schedule_inputs": schedule_input_mode == "community_float",
            "has_community_preceptor_role": any(
                role.role == EffectiveRole.Role.COMMUNITY_PRECEPTOR for role in roles
            ),
            "has_casual_rounder_role": any(
                role.role == EffectiveRole.Role.CASUAL_ROUNDER for role in roles
            ),
            "availability_templates": availability_templates,
            "monthly_availability_rules": monthly_availability_rules,
            "community_preceptor_rules": community_preceptor_rules,
            "casual_rounder_rules": casual_rounder_rules,
            "can_manage_scheduling_administration": can_manage,
            "physician_roles_table": _physician_roles_table(physician, roles, can_manage),
            "faculty_availability_templates_table": _faculty_availability_templates_table(
                physician,
                availability_templates,
                label=(
                    "Float availability templates"
                    if schedule_input_mode == "community_float"
                    else "Faculty Schedule templates"
                ),
            ),
            "monthly_availability_rules_table": _physician_rule_table(
                "monthly-availability-rules",
                "Monthly scheduling rules",
                monthly_availability_rules,
                physician,
                show_periods=True,
                show_availability=True,
                deactivate=True,
                monthly_summary=True,
            ),
            "physician_community_preceptor_rules_table": _physician_rule_table(
                "physician-community-preceptor-rules",
                "Community Preceptor rules",
                community_preceptor_rules,
                physician,
                show_periods=True,
            ),
            "physician_casual_rounder_rules_table": _physician_rule_table(
                "physician-casual-rounder-rules",
                "Casual Rounder rules",
                casual_rounder_rules,
                physician,
            ),
        },
    )


def _effective_range(start, end, *, start_label="Unbounded past", end_label="Unbounded future"):
    return (
        f"{date_format(start, 'N j, Y') if start else start_label} through "
        f"{date_format(end, 'N j, Y') if end else end_label}"
    )


def _role_attributes(role):
    if role.role == EffectiveRole.Role.COMMUNITY_PRECEPTOR:
        return f"Faculty-equivalent: {'Yes' if role.faculty_equivalent else 'No'}"
    if role.role == EffectiveRole.Role.FACULTY:
        return f"Faculty leadership: {role.get_faculty_leadership_display()}"
    return "None"


def _physician_roles_table(physician, roles, can_manage):
    columns = [DataTableColumn(label) for label in ("Role", "Attributes", "Effective range")]
    if can_manage:
        columns.append(DataTableColumn("Actions"))
    return DataTable(
        identity="physician-roles",
        label="Physician roles",
        columns=tuple(columns),
        rows=tuple(
            DataTableRow(
                tuple(
                    [
                        DataTableCell(role.get_role_display(), row_header=True),
                        DataTableCell(_role_attributes(role)),
                        DataTableCell(
                            _effective_range(
                                role.effective_start, role.effective_end, end_label="ongoing"
                            )
                        ),
                    ]
                    + (
                        [
                            DataTableCell(
                                "",
                                kind="links",
                                links=(
                                    DataTableLink(
                                        "Edit",
                                        reverse(
                                            "physician-role-correct", args=(physician.pk, role.pk)
                                        ),
                                    ),
                                    DataTableLink(
                                        "End",
                                        reverse("physician-role-end", args=(physician.pk, role.pk)),
                                    ),
                                ),
                            )
                        ]
                        if can_manage
                        else []
                    )
                )
            )
            for role in roles
        ),
        empty_state="No roles recorded.",
    )


def _faculty_availability_templates_table(
    physician, templates, *, label="Faculty Schedule templates"
):
    return DataTable(
        identity="faculty-availability-templates",
        label=label,
        columns=(DataTableColumn("Effective range"), DataTableColumn("Actions")),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(
                        _effective_range(
                            template.effective_start,
                            template.effective_end,
                            start_label="No start",
                            end_label="ongoing",
                        ),
                        row_header=True,
                    ),
                    (
                        DataTableCell(
                            "",
                            kind="links",
                            links=(
                                DataTableLink(
                                    "Edit template",
                                    reverse(
                                        "physician-faculty-availability-correct",
                                        args=(physician.pk, template.pk),
                                    ),
                                ),
                                DataTableLink(
                                    "Start later template",
                                    reverse(
                                        "physician-faculty-availability-start-later",
                                        args=(physician.pk, template.pk),
                                    ),
                                ),
                            ),
                        )
                        if template == templates[-1]
                        else DataTableCell(
                            "Edit template",
                            kind="link",
                            url=reverse(
                                "physician-faculty-availability-correct",
                                args=(physician.pk, template.pk),
                            ),
                        )
                    ),
                )
            )
            for template in templates
        ),
        empty_state="No Faculty Schedule templates recorded.",
    )


def _physician_rule_table(
    identity,
    label,
    rules,
    physician,
    *,
    show_periods=False,
    show_availability=False,
    deactivate=False,
    monthly_summary=False,
):
    if monthly_summary:
        columns = [DataTableColumn("Rule summary")]
    else:
        columns = [DataTableColumn("Weekday"), DataTableColumn("Recurrence")]
    if show_periods and not monthly_summary:
        columns.append(DataTableColumn("Periods"))
    if show_availability and not monthly_summary:
        columns.append(DataTableColumn("Schedule option"))
    if not monthly_summary:
        columns.append(DataTableColumn("Effective range"))
    if deactivate:
        columns.append(DataTableColumn("Status"))
    columns.append(DataTableColumn("Actions"))
    if deactivate:
        correct_name = "physician-monthly-availability-rule-correct"
        remove_name = "physician-monthly-availability-rule-deactivate"
    elif show_periods:
        correct_name = "physician-community-preceptor-rule-correct"
        remove_name = "physician-community-preceptor-rule-remove"
    else:
        correct_name = "physician-casual-rounder-rule-correct"
        remove_name = "physician-casual-rounder-rule-remove"
    return DataTable(
        identity=identity,
        label=label,
        columns=tuple(columns),
        rows=tuple(
            DataTableRow(
                tuple(
                    (
                        [DataTableCell(monthly_rule_summary(rule), row_header=True)]
                        if monthly_summary
                        else [
                            DataTableCell(rule.get_weekday_display(), row_header=True),
                            DataTableCell(rule.ordinal_display),
                        ]
                    )
                    + (
                        [DataTableCell(rule.periods_display)]
                        if show_periods and not monthly_summary
                        else []
                    )
                    + (
                        [DataTableCell(rule.availability_display)]
                        if show_availability and not monthly_summary
                        else []
                    )
                    + (
                        [DataTableCell(_effective_range(rule.effective_start, rule.effective_end))]
                        if not monthly_summary
                        else []
                    )
                    + (
                        [
                            DataTableCell(
                                "Active" if rule.is_active else "Deactivated", kind="status"
                            )
                        ]
                        if deactivate
                        else []
                    )
                    + [
                        DataTableCell(
                            "",
                            kind="links",
                            links=(
                                DataTableLink(
                                    "Edit", reverse(correct_name, args=(physician.pk, rule.pk))
                                ),
                                *(
                                    (
                                        DataTableLink(
                                            "Deactivate",
                                            reverse(remove_name, args=(physician.pk, rule.pk)),
                                        ),
                                    )
                                    if deactivate and rule.is_active
                                    else (
                                        DataTableLink(
                                            "Remove",
                                            reverse(remove_name, args=(physician.pk, rule.pk)),
                                        ),
                                    )
                                    if not deactivate
                                    else ()
                                ),
                            ),
                        )
                    ]
                )
            )
            for rule in rules
        ),
        empty_state=f"No {label.lower()} recorded.",
    )


def _existing_roles_table(roles, current_role=None):
    return DataTable(
        identity="existing-roles",
        label="Existing roles",
        columns=tuple(
            DataTableColumn(label) for label in ("Role", "Attributes", "Effective range", "Context")
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(role.get_role_display(), row_header=True),
                    DataTableCell(_role_attributes(role)),
                    DataTableCell(
                        _effective_range(
                            role.effective_start, role.effective_end, end_label="ongoing"
                        )
                    ),
                    DataTableCell(
                        "Editing this role"
                        if current_role and role.pk == current_role.pk
                        else "Retained role",
                        kind="status"
                        if current_role and role.pk == current_role.pk
                        else "metadata",
                        tone="attention" if current_role and role.pk == current_role.pk else "",
                    ),
                ),
                tone="attention" if current_role and role.pk == current_role.pk else "",
            )
            for role in roles
        ),
        empty_state="No roles recorded.",
    )


def _recurrence_rule_context_table(
    rules,
    *,
    current_rule,
    fixed_physician,
    show_coverage,
    show_availability=False,
    monthly_summary=False,
):
    columns = [] if fixed_physician else [DataTableColumn("Physician")]
    if monthly_summary:
        columns.append(DataTableColumn("Rule summary"))
    else:
        columns.extend((DataTableColumn("Weekday"), DataTableColumn("Recurrence")))
    if show_coverage and not monthly_summary:
        columns.append(DataTableColumn("Coverage"))
    if show_availability and not monthly_summary:
        columns.append(DataTableColumn("Schedule option"))
    if not monthly_summary:
        columns.append(DataTableColumn("Effective range"))
    columns.append(DataTableColumn("Context"))
    return DataTable(
        identity="recurrence-rule-context",
        label="Existing rules",
        columns=tuple(columns),
        rows=tuple(
            DataTableRow(
                tuple(
                    (
                        []
                        if fixed_physician
                        else [DataTableCell(rule.physician.display_name, row_header=True)]
                    )
                    + (
                        [DataTableCell(monthly_rule_summary(rule), row_header=fixed_physician)]
                        if monthly_summary
                        else [
                            DataTableCell(rule.get_weekday_display()),
                            DataTableCell(rule.ordinal_display),
                        ]
                    )
                    + (
                        [DataTableCell(rule.periods_display)]
                        if show_coverage and not monthly_summary
                        else []
                    )
                    + (
                        [DataTableCell(rule.availability_display)]
                        if show_availability and not monthly_summary
                        else []
                    )
                    + (
                        [DataTableCell(_effective_range(rule.effective_start, rule.effective_end))]
                        if not monthly_summary
                        else []
                    )
                    + [
                        DataTableCell(
                            "Editing this rule"
                            if current_rule and rule.pk == current_rule.pk
                            else "Retained rule"
                            if getattr(rule, "is_active", True)
                            else "Deactivated rule",
                            kind="status"
                            if current_rule and rule.pk == current_rule.pk
                            else "metadata",
                            tone="attention" if current_rule and rule.pk == current_rule.pk else "",
                        ),
                    ]
                ),
                tone="attention" if current_rule and rule.pk == current_rule.pk else "",
            )
            for rule in rules
        ),
        empty_state="No retained rules for this physician.",
    )


@login_required
def physician_history(request, physician_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    return render(
        request,
        "schedules/physician_history.html",
        {
            "physician": physician,
            "physician_history_table": _physician_history_table(physician.changes.all()),
        },
    )


@login_required
def physicians_history(request):
    return render(
        request,
        "schedules/physicians_history.html",
        {
            "physicians_history_table": _physicians_history_table(
                PhysicianChange.objects.select_related("physician", "changed_by")
            )
        },
    )


@login_required
def custom_availability_category_workspace(request):
    categories = CustomAvailabilityCategory.objects.prefetch_related(
        Prefetch(
            "assignments",
            queryset=CustomAvailabilityCategoryAssignment.objects.filter(
                is_active=True
            ).select_related("physician"),
            to_attr="active_assignments",
        )
    )
    categories = tuple(categories)
    return render(
        request,
        "schedules/custom_availability_category_workspace.html",
        {
            "categories": categories,
            "schedule_options_table": _schedule_options_table(categories),
            "physician_workspace_navigation": _physician_workspace_navigation("schedule-options"),
        },
    )


def _schedule_options_table(categories):
    return DataTable(
        identity="schedule-options",
        label="Schedule options",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Schedule option",
                "Availability impact",
                "Assigned physicians",
                "Status",
                "Action",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    DataTableCell(category.name, row_header=True),
                    DataTableCell(category.get_impact_display()),
                    DataTableCell(
                        ", ".join(
                            assignment.physician.display_name
                            for assignment in category.active_assignments
                        )
                        or "None"
                    ),
                    DataTableCell(
                        "Built-in"
                        if category.system_key
                        else "Active"
                        if category.is_active
                        else "Inactive",
                        kind="status",
                    ),
                    DataTableCell(
                        "View" if category.system_key or not category.is_active else "View or edit",
                        kind="link",
                        url=reverse("custom-availability-category-edit", args=(category.pk,)),
                    ),
                )
            )
            for category in categories
        ),
        empty_state="No schedule options recorded.",
    )


@login_required
def custom_availability_category_create(request):
    form = CustomAvailabilityCategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            category = create_custom_availability_category(
                scheduler=request.user, **form.cleaned_data
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("custom-availability-category-edit", category_id=category.pk)
    return render(
        request,
        "schedules/custom_availability_category_form.html",
        {"form": form, "title": "Add schedule option", "category": None},
    )


@login_required
def custom_availability_category_edit(request, category_id):
    category = get_object_or_404(CustomAvailabilityCategory, pk=category_id)
    action = request.POST.get("action") if request.method == "POST" else None
    definition_form = CustomAvailabilityCategoryForm(
        request.POST if action == "definition" else None,
        initial={"name": category.name, "impact": category.impact},
    )
    assignment_form = CustomAvailabilityCategoryAssignmentForm(
        request.POST if action == "assignments" else None,
        initial={
            "physicians": category.assignments.filter(is_active=True).values_list(
                "physician_id", flat=True
            )
        },
    )
    deactivate_form = CustomAvailabilityCategoryDeactivateForm(
        request.POST if action == "deactivate" else None
    )
    if request.method == "POST":
        try:
            if category.system_key and action in {"definition", "deactivate"}:
                raise ValidationError("Built-in schedule options cannot be changed.")
            if action == "definition" and definition_form.is_valid():
                correct_custom_availability_category(
                    category=category, scheduler=request.user, **definition_form.cleaned_data
                )
            elif action == "assignments" and assignment_form.is_valid():
                set_custom_availability_category_physicians(
                    category=category,
                    physicians=assignment_form.cleaned_data["physicians"],
                    scheduler=request.user,
                    reason=assignment_form.cleaned_data["reason"],
                )
            elif action == "deactivate" and deactivate_form.is_valid():
                deactivate_custom_availability_category(
                    category=category,
                    scheduler=request.user,
                    reason=deactivate_form.cleaned_data["reason"],
                )
            else:
                return render(
                    request,
                    "schedules/custom_availability_category_form.html",
                    {
                        "category": category,
                        "definition_form": definition_form,
                        "assignment_form": assignment_form,
                        "deactivate_form": deactivate_form,
                        "is_builtin": bool(category.system_key),
                        "title": f"Edit {category.name}",
                    },
                )
        except ValidationError as error:
            if action == "definition":
                definition_form.add_error(None, error)
            elif action == "assignments":
                assignment_form.add_error(None, error)
            else:
                deactivate_form.add_error(None, error)
        else:
            return redirect("custom-availability-category-edit", category_id=category.pk)
    return render(
        request,
        "schedules/custom_availability_category_form.html",
        {
            "category": category,
            "definition_form": definition_form,
            "assignment_form": assignment_form,
            "deactivate_form": deactivate_form,
            "is_builtin": bool(category.system_key),
            "title": f"Edit {category.name}",
        },
    )


@login_required
def custom_availability_category_history(request):
    return render(
        request,
        "schedules/custom_availability_category_history.html",
        {
            "custom_availability_category_history_table": _custom_availability_category_history_table(
                CustomAvailabilityCategoryChange.objects.select_related(
                    "category", "physician", "changed_by"
                )
            )
        },
    )


def _physician_history_table(changes):
    return _physician_change_table(
        identity="physician-history",
        label="Physician change history",
        changes=changes,
        include_physician=False,
    )


def _physicians_history_table(changes):
    return _physician_change_table(
        identity="physicians-history",
        label="All physician change history",
        changes=changes,
        include_physician=True,
    )


def _physician_change_table(*, identity, label, changes, include_physician):
    columns = ("Physician",) if include_physician else ()
    columns += ("Recorded", "Scheduler", "Fact", "Prior value", "Recorded value", "Note")
    return DataTable(
        identity=identity,
        label=label,
        columns=tuple(DataTableColumn(column) for column in columns),
        rows=tuple(
            DataTableRow(
                (
                    *(
                        (
                            DataTableCell(
                                change.physician.display_name,
                                kind="link",
                                url=reverse("physician-detail", args=(change.physician.pk,)),
                                row_header=True,
                            ),
                        )
                        if include_physician
                        else ()
                    ),
                    history_timestamp(change.changed_at),
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(
                        change.get_subject_display(),
                        kind="metadata",
                        row_header=not include_physician,
                    ),
                    _physician_change_cell(change.previous_value, change),
                    _physician_change_cell(change.recorded_value, change),
                    DataTableCell(change.reason or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No changes recorded.",
    )


def _physician_change_cell(value, change):
    if change.subject == PhysicianChange.Subject.MONTHLY_AVAILABILITY_RULE and value:
        return DataTableCell(
            monthly_rule_summary_values(
                weekday=MonthlyPhysicianAvailabilityRule.WEEKDAY_CHOICES[value["weekday"]][1],
                ordinals=value["ordinals"],
                periods=value["periods"],
                availability_selection=value["availability_selection"],
                effective_start=_history_date(value["effective_start"]),
                effective_end=_history_date(value["effective_end"]),
                annual_start_month=value.get("annual_start_month"),
                annual_end_month=value.get("annual_end_month"),
            )
        )
    return DataTableCell(retained_value(value), kind="code")


def _history_date(value):
    return date.fromisoformat(value) if value else None


def _custom_availability_category_history_table(changes):
    return DataTable(
        identity="schedule-option-history",
        label="Schedule option history",
        columns=tuple(
            DataTableColumn(label)
            for label in (
                "Recorded",
                "Scheduler",
                "Schedule option",
                "Physician",
                "Change",
                "Prior value",
                "Recorded value",
                "Note",
            )
        ),
        rows=tuple(
            DataTableRow(
                (
                    history_timestamp(change.changed_at),
                    DataTableCell(change.changed_by.get_username()),
                    DataTableCell(change.category.name, row_header=True),
                    DataTableCell(
                        change.physician.display_name if change.physician else "—", kind="metadata"
                    ),
                    DataTableCell(change.get_action_display(), kind="status"),
                    DataTableCell(retained_value(change.previous_value), kind="code"),
                    DataTableCell(retained_value(change.recorded_value), kind="code"),
                    DataTableCell(change.reason or "No note.", kind="metadata"),
                )
            )
            for change in changes
        ),
        empty_state="No schedule option changes recorded.",
    )


@login_required
@scheduling_administration_required
def physician_role_add(request, physician_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    form = EffectiveRoleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            add_role(physician=physician, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/effective_fact_form.html",
        {
            "form": form,
            "title": "Add role",
            "physician": physician,
            "existing_roles": physician.roles.all(),
            "existing_roles_table": _existing_roles_table(physician.roles.all()),
        },
    )


@login_required
@scheduling_administration_required
def physician_role_correct(request, physician_id, role_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    role = get_object_or_404(EffectiveRole, pk=role_id, physician=physician)
    form = EffectiveRoleForm(
        request.POST or None,
        initial={
            "role": role.role,
            "effective_start": role.effective_start,
            "effective_end": role.effective_end,
            "faculty_equivalent": role.faculty_equivalent,
            "faculty_leadership": role.faculty_leadership,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_role(effective_role=role, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/effective_fact_form.html",
        {
            "form": form,
            "title": f"Edit {role.get_role_display()}",
            "physician": physician,
            "existing_roles": physician.roles.all(),
            "existing_roles_table": _existing_roles_table(physician.roles.all(), current_role=role),
            "current_role": role,
        },
    )


@login_required
@scheduling_administration_required
def physician_role_end(request, physician_id, role_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    role = get_object_or_404(EffectiveRole, pk=role_id, physician=physician)
    form = EndEffectiveFactForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            end_role(effective_role=role, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/effective_fact_form.html",
        {"form": form, "title": f"End {role.get_role_display()}", "physician": physician},
    )


@login_required
def faculty_availability_add(request, physician_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    form = FacultyAvailabilityTemplateForm(
        request.POST or None,
        physician=physician,
        initial={"effective_start": organization_today()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_faculty_availability_template(
                physician=physician,
                effective_start=form.cleaned_data["effective_start"],
                effective_end=form.cleaned_data["effective_end"],
                slots=form.slot_values(),
                scheduler=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/faculty_availability_form.html",
        {
            "form": form,
            "title": _schedule_template_title(
                physician, "Add", on_date=form.schedule_input_context_date
            ),
            "physician": physician,
            "community_float_schedule_inputs": float_schedule_input_mode(
                physician, on_date=form.schedule_input_context_date
            )
            == "community_float",
        },
    )


@login_required
def faculty_availability_correct(request, physician_id, template_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    template = get_object_or_404(FacultyAvailabilityTemplate, pk=template_id, physician=physician)
    form = FacultyAvailabilityTemplateForm(
        request.POST or None,
        physician=physician,
        template=template,
        initial={
            "effective_start": template.effective_start,
            "effective_end": template.effective_end,
            **{
                slot: FacultyAvailabilityTemplate.selection_choice_value(selection)
                for slot, selection in template.slots.items()
            },
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_faculty_availability_template(
                template=template,
                effective_start=form.cleaned_data["effective_start"],
                effective_end=form.cleaned_data["effective_end"],
                slots=form.slot_values(),
                scheduler=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/faculty_availability_form.html",
        {
            "form": form,
            "title": _schedule_template_title(
                physician, "Edit", on_date=form.schedule_input_context_date
            ),
            "physician": physician,
            "community_float_schedule_inputs": float_schedule_input_mode(
                physician, on_date=form.schedule_input_context_date
            )
            == "community_float",
        },
    )


@login_required
def faculty_availability_start_later(request, physician_id, template_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    template = get_object_or_404(FacultyAvailabilityTemplate, pk=template_id, physician=physician)
    form = FacultyAvailabilityTemplateForm(
        request.POST or None,
        physician=physician,
        transition_from=template,
        initial={"effective_start": organization_today()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            start_later_faculty_availability_template(
                template=template,
                effective_start=form.cleaned_data["effective_start"],
                slots=form.slot_values(),
                scheduler=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/faculty_availability_form.html",
        {
            "form": form,
            "title": _schedule_template_title(
                physician, "Start later", on_date=form.schedule_input_context_date
            ),
            "physician": physician,
            "transition": True,
            "community_float_schedule_inputs": float_schedule_input_mode(
                physician, on_date=form.schedule_input_context_date
            )
            == "community_float",
        },
    )


@login_required
def monthly_availability_rule_create(request, physician_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    form = MonthlyPhysicianAvailabilityRuleForm(
        request.POST or None,
        physician=physician,
        initial={"effective_start": organization_today()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_monthly_physician_availability_rule(
                physician=physician,
                weekday=form.cleaned_data["weekday"],
                ordinals=form.cleaned_data["ordinals"],
                periods=form.cleaned_data["periods"],
                availability_selection=form.availability_selection(),
                effective_start=form.cleaned_data["effective_start"],
                effective_end=form.cleaned_data["effective_end"],
                annual_start_month=form.cleaned_data["annual_start_month"],
                annual_end_month=form.cleaned_data["annual_end_month"],
                scheduler=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/monthly_availability_rule_form.html",
        _monthly_availability_rule_form_context(
            form=form, physician=physician, title="Add monthly scheduling rule"
        ),
    )


@login_required
def monthly_availability_rule_correct(request, physician_id, rule_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    rule = get_object_or_404(MonthlyPhysicianAvailabilityRule, pk=rule_id, physician=physician)
    form = MonthlyPhysicianAvailabilityRuleForm(
        request.POST or None,
        physician=physician,
        rule=rule,
        initial={
            "weekday": rule.weekday,
            "ordinals": [str(value) for value in rule.ordinals],
            "periods": rule.periods,
            "availability": FacultyAvailabilityTemplate.selection_choice_value(
                rule.availability_selection
            ),
            "effective_start": rule.effective_start,
            "effective_end": rule.effective_end,
            "annual_start_month": rule.annual_start_month,
            "annual_end_month": rule.annual_end_month,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_monthly_physician_availability_rule(
                rule=rule,
                weekday=form.cleaned_data["weekday"],
                ordinals=form.cleaned_data["ordinals"],
                periods=form.cleaned_data["periods"],
                availability_selection=form.availability_selection(),
                effective_start=form.cleaned_data["effective_start"],
                effective_end=form.cleaned_data["effective_end"],
                annual_start_month=form.cleaned_data["annual_start_month"],
                annual_end_month=form.cleaned_data["annual_end_month"],
                scheduler=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/monthly_availability_rule_form.html",
        _monthly_availability_rule_form_context(
            form=form,
            physician=physician,
            rule=rule,
            deactivate_form=MonthlyPhysicianAvailabilityRuleDeactivateForm(),
            title="Edit monthly scheduling rule",
        ),
    )


@login_required
def monthly_availability_rule_deactivate(request, physician_id, rule_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    rule = get_object_or_404(MonthlyPhysicianAvailabilityRule, pk=rule_id, physician=physician)
    if request.method != "POST":
        return redirect("physician-monthly-availability-rule-correct", physician.pk, rule.pk)
    deactivate_form = MonthlyPhysicianAvailabilityRuleDeactivateForm(request.POST)
    if deactivate_form.is_valid():
        deactivate_monthly_physician_availability_rule(
            rule=rule, scheduler=request.user, reason=deactivate_form.cleaned_data["reason"]
        )
        return redirect("physician-detail", physician_id=physician.pk)
    form = MonthlyPhysicianAvailabilityRuleForm(
        physician=physician,
        rule=rule,
        initial={
            "weekday": rule.weekday,
            "ordinals": [str(value) for value in rule.ordinals],
            "periods": rule.periods,
            "availability": FacultyAvailabilityTemplate.selection_choice_value(
                rule.availability_selection
            ),
            "effective_start": rule.effective_start,
            "effective_end": rule.effective_end,
            "annual_start_month": rule.annual_start_month,
            "annual_end_month": rule.annual_end_month,
        },
    )
    return render(
        request,
        "schedules/monthly_availability_rule_form.html",
        _monthly_availability_rule_form_context(
            form=form,
            physician=physician,
            rule=rule,
            deactivate_form=deactivate_form,
            title="Edit monthly scheduling rule",
        ),
    )


def _monthly_availability_rule_form_context(
    *, form, physician, title, rule=None, deactivate_form=None
):
    recurrence_rules = tuple(physician.monthly_availability_rules.all())
    return {
        "form": form,
        "physician": physician,
        "rule": rule,
        "deactivate_form": deactivate_form,
        "title": title,
        "community_float_schedule_inputs": float_schedule_input_mode(
            physician,
            on_date=getattr(form, "schedule_input_context_date", organization_today()),
        )
        == "community_float",
        "recurrence_rules": recurrence_rules,
        "current_rule": rule,
        "fixed_physician": physician,
        "recurrence_rules_show_coverage": True,
        "recurrence_rules_show_availability": True,
        "recurrence_rule_context_table": _recurrence_rule_context_table(
            recurrence_rules,
            current_rule=rule,
            fixed_physician=physician,
            show_coverage=True,
            show_availability=True,
            monthly_summary=True,
        ),
        "recurrence_overlap_guidance": (
            "Active rules cannot cover the same physician, weekday, and period. Save to check conflicts."
        ),
    }


def _schedule_template_title(physician, action, *, on_date=None):
    label = (
        "Float availability"
        if float_schedule_input_mode(physician, on_date=on_date) == "community_float"
        else "Faculty Schedule"
    )
    return f"{action} {label} template"


@login_required
def physician_casual_rounder_rule_create(request, physician_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    form_data = request.POST.copy() if request.method == "POST" else None
    if form_data is not None:
        form_data["physician"] = physician.pk
    form = CasualRounderRuleForm(
        form_data,
        initial={"physician": physician.pk, "effective_start": organization_today()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_casual_rounder_rule(scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/rounding_form.html",
        _recurrence_rule_form_context(
            form=form,
            rule_model=CasualRounderRule,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            title="Add casual rounder rule",
            fixed_physician=physician,
            return_url=reverse("physician-detail", args=(physician.pk,)),
        ),
    )


@login_required
def physician_casual_rounder_rule_correct(request, physician_id, rule_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    rule = get_object_or_404(CasualRounderRule, pk=rule_id, physician=physician)
    form_data = request.POST.copy() if request.method == "POST" else None
    if form_data is not None:
        form_data["physician"] = physician.pk
    form = CasualRounderRuleForm(
        form_data,
        initial={
            "physician": physician.pk,
            "weekday": rule.weekday,
            "ordinals": [str(value) for value in rule.ordinals],
            "effective_start": rule.effective_start,
            "effective_end": rule.effective_end,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_casual_rounder_rule(rule=rule, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/rounding_form.html",
        _recurrence_rule_form_context(
            form=form,
            rule_model=CasualRounderRule,
            role=EffectiveRole.Role.CASUAL_ROUNDER,
            title="Edit casual rounder rule",
            casual_rule=rule,
            current_rule=rule,
            fixed_physician=physician,
            return_url=reverse("physician-detail", args=(physician.pk,)),
            remove_action=reverse(
                "physician-casual-rounder-rule-remove", args=(physician.pk, rule.pk)
            ),
        ),
    )


@login_required
def physician_casual_rounder_rule_remove(request, physician_id, rule_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    rule = get_object_or_404(CasualRounderRule, pk=rule_id, physician=physician)
    if request.method != "POST":
        return redirect("physician-casual-rounder-rule-correct", physician.pk, rule.pk)
    form = RoundingRemovalForm(request.POST)
    if form.is_valid():
        remove_casual_rounder_rule(
            rule=rule, scheduler=request.user, reason=form.cleaned_data["reason"]
        )
        return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/rounding_form.html",
        {
            "form": CasualRounderRuleForm(
                initial={
                    "physician": physician.pk,
                    "weekday": rule.weekday,
                    "ordinals": [str(value) for value in rule.ordinals],
                    "effective_start": rule.effective_start,
                    "effective_end": rule.effective_end,
                }
            ),
            "remove_form": form,
            "title": "Remove casual rounder rule",
            "casual_rule": rule,
            "fixed_physician": physician,
            "return_url": reverse("physician-detail", args=(physician.pk,)),
            "remove_action": reverse(
                "physician-casual-rounder-rule-remove", args=(physician.pk, rule.pk)
            ),
        },
    )


@login_required
def physician_community_preceptor_rule_create(request, physician_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    form_data = request.POST.copy() if request.method == "POST" else None
    if form_data is not None:
        form_data["physician"] = physician.pk
    form = CommunityPreceptorRuleForm(
        form_data,
        initial={"physician": physician.pk, "effective_start": organization_today()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_community_preceptor_rule(scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/community_preceptor_form.html",
        _recurrence_rule_form_context(
            form=form,
            rule_model=CommunityPreceptorRule,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            title="Add Community Preceptor rule",
            fixed_physician=physician,
            return_url=reverse("physician-detail", args=(physician.pk,)),
        ),
    )


@login_required
def physician_community_preceptor_rule_correct(request, physician_id, rule_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    rule = get_object_or_404(CommunityPreceptorRule, pk=rule_id, physician=physician)
    form_data = request.POST.copy() if request.method == "POST" else None
    if form_data is not None:
        form_data["physician"] = physician.pk
    form = CommunityPreceptorRuleForm(
        form_data,
        initial={
            "physician": physician.pk,
            "weekday": rule.weekday,
            "ordinals": [str(value) for value in rule.ordinals],
            "periods": list(rule.periods),
            "effective_start": rule.effective_start,
            "effective_end": rule.effective_end,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            correct_community_preceptor_rule(rule=rule, scheduler=request.user, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/community_preceptor_form.html",
        _recurrence_rule_form_context(
            form=form,
            rule_model=CommunityPreceptorRule,
            role=EffectiveRole.Role.COMMUNITY_PRECEPTOR,
            title="Edit Community Preceptor rule",
            rule=rule,
            current_rule=rule,
            fixed_physician=physician,
            return_url=reverse("physician-detail", args=(physician.pk,)),
            remove_action=reverse(
                "physician-community-preceptor-rule-remove", args=(physician.pk, rule.pk)
            ),
        ),
    )


@login_required
def physician_community_preceptor_rule_remove(request, physician_id, rule_id):
    physician = get_object_or_404(Physician, pk=physician_id)
    rule = get_object_or_404(CommunityPreceptorRule, pk=rule_id, physician=physician)
    if request.method != "POST":
        return redirect("physician-community-preceptor-rule-correct", physician.pk, rule.pk)
    form = CommunityPreceptorRemovalForm(request.POST)
    if form.is_valid():
        remove_community_preceptor_rule(
            rule=rule, scheduler=request.user, reason=form.cleaned_data["reason"]
        )
        return redirect("physician-detail", physician_id=physician.pk)
    return render(
        request,
        "schedules/community_preceptor_form.html",
        {
            "form": CommunityPreceptorRuleForm(
                initial={
                    "physician": physician.pk,
                    "weekday": rule.weekday,
                    "ordinals": [str(value) for value in rule.ordinals],
                    "periods": list(rule.periods),
                    "effective_start": rule.effective_start,
                    "effective_end": rule.effective_end,
                }
            ),
            "remove_form": form,
            "title": "Remove Community Preceptor rule",
            "rule": rule,
            "fixed_physician": physician,
            "return_url": reverse("physician-detail", args=(physician.pk,)),
            "remove_action": reverse(
                "physician-community-preceptor-rule-remove", args=(physician.pk, rule.pk)
            ),
        },
    )
