"""URL routing for the preceptor scheduling project."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from preceptor_project.health import health
from schedules.auth_views import MurielLoginView, MurielPasswordResetView
from schedules.forms.accounts import UsernameOrEmailAuthenticationForm
from schedules.physician_scope import set_physician_scope
from schedules.views import (
    academic_events,
    accounts,
    community_preceptors,
    holidays,
    interviews,
    late_docs,
    lead_times,
    master_schedule,
    physicians,
    precepting_imports,
    preceptors,
    reports,
    rounding,
    solver,
    stipend_rates,
    vacation_imports,
    vacations,
    work_remaining,
)

urlpatterns = [
    path("health/", health, name="health"),
    path("presentation-scope/", set_physician_scope, name="physician-scope"),
    path(
        "sign-in/",
        MurielLoginView.as_view(
            authentication_form=UsernameOrEmailAuthenticationForm,
            template_name="registration/sign_in.html",
        ),
        name="sign-in",
    ),
    path(
        "sign-out/",
        auth_views.LogoutView.as_view(next_page="sign-in"),
        name="sign-out",
    ),
    path(
        "password/reset/",
        MurielPasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.html",
            subject_template_name="registration/password_reset_subject.txt",
            success_url=reverse_lazy("password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password/reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="registration/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "password/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            success_url=reverse_lazy("password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "password/reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
    path(
        "password/change/",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change_form.html",
            success_url=reverse_lazy("password_change_done"),
        ),
        name="password_change",
    ),
    path(
        "password/change/done/",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="registration/password_change_done.html"
        ),
        name="password_change_done",
    ),
    path("admin/", admin.site.urls),
    path("accounts/", accounts.account_workspace, name="account-workspace"),
    path("accounts/invite/", accounts.account_invite, name="account-invite"),
    path("accounts/history/", accounts.account_history, name="account-history"),
    path(
        "accounts/<int:account_id>/invitation/resend/",
        accounts.account_invitation_resend,
        name="account-invitation-resend",
    ),
    path(
        "accounts/<int:account_id>/invitation/revoke/",
        accounts.account_invitation_revoke,
        name="account-invitation-revoke",
    ),
    path("accounts/<int:account_id>/", accounts.account_edit, name="account-edit"),
    path("reports/", reports.schedule_reports, name="schedule-reports"),
    path("reports/schedule.pdf", reports.schedule_report_pdf, name="schedule-report-pdf"),
    path(
        "reports/community-preceptor-stipend/",
        reports.community_preceptor_stipend_report,
        name="community-preceptor-stipend-report",
    ),
    path(
        "reports/late-doc-stipend/", reports.late_doc_stipend_report, name="late-doc-stipend-report"
    ),
    path(
        "reports/admin-time/", reports.faculty_admin_time_report, name="faculty-admin-time-report"
    ),
    path(
        "reports/community-preceptor-stipend.pdf",
        reports.community_preceptor_stipend_pdf,
        name="community-preceptor-stipend-pdf",
    ),
    path("reports/late-doc-stipend.pdf", reports.late_doc_stipend_pdf, name="late-doc-stipend-pdf"),
    path(
        "reports/stipend-rates/",
        stipend_rates.stipend_rate_workspace,
        name="stipend-rate-workspace",
    ),
    path(
        "reports/stipend-rates/<int:rate_id>/correct/",
        stipend_rates.stipend_rate_correct,
        name="stipend-rate-correct",
    ),
    path(
        "reports/stipend-rates/<int:rate_id>/remove/",
        stipend_rates.stipend_rate_remove,
        name="stipend-rate-remove",
    ),
    path(
        "reports/stipend-rates/<str:kind>/initial/",
        stipend_rates.stipend_rate_initial,
        name="stipend-rate-initial",
    ),
    path(
        "reports/stipend-rates/<str:kind>/<str:direction>/",
        stipend_rates.stipend_rate_append,
        name="stipend-rate-append",
    ),
    path(
        "reports/stipend-rates/history/",
        stipend_rates.stipend_rate_history,
        name="stipend-rate-history",
    ),
    path(
        "invitation/complete/",
        accounts.scheduler_invitation_complete,
        name="scheduler-invitation-complete",
    ),
    path(
        "invitation/<str:token>/",
        accounts.scheduler_invitation_accept,
        name="scheduler-invitation-accept",
    ),
    path("", preceptors.monthly_workspace, name="monthly-workspace"),
    path("pdf/", preceptors.precepting_calendar_pdf, name="precepting-calendar-pdf"),
    path("schedule/pdf/", master_schedule.master_schedule_pdf, name="master-schedule-pdf"),
    path(
        "schedule/work-remaining/",
        work_remaining.work_remaining_workspace,
        name="work-remaining",
    ),
    path("schedule/lead-times/", lead_times.lead_time_workspace, name="lead-time-workspace"),
    path("schedule/lead-times/history/", lead_times.lead_time_history, name="lead-time-history"),
    path(
        "schedule/lead-times/<str:family>/edit/", lead_times.lead_time_edit, name="lead-time-edit"
    ),
    path("schedule/", master_schedule.master_schedule_workspace, name="master-schedule"),
    path("list/", preceptors.monthly_list, name="preceptor-list"),
    path("exceptions/", preceptors.scheduling_exceptions, name="scheduling-exceptions"),
    path("solve/", solver.solver_workspace, name="precepting-solver-workspace"),
    path(
        "import/", precepting_imports.precepting_workbook_import, name="precepting-workbook-import"
    ),
    path("solve/<int:proposal_id>/", solver.solver_review, name="precepting-solver-review"),
    path(
        "solve/<int:proposal_id>/performance-probe-cleanup/",
        solver.solver_performance_probe_cleanup,
        name="precepting-solver-performance-probe-cleanup",
    ),
    path(
        "solve/<int:proposal_id>/reconcile/",
        solver.solver_reconcile,
        name="precepting-solver-reconcile",
    ),
    path(
        "solve/<int:proposal_id>/week/<str:on_date>/",
        solver.solver_proposal_week,
        name="precepting-solver-week",
    ),
    path(
        "solve/<int:proposal_id>/assignment/<str:assignment_type>/<str:on_date>/<str:period_of_day>/<str:position>/",
        solver.solver_assignment_edit,
        name="precepting-solver-assignment-edit",
    ),
    path(
        "solve/<int:proposal_id>/compare/", solver.solver_compare, name="precepting-solver-compare"
    ),
    path("solve/<int:proposal_id>/apply/", solver.solver_apply, name="precepting-solver-apply"),
    path(
        "solve/<int:proposal_id>/late-doc/<str:on_date>/",
        solver.solver_late_doc_review,
        name="precepting-solver-late-doc-review",
    ),
    path("week/late-doc/<str:on_date>/", late_docs.late_doc_edit, name="late-doc-edit"),
    path("week/late-doc/<str:on_date>/remove/", late_docs.late_doc_remove, name="late-doc-remove"),
    path(
        "week/late-doc/<str:on_date>/history/", late_docs.late_doc_history, name="late-doc-history"
    ),
    path("vacations/", vacations.vacation_workspace, name="vacation-workspace"),
    path("vacations/list/", vacations.vacation_list, name="vacation-list"),
    path(
        "vacations/import/",
        vacation_imports.vacation_calendar_import,
        name="vacation-calendar-import",
    ),
    path("vacations/holidays/", holidays.holiday_workspace, name="holiday-workspace"),
    path("vacations/holidays/history/", holidays.holiday_history, name="holiday-history"),
    path(
        "vacations/holidays/<int:occurrence_id>/schedule/",
        holidays.holiday_occurrence_schedule,
        name="holiday-occurrence-schedule",
    ),
    path(
        "vacations/holidays/<int:occurrence_id>/correct/",
        holidays.holiday_occurrence_correct,
        name="holiday-occurrence-correct",
    ),
    path("vacations/add/", vacations.vacation_create, name="vacation-create"),
    path("vacations/history/", vacations.vacation_history, name="vacation-history"),
    path(
        "vacations/<int:vacation_id>/correct/", vacations.vacation_correct, name="vacation-correct"
    ),
    path("vacations/<int:vacation_id>/remove/", vacations.vacation_remove, name="vacation-remove"),
    path(
        "academic-events/",
        academic_events.academic_event_workspace,
        name="academic-event-workspace",
    ),
    path("academic-events/list/", academic_events.academic_event_list, name="academic-event-list"),
    path(
        "academic-events/add/", academic_events.academic_event_create, name="academic-event-create"
    ),
    path(
        "academic-events/definitions/<int:definition_id>/not-occurring/",
        academic_events.academic_event_mark_not_occurring,
        name="academic-event-mark-not-occurring",
    ),
    path(
        "academic-events/history/",
        academic_events.academic_event_history,
        name="academic-event-history",
    ),
    path("interviews/", interviews.interview_workspace, name="interview-workspace"),
    path("interviews/list/", interviews.interview_list, name="interview-list"),
    path("interviews/add/", interviews.interview_create, name="interview-create"),
    path("interviews/history/", interviews.interview_history, name="interview-history"),
    path(
        "interviews/<int:shift_id>/correct/",
        interviews.interview_correct,
        name="interview-correct",
    ),
    path(
        "interviews/<int:shift_id>/remove/",
        interviews.interview_remove,
        name="interview-remove",
    ),
    path(
        "academic-events/<int:event_id>/correct/",
        academic_events.academic_event_correct,
        name="academic-event-correct",
    ),
    path(
        "academic-events/<int:event_id>/assignments/correct/",
        academic_events.academic_event_monthly_assignment_correct,
        name="academic-event-monthly-assignment-correct",
    ),
    path(
        "academic-events/<int:event_id>/remove/",
        academic_events.academic_event_remove,
        name="academic-event-remove",
    ),
    path(
        "academic-events/<int:event_id>/reverse-not-occurring/",
        academic_events.academic_event_reverse_not_occurring,
        name="academic-event-reverse-not-occurring",
    ),
    path(
        "event-definitions/",
        academic_events.academic_event_definition_workspace,
        name="academic-event-definition-workspace",
    ),
    path(
        "event-definitions/add/",
        academic_events.academic_event_definition_create,
        name="academic-event-definition-create",
    ),
    path(
        "event-definitions/import/",
        academic_events.academic_event_definition_import,
        name="academic-event-definition-import",
    ),
    path(
        "academic-events/import/",
        academic_events.academic_event_schedule_import,
        name="academic-event-schedule-import",
    ),
    path(
        "event-definitions/history/",
        academic_events.academic_event_definition_history,
        name="academic-event-definition-history",
    ),
    path(
        "event-definitions/<int:definition_id>/correct/",
        academic_events.academic_event_definition_correct,
        name="academic-event-definition-correct",
    ),
    path(
        "event-definitions/<int:definition_id>/remove/",
        academic_events.academic_event_definition_remove,
        name="academic-event-definition-remove",
    ),
    path(
        "event-definitions/<int:definition_id>/rules/add/",
        academic_events.academic_event_monthly_rule_create,
        name="academic-event-monthly-rule-create",
    ),
    path(
        "event-rules/<int:rule_id>/correct/",
        academic_events.academic_event_monthly_rule_correct,
        name="academic-event-monthly-rule-correct",
    ),
    path(
        "event-rules/<int:rule_id>/remove/",
        academic_events.academic_event_monthly_rule_remove,
        name="academic-event-monthly-rule-remove",
    ),
    path("rounding/", rounding.rounding_workspace, name="rounding-workspace"),
    path("rounding/list/", rounding.rounding_list, name="rounding-list"),
    path("rounding/normal/", rounding.normal_rounder_workspace, name="normal-rounder-workspace"),
    path("rounding/casual/", rounding.casual_rounder_workspace, name="casual-rounder-workspace"),
    path("rounding/normal/add/", rounding.normal_rounder_record, name="normal-rounder-record"),
    path(
        "rounding/normal/<int:normal_rounder_id>/correct/",
        rounding.normal_rounder_correct,
        name="normal-rounder-correct",
    ),
    path(
        "rounding/normal/<int:normal_rounder_id>/remove/",
        rounding.normal_rounder_remove,
        name="normal-rounder-remove",
    ),
    path(
        "rounding/casual/add/",
        rounding.casual_rounder_rule_create,
        name="casual-rounder-rule-create",
    ),
    path(
        "rounding/casual/add/<int:physician_id>/",
        rounding.casual_rounder_rule_create_for_physician,
        name="casual-rounder-rule-create-for-physician",
    ),
    path(
        "rounding/casual/<int:rule_id>/correct/",
        rounding.casual_rounder_rule_correct,
        name="casual-rounder-rule-correct",
    ),
    path(
        "rounding/casual/<int:rule_id>/remove/",
        rounding.casual_rounder_rule_remove,
        name="casual-rounder-rule-remove",
    ),
    path("rounding/history/", rounding.rounding_history, name="rounding-history"),
    path(
        "community-preceptors/",
        community_preceptors.community_preceptor_workspace,
        name="community-preceptor-workspace",
    ),
    path(
        "community-preceptors/list/",
        community_preceptors.community_preceptor_list,
        name="community-preceptor-list",
    ),
    path(
        "community-preceptors/rules/",
        community_preceptors.community_preceptor_rules,
        name="community-preceptor-rules",
    ),
    path(
        "community-preceptors/add/",
        community_preceptors.community_preceptor_rule_create,
        name="community-preceptor-rule-create",
    ),
    path(
        "community-preceptors/<int:rule_id>/correct/",
        community_preceptors.community_preceptor_rule_correct,
        name="community-preceptor-rule-correct",
    ),
    path(
        "community-preceptors/<int:rule_id>/remove/",
        community_preceptors.community_preceptor_rule_remove,
        name="community-preceptor-rule-remove",
    ),
    path(
        "community-preceptors/history/",
        community_preceptors.community_preceptor_history,
        name="community-preceptor-history",
    ),
    path(
        "sessions/<int:session_id>/assignments/history/",
        preceptors.session_assignment_history,
        name="session-assignment-history",
    ),
    path(
        "sessions/<int:session_id>/assignments/<str:position>/",
        preceptors.session_assignment_edit,
        name="session-assignment-edit",
    ),
    path(
        "sessions/<int:session_id>/assignments/<str:position>/remove/",
        preceptors.session_assignment_remove,
        name="session-assignment-remove",
    ),
    path(
        "sessions/<int:session_id>/findings/acknowledge/",
        preceptors.session_finding_acknowledge,
        name="session-finding-acknowledge",
    ),
    path(
        "sessions/<int:session_id>/findings/override/remove/",
        preceptors.session_finding_override_remove,
        name="session-finding-override-remove",
    ),
    path("physicians/", physicians.physicians_directory, name="physicians-directory"),
    path("physicians/history/", physicians.physicians_history, name="physicians-history"),
    path(
        "physicians/availability-categories/",
        physicians.custom_availability_category_workspace,
        name="custom-availability-category-workspace",
    ),
    path(
        "physicians/availability-categories/add/",
        physicians.custom_availability_category_create,
        name="custom-availability-category-create",
    ),
    path(
        "physicians/availability-categories/history/",
        physicians.custom_availability_category_history,
        name="custom-availability-category-history",
    ),
    path(
        "physicians/availability-categories/<int:category_id>/",
        physicians.custom_availability_category_edit,
        name="custom-availability-category-edit",
    ),
    path("physicians/add/", physicians.physician_create, name="physician-create"),
    path("physicians/<int:physician_id>/", physicians.physician_detail, name="physician-detail"),
    path(
        "physicians/<int:physician_id>/history/",
        physicians.physician_history,
        name="physician-history",
    ),
    path(
        "physicians/<int:physician_id>/roles/add/",
        physicians.physician_role_add,
        name="physician-role-add",
    ),
    path(
        "physicians/<int:physician_id>/roles/<int:role_id>/correct/",
        physicians.physician_role_correct,
        name="physician-role-correct",
    ),
    path(
        "physicians/<int:physician_id>/roles/<int:role_id>/end/",
        physicians.physician_role_end,
        name="physician-role-end",
    ),
    path(
        "physicians/<int:physician_id>/faculty-schedule/add/",
        physicians.faculty_availability_add,
        name="physician-faculty-availability-add",
    ),
    path(
        "physicians/<int:physician_id>/faculty-schedule/<int:template_id>/correct/",
        physicians.faculty_availability_correct,
        name="physician-faculty-availability-correct",
    ),
    path(
        "physicians/<int:physician_id>/faculty-schedule/<int:template_id>/start-later/",
        physicians.faculty_availability_start_later,
        name="physician-faculty-availability-start-later",
    ),
    path(
        "physicians/<int:physician_id>/monthly-availability-rules/add/",
        physicians.monthly_availability_rule_create,
        name="physician-monthly-availability-rule-create",
    ),
    path(
        "physicians/<int:physician_id>/monthly-availability-rules/<int:rule_id>/correct/",
        physicians.monthly_availability_rule_correct,
        name="physician-monthly-availability-rule-correct",
    ),
    path(
        "physicians/<int:physician_id>/monthly-availability-rules/<int:rule_id>/deactivate/",
        physicians.monthly_availability_rule_deactivate,
        name="physician-monthly-availability-rule-deactivate",
    ),
    path(
        "physicians/<int:physician_id>/community-preceptor-schedule/add/",
        physicians.physician_community_preceptor_rule_create,
        name="physician-community-preceptor-rule-create",
    ),
    path(
        "physicians/<int:physician_id>/community-preceptor-schedule/<int:rule_id>/correct/",
        physicians.physician_community_preceptor_rule_correct,
        name="physician-community-preceptor-rule-correct",
    ),
    path(
        "physicians/<int:physician_id>/community-preceptor-schedule/<int:rule_id>/remove/",
        physicians.physician_community_preceptor_rule_remove,
        name="physician-community-preceptor-rule-remove",
    ),
    path(
        "physicians/<int:physician_id>/casual-rounder-rules/add/",
        physicians.physician_casual_rounder_rule_create,
        name="physician-casual-rounder-rule-create",
    ),
    path(
        "physicians/<int:physician_id>/casual-rounder-rules/<int:rule_id>/correct/",
        physicians.physician_casual_rounder_rule_correct,
        name="physician-casual-rounder-rule-correct",
    ),
    path(
        "physicians/<int:physician_id>/casual-rounder-rules/<int:rule_id>/remove/",
        physicians.physician_casual_rounder_rule_remove,
        name="physician-casual-rounder-rule-remove",
    ),
]

handler400 = "preceptor_project.security.bad_request"
handler403 = "preceptor_project.security.permission_denied"
handler404 = "preceptor_project.security.page_not_found"
handler500 = "preceptor_project.security.server_error"
