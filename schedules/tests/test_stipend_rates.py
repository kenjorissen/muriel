import json
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from threading import Barrier, Lock, Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from schedules.models import StipendRate, StipendRateChange
from schedules.services.stipend_rates import (
    append_stipend_rate,
    correct_stipend_rate,
    remove_stipend_rate,
    set_initial_stipend_rate,
    stipend_rate_on,
    stipend_rates,
)
from schedules.tests.test_table_presentation import _RenderedDataTable, _RenderedTableParser


class StipendRateServicesTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(
            username="administrator", password="test-password"
        )
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def set_initial(self, kind=StipendRate.Kind.LATE_DOC, hourly_rate="75.00"):
        return set_initial_stipend_rate(
            kind=kind, hourly_rate=hourly_rate, scheduler=self.administrator, note="Initial rate"
        )

    def timeline_state(self, kind):
        return (
            list(
                StipendRate.objects.filter(kind=kind)
                .order_by("effective_start", "pk")
                .values_list("pk", "hourly_rate", "effective_start", "effective_end")
            ),
            list(
                StipendRateChange.objects.filter(rate__kind=kind)
                .order_by("pk")
                .values_list("pk", "rate_id", "action", "previous_value", "recorded_value", "note")
            ),
        )

    def assert_rejected_without_mutation(self, kind, mutation):
        state = self.timeline_state(kind)

        with self.assertRaises(ValidationError):
            mutation()

        self.assertEqual(self.timeline_state(kind), state)

    def test_initial_rate_is_unbounded_and_retained(self):
        rate = self.set_initial()

        self.assertEqual((rate.effective_start, rate.effective_end), (None, None))
        self.assertEqual(StipendRateChange.objects.get().recorded_value["hourly_rate"], "75.00")

    def test_initial_rate_rejects_invalid_hourly_rates_without_retaining_history(self):
        for hourly_rate in ("0.00", "-1.00", "75.001"):
            with self.subTest(hourly_rate=hourly_rate):
                self.assert_rejected_without_mutation(
                    StipendRate.Kind.LATE_DOC,
                    lambda hourly_rate=hourly_rate: set_initial_stipend_rate(
                        kind=StipendRate.Kind.LATE_DOC,
                        hourly_rate=hourly_rate,
                        scheduler=self.administrator,
                    ),
                )

    def test_append_later_and_earlier_keep_a_gapless_timeline_and_history(self):
        initial = self.set_initial()
        earlier = append_stipend_rate(
            kind=initial.kind,
            hourly_rate="60.00",
            transition_date=date(2025, 1, 31),
            direction="earlier",
            scheduler=self.administrator,
            note="Earlier rate",
        )
        later = append_stipend_rate(
            kind=initial.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
            note="Later rate",
        )

        self.assertEqual(
            [
                (rate.hourly_rate, rate.effective_start, rate.effective_end)
                for rate in stipend_rates(initial.kind)
            ],
            [
                (Decimal("60.00"), None, date(2025, 1, 31)),
                (Decimal("75.00"), date(2025, 2, 1), date(2025, 2, 28)),
                (Decimal("90.00"), date(2025, 3, 1), None),
            ],
        )
        self.assertEqual(stipend_rate_on(kind=initial.kind, on_date=date(2025, 1, 31)), earlier)
        self.assertEqual(stipend_rate_on(kind=initial.kind, on_date=date(2025, 3, 1)), later)
        self.assertEqual(StipendRateChange.objects.count(), 5)
        self.assertEqual(StipendRateChange.objects.latest("pk").note, "Later rate")

    def test_repeated_later_subdivisions_keep_synthetic_timelines_in_chronological_order(self):
        for kind, initial_rate, transitions, expected in (
            (
                StipendRate.Kind.COMMUNITY_PRECEPTOR,
                "75.00",
                ((date(2022, 7, 1), "82.50"), (date(2025, 1, 1), "90.00")),
                [
                    (Decimal("75.00"), None, date(2022, 6, 30)),
                    (Decimal("82.50"), date(2022, 7, 1), date(2024, 12, 31)),
                    (Decimal("90.00"), date(2025, 1, 1), None),
                ],
            ),
            (
                StipendRate.Kind.LATE_DOC,
                "65.00",
                ((date(2023, 1, 1), "72.50"), (date(2025, 7, 1), "80.00")),
                [
                    (Decimal("65.00"), None, date(2022, 12, 31)),
                    (Decimal("72.50"), date(2023, 1, 1), date(2025, 6, 30)),
                    (Decimal("80.00"), date(2025, 7, 1), None),
                ],
            ),
        ):
            with self.subTest(kind=kind):
                self.assert_repeated_subdivision(kind, initial_rate, transitions, "later", expected)

    def test_repeated_earlier_subdivisions_keep_the_first_boundary_selected(self):
        for kind, initial_rate, transitions, expected in (
            (
                StipendRate.Kind.COMMUNITY_PRECEPTOR,
                "75.00",
                ((date(2025, 1, 31), "60.00"), (date(2024, 1, 31), "50.00")),
                [
                    (Decimal("50.00"), None, date(2024, 1, 31)),
                    (Decimal("60.00"), date(2024, 2, 1), date(2025, 1, 31)),
                    (Decimal("75.00"), date(2025, 2, 1), None),
                ],
            ),
            (
                StipendRate.Kind.LATE_DOC,
                "65.00",
                ((date(2025, 7, 31), "55.00"), (date(2024, 7, 31), "45.00")),
                [
                    (Decimal("45.00"), None, date(2024, 7, 31)),
                    (Decimal("55.00"), date(2024, 8, 1), date(2025, 7, 31)),
                    (Decimal("65.00"), date(2025, 8, 1), None),
                ],
            ),
        ):
            with self.subTest(kind=kind):
                self.assert_repeated_subdivision(
                    kind, initial_rate, transitions, "earlier", expected
                )

    def assert_repeated_subdivision(self, kind, initial_rate, transitions, direction, expected):
        self.set_initial(kind, initial_rate)
        for transition_date, hourly_rate in transitions:
            append_stipend_rate(
                kind=kind,
                hourly_rate=hourly_rate,
                transition_date=transition_date,
                direction=direction,
                scheduler=self.administrator,
            )
        self.assertEqual(
            [
                (rate.hourly_rate, rate.effective_start, rate.effective_end)
                for rate in stipend_rates(kind)
            ],
            expected,
        )
        self.assertEqual(StipendRateChange.objects.filter(rate__kind=kind).count(), 5)

    def test_append_rejects_transition_outside_the_period_being_split(self):
        initial = self.set_initial()
        append_stipend_rate(
            kind=initial.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        self.assert_rejected_without_mutation(
            initial.kind,
            lambda: append_stipend_rate(
                kind=initial.kind,
                hourly_rate="95.00",
                transition_date=date(2025, 3, 1),
                direction="later",
                scheduler=self.administrator,
            ),
        )
        self.assert_rejected_without_mutation(
            initial.kind,
            lambda: append_stipend_rate(
                kind=initial.kind,
                hourly_rate="60.00",
                transition_date=date(2025, 2, 28),
                direction="earlier",
                scheduler=self.administrator,
            ),
        )

    def test_append_rejects_invalid_hourly_rates_in_both_directions_without_mutation(self):
        initial = self.set_initial()
        append_stipend_rate(
            kind=initial.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        for direction, transition_date in (
            ("earlier", date(2025, 1, 31)),
            ("later", date(2025, 4, 1)),
        ):
            with self.subTest(direction=direction):
                self.assert_rejected_without_mutation(
                    initial.kind,
                    lambda direction=direction, transition_date=transition_date: (
                        append_stipend_rate(
                            kind=initial.kind,
                            hourly_rate="0.00",
                            transition_date=transition_date,
                            direction=direction,
                            scheduler=self.administrator,
                        )
                    ),
                )

    def test_editing_a_bounded_transition_moves_its_adjacent_boundary_and_records_both(self):
        initial = self.set_initial()
        later = append_stipend_rate(
            kind=initial.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        correct_stipend_rate(
            rate=later,
            hourly_rate="91.25",
            effective_start=date(2025, 2, 15),
            effective_end=None,
            scheduler=self.administrator,
            note="Move transition",
        )

        initial.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(initial.effective_end, date(2025, 2, 14))
        self.assertEqual(
            (later.effective_start, later.hourly_rate), (date(2025, 2, 15), Decimal("91.25"))
        )
        changes = list(StipendRateChange.objects.order_by("pk"))
        self.assertEqual([change.action for change in changes[-2:]], ["corrected", "corrected"])
        self.assertTrue(all(change.note == "Move transition" for change in changes[-2:]))

    def test_edit_cannot_bound_an_outer_timeline_edge(self):
        rate = self.set_initial()

        self.assert_rejected_without_mutation(
            rate.kind,
            lambda: correct_stipend_rate(
                rate=rate,
                hourly_rate="75.00",
                effective_start=date(2025, 1, 1),
                effective_end=None,
                scheduler=self.administrator,
            ),
        )

    def test_bounded_correction_rejects_invalid_rates_and_boundaries_without_mutation(self):
        initial = self.set_initial()
        middle = append_stipend_rate(
            kind=initial.kind,
            hourly_rate="80.00",
            transition_date=date(2025, 2, 1),
            direction="later",
            scheduler=self.administrator,
        )
        append_stipend_rate(
            kind=initial.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        self.assert_rejected_without_mutation(
            initial.kind,
            lambda: correct_stipend_rate(
                rate=middle,
                hourly_rate="0.00",
                effective_start=date(2025, 2, 1),
                effective_end=date(2025, 2, 28),
                scheduler=self.administrator,
            ),
        )
        self.assert_rejected_without_mutation(
            initial.kind,
            lambda: correct_stipend_rate(
                rate=middle,
                hourly_rate="80.00",
                effective_start=date(2025, 2, 15),
                effective_end=date(2025, 2, 10),
                scheduler=self.administrator,
            ),
        )

    def test_only_scheduling_administrators_can_mutate(self):
        with self.assertRaises(PermissionDenied):
            set_initial_stipend_rate(
                kind=StipendRate.Kind.LATE_DOC, hourly_rate="75.00", scheduler=self.scheduler
            )

    def test_removing_the_first_rate_extends_the_following_rate_backward(self):
        first = self.set_initial()
        following = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        remove_stipend_rate(rate=first, scheduler=self.administrator, note="Incorrect rate")

        first.refresh_from_db()
        following.refresh_from_db()
        self.assertIsNotNone(first.retired_at)
        self.assertEqual((following.effective_start, following.effective_end), (None, None))
        self.assertEqual(stipend_rate_on(kind=first.kind, on_date=date(2025, 1, 1)), following)
        removed = StipendRateChange.objects.get(rate=first, action=StipendRateChange.Action.RETIRED)
        self.assertEqual(removed.previous_value["effective_end"], "2025-02-28")
        self.assertIsNone(removed.recorded_value)
        adjusted = StipendRateChange.objects.get(
            rate=following, action=StipendRateChange.Action.CORRECTED
        )
        self.assertEqual(adjusted.previous_value["effective_start"], "2025-03-01")
        self.assertEqual(adjusted.recorded_value["effective_start"], None)
        self.assertEqual(removed.note, "Incorrect rate")
        self.assertEqual(removed.changed_by, self.administrator)

    def test_removing_the_last_rate_extends_the_previous_rate_forward(self):
        first = self.set_initial()
        last = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        remove_stipend_rate(rate=last, scheduler=self.administrator)

        first.refresh_from_db()
        last.refresh_from_db()
        self.assertEqual((first.effective_start, first.effective_end), (None, None))
        self.assertIsNotNone(last.retired_at)
        self.assertEqual(list(stipend_rates(first.kind)), [first])

    def test_removing_an_internal_rate_requires_and_applies_the_selected_absorber(self):
        first = self.set_initial()
        middle = append_stipend_rate(
            kind=first.kind,
            hourly_rate="80.00",
            transition_date=date(2025, 2, 1),
            direction="later",
            scheduler=self.administrator,
        )
        last = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        self.assert_rejected_without_mutation(
            first.kind,
            lambda: remove_stipend_rate(rate=middle, scheduler=self.administrator),
        )
        remove_stipend_rate(rate=middle, absorber=first.pk, scheduler=self.administrator)
        first.refresh_from_db()
        self.assertEqual(first.effective_end, date(2025, 2, 28))
        self.assertEqual(last.effective_start, date(2025, 3, 1))

    def test_removing_an_internal_rate_can_extend_the_following_rate_backward(self):
        first = self.set_initial()
        middle = append_stipend_rate(
            kind=first.kind,
            hourly_rate="80.00",
            transition_date=date(2025, 2, 1),
            direction="later",
            scheduler=self.administrator,
        )
        last = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        remove_stipend_rate(rate=middle, absorber=last.pk, scheduler=self.administrator)

        first.refresh_from_db()
        last.refresh_from_db()
        self.assertEqual(first.effective_end, date(2025, 1, 31))
        self.assertEqual(last.effective_start, date(2025, 2, 1))
        self.assertEqual(stipend_rate_on(kind=first.kind, on_date=date(2025, 2, 15)), last)

    def test_removal_rejects_a_sole_rate_invalid_absorbers_and_stale_targets(self):
        only = self.set_initial()
        self.assert_rejected_without_mutation(
            only.kind,
            lambda: remove_stipend_rate(rate=only, scheduler=self.administrator),
        )
        following = append_stipend_rate(
            kind=only.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )
        self.assert_rejected_without_mutation(
            only.kind,
            lambda: remove_stipend_rate(
                rate=only, absorber=following.pk, scheduler=self.administrator
            ),
        )
        remove_stipend_rate(rate=following, scheduler=self.administrator)
        self.assert_rejected_without_mutation(
            only.kind,
            lambda: remove_stipend_rate(rate=following, scheduler=self.administrator),
        )

    def test_removal_rejects_an_absorber_that_is_no_longer_a_neighbor(self):
        first = self.set_initial()
        middle = append_stipend_rate(
            kind=first.kind,
            hourly_rate="80.00",
            transition_date=date(2025, 2, 1),
            direction="later",
            scheduler=self.administrator,
        )
        following = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )
        last = append_stipend_rate(
            kind=first.kind,
            hourly_rate="95.00",
            transition_date=date(2025, 4, 1),
            direction="later",
            scheduler=self.administrator,
        )
        remove_stipend_rate(rate=following, absorber=last.pk, scheduler=self.administrator)

        self.assert_rejected_without_mutation(
            first.kind,
            lambda: remove_stipend_rate(
                rate=middle, absorber=following.pk, scheduler=self.administrator
            ),
        )

    def test_removal_rolls_back_the_retirement_and_history_when_a_record_fails(self):
        first = self.set_initial()
        following = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )
        state = self.timeline_state(first.kind)

        with (
            patch("schedules.services.stipend_rates._record_change", side_effect=RuntimeError),
            self.assertRaises(RuntimeError),
        ):
            remove_stipend_rate(rate=following, scheduler=self.administrator)

        self.assertEqual(self.timeline_state(first.kind), state)

    def test_only_scheduling_administrators_can_remove_rates(self):
        first = self.set_initial()
        following = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )

        with self.assertRaises(PermissionDenied):
            remove_stipend_rate(rate=following, scheduler=self.scheduler)


class StipendRateConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(
            username="administrator", password="test-password"
        )
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.LATE_DOC, hourly_rate="75.00", scheduler=self.administrator
        )

    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_later_additions_leave_one_gapless_timeline(self):
        barrier = Barrier(2)
        errors = []
        error_lock = Lock()

        def add(on_date, rate):
            close_old_connections()
            try:
                barrier.wait(timeout=1)
                append_stipend_rate(
                    kind=StipendRate.Kind.LATE_DOC,
                    hourly_rate=rate,
                    transition_date=on_date,
                    direction="later",
                    scheduler=self.administrator,
                )
            except Exception as error:
                with error_lock:
                    errors.append(error)
            finally:
                close_old_connections()

        threads = [
            Thread(target=add, args=(date(2025, 2, 1), "80.00")),
            Thread(target=add, args=(date(2025, 3, 1), "85.00")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertLessEqual(len(errors), 1, errors)
        self.assertTrue(all(isinstance(error, ValidationError) for error in errors), errors)
        rates = list(stipend_rates(StipendRate.Kind.LATE_DOC))
        self.assertEqual(rates[0].effective_start, None)
        self.assertEqual(rates[-1].effective_end, None)
        self.assertTrue(
            all(
                following.effective_start == previous.effective_end + timedelta(days=1)
                for previous, following in pairwise(rates)
            )
        )

    @skipUnlessDBFeature("has_select_for_update")
    def test_competing_removals_reject_the_stale_target_without_partial_history(self):
        first = StipendRate.objects.get(kind=StipendRate.Kind.LATE_DOC)
        middle = append_stipend_rate(
            kind=first.kind,
            hourly_rate="80.00",
            transition_date=date(2025, 2, 1),
            direction="later",
            scheduler=self.administrator,
        )
        following = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )
        history_count = StipendRateChange.objects.count()
        barrier = Barrier(2)
        errors = []
        error_lock = Lock()

        def remove(absorber):
            close_old_connections()
            try:
                # Both requests begin with a still-active copy of the same rate.
                stale_middle = StipendRate.objects.get(pk=middle.pk)
                barrier.wait(timeout=1)
                remove_stipend_rate(
                    rate=stale_middle, absorber=absorber.pk, scheduler=self.administrator
                )
            except Exception as error:
                with error_lock:
                    errors.append(error)
            finally:
                close_old_connections()

        threads = [Thread(target=remove, args=(first,)), Thread(target=remove, args=(following,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], ValidationError)

        middle.refresh_from_db()
        first.refresh_from_db()
        following.refresh_from_db()
        self.assertIsNotNone(middle.retired_at)
        self.assertTrue(
            (first.effective_end, following.effective_start)
            in (
                (date(2025, 2, 28), date(2025, 3, 1)),
                (date(2025, 1, 31), date(2025, 2, 1)),
            )
        )

        changes = list(StipendRateChange.objects.filter(pk__gt=history_count).order_by("pk"))
        self.assertEqual(len(changes), 2)
        self.assertEqual(
            {(change.rate_id, change.action) for change in changes},
            {
                (middle.pk, StipendRateChange.Action.RETIRED),
                (first.pk, StipendRateChange.Action.CORRECTED),
            }
            if first.effective_end == date(2025, 2, 28)
            else {
                (middle.pk, StipendRateChange.Action.RETIRED),
                (following.pk, StipendRateChange.Action.CORRECTED),
            },
        )


class StipendRatePresentationTests(TestCase):
    def setUp(self):
        self.administrator = get_user_model().objects.create_user(
            username="administrator", password="test-password"
        )
        self.administrator.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="schedules", codename="manage_scheduling_administration"
            )
        )
        self.scheduler = get_user_model().objects.create_user(
            username="scheduler", password="test-password"
        )

    def test_empty_sections_offer_only_source_scoped_initial_actions_to_administrators(self):
        self.client.force_login(self.administrator)

        response = self.client.get(reverse("stipend-rate-workspace"))

        self.assertContains(response, "unbounded at both outer ends")
        links = _links_in_section(response.content.decode())
        self.assertEqual(
            links["Community Preceptor"]["Set initial rate"],
            reverse("stipend-rate-initial", args=(StipendRate.Kind.COMMUNITY_PRECEPTOR,)),
        )
        self.assertEqual(
            links["Late Doc"]["Set initial rate"],
            reverse("stipend-rate-initial", args=(StipendRate.Kind.LATE_DOC,)),
        )
        self.assertNotContains(response, "Add stipend rate")
        self.assertContains(response, reverse("stipend-rate-history"))

    def test_configured_section_offers_append_actions_and_edit(self):
        rate = set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        self.client.force_login(self.administrator)

        response = self.client.get(reverse("stipend-rate-workspace"))

        table = _RenderedDataTable(response.content.decode(), "community-preceptor-rates")
        rate_row = _assert_table_row_fields(
            self,
            table,
            {
                "Hourly rate": "$100.00",
                "Effective start": "Unbounded",
                "Effective end": "Ongoing",
            },
            identity=("Hourly rate",),
        )
        action_links = table.cell_for_header(rate_row, "Actions").descendants_named("a")
        edit_links = [link for link in action_links if link.text().strip().startswith("Edit")]
        self.assertEqual(len(edit_links), 1)
        self.assertEqual(
            edit_links[0].attributes["href"], reverse("stipend-rate-correct", args=(rate.pk,))
        )
        self.assertIn("at least one rate must remain", edit_links[0].text().casefold())
        self.assertFalse(any(link.text().strip() == "Remove" for link in action_links))
        self.assertEqual(
            _links_in_section(response.content.decode())["Community Preceptor"]["Add earlier rate"],
            reverse("stipend-rate-append", args=(rate.kind, "earlier")),
        )
        self.assertEqual(
            _links_in_section(response.content.decode())["Community Preceptor"]["Add later rate"],
            reverse("stipend-rate-append", args=(rate.kind, "later")),
        )

    def test_workspace_keeps_rate_tables_full_width_and_dates_localized(self):
        initial = set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        append_stipend_rate(
            kind=initial.kind,
            hourly_rate="110.00",
            transition_date=date(2025, 2, 3),
            direction="later",
            scheduler=self.administrator,
        )
        set_initial_stipend_rate(
            kind=StipendRate.Kind.LATE_DOC,
            hourly_rate="75.00",
            scheduler=self.administrator,
        )
        self.client.force_login(self.administrator)

        response = self.client.get(reverse("stipend-rate-workspace"))

        community_table = _RenderedDataTable(response.content.decode(), "community-preceptor-rates")
        late_doc_table = _RenderedDataTable(response.content.decode(), "late-doc-rates")
        self.assertNotIn("data-table--compact", community_table.table.attributes["class"])
        self.assertNotIn("data-table--compact", late_doc_table.table.attributes["class"])
        for expected in (
            {
                "Hourly rate": "$100.00",
                "Effective start": "Unbounded",
                "Effective end": "Feb. 2, 2025",
            },
            {
                "Hourly rate": "$110.00",
                "Effective start": "Feb. 3, 2025",
                "Effective end": "Ongoing",
            },
        ):
            _assert_table_row_fields(self, community_table, expected, identity=("Hourly rate",))

    def test_initial_and_append_forms_scope_kind_and_progressively_collect_only_needed_values(self):
        self.client.force_login(self.administrator)
        initial_url = reverse("stipend-rate-initial", args=(StipendRate.Kind.LATE_DOC,))

        initial = self.client.get(initial_url)
        created = self.client.post(initial_url, {"hourly_rate": "80.00", "note": "Initial"})
        append_url = reverse("stipend-rate-append", args=(StipendRate.Kind.LATE_DOC, "later"))
        append = self.client.get(append_url)
        added = self.client.post(
            append_url,
            {"hourly_rate": "90.00", "transition_date": "2025-02-01", "note": "Later"},
        )

        self.assertNotContains(initial, "Stipend type")
        self.assertNotContains(initial, "Transition date")
        self.assertContains(append, "Transition date")
        self.assertRedirects(created, reverse("stipend-rate-workspace"))
        self.assertRedirects(added, reverse("stipend-rate-workspace"))
        workspace = self.client.get(reverse("stipend-rate-workspace"))
        late_doc_table = _RenderedDataTable(workspace.content.decode(), "late-doc-rates")
        for expected in (
            {
                "Hourly rate": "$80.00",
                "Effective start": "Unbounded",
                "Effective end": "Jan. 31, 2025",
            },
            {
                "Hourly rate": "$90.00",
                "Effective start": "Feb. 1, 2025",
                "Effective end": "Ongoing",
            },
        ):
            _assert_table_row_fields(self, late_doc_table, expected, identity=("Hourly rate",))
        history = _RenderedDataTable(
            self.client.get(reverse("stipend-rate-history")).content.decode(),
            "stipend-rate-history",
        )
        _assert_history_row(
            self,
            history,
            {
                "Stipend type": "Late Doc",
                "Change": "Created",
                "Prior value": "null",
                "Recorded value": {"hourly_rate": "80.00"},
            },
        )
        _assert_history_row(
            self,
            history,
            {
                "Stipend type": "Late Doc",
                "Change": "Corrected",
                "Prior value": {"hourly_rate": "80.00"},
                "Recorded value": {
                    "effective_end": "2025-01-31",
                    "hourly_rate": "80.00",
                },
            },
        )

    def test_edit_form_binds_unbounded_boundaries_to_their_named_fields_and_ignores_forged_values(
        self,
    ):
        rate = set_initial_stipend_rate(
            kind=StipendRate.Kind.LATE_DOC, hourly_rate="80.00", scheduler=self.administrator
        )
        self.client.force_login(self.administrator)
        url = reverse("stipend-rate-correct", args=(rate.pk,))

        response = self.client.get(url)
        saved = self.client.post(
            url,
            {
                "hourly_rate": "81.00",
                "effective_start": "2025-01-01",
                "effective_end": "2025-12-31",
            },
        )

        fields = _form_fields(response.content.decode())
        self.assertIn("disabled", fields["effective_start"].attributes)
        self.assertIn("disabled", fields["effective_end"].attributes)
        self.assertIn("unbounded into the past", fields["effective_start"].parent.text())
        self.assertIn("unbounded into the future", fields["effective_end"].parent.text())
        self.assertRedirects(saved, reverse("stipend-rate-workspace"))
        rate.refresh_from_db()
        self.assertEqual(rate.hourly_rate, Decimal("81.00"))
        self.assertIsNone(rate.effective_start)
        self.assertIsNone(rate.effective_end)

    def test_authenticated_schedulers_can_inspect_but_not_mutate(self):
        rate = set_initial_stipend_rate(
            kind=StipendRate.Kind.COMMUNITY_PRECEPTOR,
            hourly_rate="100.00",
            scheduler=self.administrator,
        )
        self.client.force_login(self.scheduler)

        response = self.client.get(reverse("stipend-rate-workspace"))

        self.assertContains(response, "Community Preceptor")
        self.assertContains(response, "$100.00")
        self.assertNotContains(response, "Set initial rate")
        self.assertNotContains(response, "Add earlier rate")
        self.assertNotContains(response, "Add later rate")
        self.assertNotContains(response, "Edit")
        self.assertNotContains(response, "Remove")

        for url, data in (
            (
                reverse("stipend-rate-append", args=(rate.kind, "earlier")),
                {"hourly_rate": "90.00", "transition_date": "2025-01-31"},
            ),
            (
                reverse("stipend-rate-append", args=(rate.kind, "later")),
                {"hourly_rate": "110.00", "transition_date": "2025-02-01"},
            ),
            (
                reverse("stipend-rate-correct", args=(rate.pk,)),
                {"hourly_rate": "110.00", "effective_start": "", "effective_end": ""},
            ),
            (reverse("stipend-rate-remove", args=(rate.pk,)), {}),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, data).status_code, 403)

    def test_removal_confirmation_requires_a_choice_for_an_internal_rate(self):
        first = set_initial_stipend_rate(
            kind=StipendRate.Kind.LATE_DOC, hourly_rate="75.00", scheduler=self.administrator
        )
        middle = append_stipend_rate(
            kind=first.kind,
            hourly_rate="80.00",
            transition_date=date(2025, 2, 1),
            direction="later",
            scheduler=self.administrator,
        )
        following = append_stipend_rate(
            kind=first.kind,
            hourly_rate="90.00",
            transition_date=date(2025, 3, 1),
            direction="later",
            scheduler=self.administrator,
        )
        self.client.force_login(self.administrator)
        url = reverse("stipend-rate-remove", args=(middle.pk,))

        response = self.client.get(url)
        before_rejected_post = _rate_timeline_history_state()
        rejected = self.client.post(url, {"note": "No choice"})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(_rate_timeline_history_state(), before_rejected_post)

        parser = _RenderedTableParser()
        parser.feed(response.content.decode())
        choice_group = next(
            fieldset
            for fieldset in parser.root.descendants_named("fieldset")
            if "form-choice-group" in fieldset.attributes.get("class", "").split()
        )
        self.assertEqual(
            choice_group.children_named("legend")[0].text().strip(),
            "Rate that will absorb this period",
        )
        choices = choice_group.descendants_named("input")
        self.assertEqual(
            [(choice.attributes["value"], choice.parent.text().strip()) for choice in choices],
            [(str(first.pk), "Previous rate ($75.00)"), (str(following.pk), "Next rate ($90.00)")],
        )
        middle.refresh_from_db()
        first.refresh_from_db()
        following.refresh_from_db()
        self.assertIsNone(middle.retired_at)
        self.assertEqual(
            (first.effective_end, following.effective_start), (date(2025, 1, 31), date(2025, 3, 1))
        )
        removed = self.client.post(url, {"absorber": following.pk, "note": "Corrected"})
        self.assertRedirects(removed, reverse("stipend-rate-workspace"))
        middle.refresh_from_db()
        following.refresh_from_db()
        self.assertIsNotNone(middle.retired_at)
        self.assertEqual(following.effective_start, date(2025, 2, 1))


