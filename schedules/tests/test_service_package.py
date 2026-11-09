from importlib import import_module

from django.test import SimpleTestCase


class ServicePackageImportTests(SimpleTestCase):
    def test_shared_serialization_has_a_direct_owner(self):
        services = import_module("schedules.services")
        import_module("schedules.services._mutation")

        self.assertFalse(hasattr(services, "serialized_scheduling_state"))

    def test_physician_owner_is_direct_and_not_reexported_by_the_root(self):
        services = import_module("schedules.services")
        import_module("schedules.services.physicians")

        self.assertFalse(hasattr(services, "create_physician"))
        self.assertFalse(hasattr(services, "faculty_availability_on"))

    def test_vacation_and_academic_event_owners_are_direct_and_not_reexported_by_the_root(self):
        services = import_module("schedules.services")
        import_module("schedules.services.vacations")
        import_module("schedules.services.academic_events")

        self.assertFalse(hasattr(services, "create_vacation"))
        self.assertFalse(hasattr(services, "create_academic_event_occurrence"))

    def test_rounding_and_community_owners_are_direct_and_not_reexported_by_the_root(self):
        services = import_module("schedules.services")
        import_module("schedules.services.rounding")
        import_module("schedules.services.community")

        self.assertFalse(hasattr(services, "record_normal_rounder"))
        self.assertFalse(hasattr(services, "create_community_preceptor_rule"))

    def test_availability_and_slot_projection_owners_are_direct_and_not_reexported_by_the_root(
        self,
    ):
        services = import_module("schedules.services")
        self.assertFalse(hasattr(services, "effective_preceptor_availability_on"))
        self.assertFalse(hasattr(services, "RequestedSlot"))

        import_module("schedules.services.availability")
        import_module("schedules.services.slot_projection")

    def test_interview_owner_is_direct_and_not_reexported_by_the_root(self):
        services = import_module("schedules.services")
        import_module("schedules.services.interviews")

        self.assertFalse(hasattr(services, "create_interview_shift"))
        self.assertFalse(hasattr(services, "interview_schedule_summary"))

    def test_preceptor_owner_is_direct_and_not_reexported_by_the_root(self):
        services = import_module("schedules.services")
        import_module("schedules.services.preceptors")

        self.assertFalse(hasattr(services, "assign_preceptor"))
        self.assertFalse(hasattr(services, "open_monthly_draft"))
