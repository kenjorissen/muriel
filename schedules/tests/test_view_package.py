from importlib import import_module

from django.test import SimpleTestCase


class ViewPackageImportTests(SimpleTestCase):
    def test_remaining_view_owners_are_direct_and_not_reexported_by_the_root(self):
        views = import_module("schedules.views")
        import_module("schedules.views.preceptors")
        import_module("schedules.views.physicians")

        self.assertFalse(hasattr(views, "monthly_workspace"))
        self.assertFalse(hasattr(views, "physician_detail"))

    def test_form_root_has_no_domain_reexports(self):
        forms = import_module("schedules.forms")
        import_module("schedules.forms.vacations")
        import_module("schedules.forms.preceptors")

        self.assertFalse(hasattr(forms, "VacationForm"))
        self.assertFalse(hasattr(forms, "AssignmentForm"))

    def test_model_root_discovers_concrete_models_without_owning_them(self):
        models = import_module("schedules.models")

        self.assertEqual(models.Physician.__module__, "schedules.models.physicians")
        self.assertEqual(models.Vacation.__module__, "schedules.models.vacations")
        self.assertEqual(models.AcademicEvent.__module__, "schedules.models.academic_events")
        self.assertEqual(models.NormalRounder.__module__, "schedules.models.rounding")
        self.assertEqual(
            models.CommunityPreceptorRule.__module__, "schedules.models.community_preceptors"
        )
        self.assertEqual(models.InterviewShift.__module__, "schedules.models.interviews")
        self.assertEqual(models.PreceptorSession.__module__, "schedules.models.preceptors")
