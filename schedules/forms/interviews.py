from django import forms

from ..services.interviews import interview_assignment_picker_candidates_on


class InterviewPhysicianChoiceField(forms.ChoiceField):
    def __init__(self, candidates, **kwargs):
        self._candidates = {str(candidate.physician.pk): candidate for candidate in candidates}
        grouped_choices = [("", "No Faculty assignment")]
        for tier in ("program_director", "available_faculty", "backup_only", "override_required"):
            tier_candidates = [candidate for candidate in candidates if candidate.tier == tier]
            if tier_candidates:
                grouped_choices.append(
                    (
                        tier_candidates[0].tier_label,
                        [
                            (
                                str(candidate.physician.pk),
                                " | ".join(
                                    part
                                    for part in (
                                        candidate.physician.display_name,
                                        "Program Director preference"
                                        if candidate.tier == "program_director"
                                        else "Backup-only preference"
                                        if candidate.tier == "backup_only"
                                        else candidate.availability_reason
                                        if candidate.requires_override
                                        else None,
                                    )
                                    if part
                                ),
                            )
                            for candidate in tier_candidates
                        ],
                    )
                )
        super().__init__(choices=grouped_choices, **kwargs)

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return None
        return self._candidates[str(value)].physician

    def requires_override(self, physician):
        return self._candidates[str(physician.pk)].requires_override


class InterviewShiftForm(forms.Form):
    date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "field-width--compact"}),
        label="Interview date",
    )
    note = forms.CharField(
        required=False,
        label="Optional note",
        widget=forms.Textarea(attrs={"class": "field-width--wide"}),
    )

    def __init__(self, *args, on_date=None, shift=None, **kwargs):
        super().__init__(*args, **kwargs)
        candidates = interview_assignment_picker_candidates_on(on_date) if on_date else ()
        self.fields["physician"] = InterviewPhysicianChoiceField(
            candidates,
            required=False,
            label="Faculty assignment",
            widget=forms.Select(attrs={"class": "field-width--full"}),
            help_text="Program Directors are preferred. Backup and override choices record findings.",
        )
        self.order_fields(("date", "physician", "note"))
        if shift is not None and str(shift.physician_id) in self.fields["physician"]._candidates:
            self.initial.setdefault("physician", shift.physician_id)
