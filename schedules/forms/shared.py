from django import forms


def optional_note_field():
    return forms.CharField(
        required=False,
        label="History note (optional)",
        widget=forms.Textarea,
        help_text="Recorded with this change in history.",
    )


def ordinal_label(value):
    return ("First", "Second", "Third", "Fourth", "Fifth")[value - 1]
