"""Dependency-free logging filters loaded before Django initializes its apps."""

import logging
import re


class SecretRedactionFilter(logging.Filter):
    """Keep authentication material out of the standard application log stream."""

    sensitive_value = re.compile(
        r"(?i)(password|token|session(?:id)?|secret|authorization|cookie)(=|:)([^\s,&]+)"
    )
    invitation_path = re.compile(r"(?<=/invitation/)[^/?\s]+")
    reset_path = re.compile(r"(?<=/password/reset/)[^/?\s]+/[^/?\s]+")

    @classmethod
    def redact(cls, value):
        text = str(value)
        text = cls.sensitive_value.sub(r"\1\2[redacted]", text)
        text = cls.invitation_path.sub("[redacted]", text)
        return cls.reset_path.sub("[redacted]", text)

    def filter(self, record):
        request = getattr(record, "request", None)
        record.request_id = getattr(request, "request_id", "unavailable")
        record.msg = self.redact(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: self.redact(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(self.redact(value) for value in record.args)
        # Exception messages and local variables can include credentials or signed tokens.
        record.exc_info = None
        record.exc_text = None
        return True