def _table_fields(table, row, headers):
    return {header: table.cell_for_header(row, header).text().strip() for header in headers}


def _assert_table_row_fields(test_case, table, expected, *, identity):
    rows = [
        row
        for row in table.body_rows()
        if _table_fields(table, row, identity) == {header: expected[header] for header in identity}
    ]
    test_case.assertEqual(len(rows), 1, f"Expected one row identified by {identity!r}")
    test_case.assertEqual(_table_fields(table, rows[0], expected), expected)
    return rows[0]


def _assert_history_row(test_case, table, expected):
    def matches(row):
        for header, expected_value in expected.items():
            actual = table.cell_for_header(row, header).text().strip()
            if isinstance(expected_value, dict):
                actual_value = json.loads(actual)
                if any(actual_value.get(key) != value for key, value in expected_value.items()):
                    return False
            elif actual != expected_value:
                return False
        return True

    rows = [row for row in table.body_rows() if matches(row)]
    test_case.assertEqual(len(rows), 1, f"Expected one history row with {expected!r}")


def _form_fields(content):
    parser = _RenderedTableParser()
    parser.feed(content)
    return {
        field.attributes["name"]: field
        for field in parser.root.descendants_named("input")
        if field.attributes.get("name")
    }


def _rate_timeline_history_state():
    return {
        "rates": list(
            StipendRate.objects.order_by("pk").values_list(
                "pk", "kind", "hourly_rate", "effective_start", "effective_end", "retired_at"
            )
        ),
        "history": list(
            StipendRateChange.objects.order_by("pk").values_list(
                "pk", "rate_id", "action", "previous_value", "recorded_value", "note"
            )
        ),
    }


def _links_in_section(content):
    parser = _RenderedTableParser()
    parser.feed(content)
    sections = {}
    for section in parser.root.descendants_named("section"):
        headings = section.children_named("header")
        if not headings:
            continue
        heading = next(iter(headings[0].children_named("h2")), None)
        if heading is None or heading.text().strip() not in {"Community Preceptor", "Late Doc"}:
            continue
        sections[heading.text().strip()] = {
            link.text().strip(): link.attributes.get("href")
            for link in section.descendants_named("a")
        }
    return sections
