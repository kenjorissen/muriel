# Password recovery and delivery

Muriel supports username or configured-email sign-in, password recovery at
`/password/reset/`, and signed-in password changes at `/password/change/`.
There is no public account creation. A person without a configured email or a
usable password needs a superuser to set a password through the private Django
Admin route or an interactive Django command. On the production host, use the
checked `/srv/muriel/create-superuser` helper to create a recovery administrator;
it does not change an existing password.

## Local recovery walkthrough

1. Start the application with `./scripts/start-dev`.
2. Open `/sign-in/` and select **Forgot password?**.
3. Enter the configured email address for an active account with a usable
   password.
4. Read the reset URL printed by Django's console email backend and open it in
   the browser.
5. Choose a new password, then sign in with either the account username or its
   configured email address.

The public completion page is intentionally identical whether the submitted
email matched an eligible account. Do not infer account existence from it.

## Invitations

Scheduling Administrators create and resend scheduler invitations from
**Accounts**. The default
`PRECEPTOR_SCHEDULER_INVITATION_DELIVERY_MODE=manual` never sends mail: Muriel
shows the administrator a copyable single-use setup link to transfer through an
approved channel. A pending invitation remains inactive until its recipient sets
a password. Resending supersedes the prior link; revoking it prevents use.

Set `PRECEPTOR_SCHEDULER_INVITATION_DELIVERY_MODE=smtp` only after SMTP is
configured below. Muriel sends the invitation synchronously during create or
resend. It has no outbound mail daemon, queue, retry worker, or mailbox. If SMTP
delivery fails, the pending inactive account and invitation remain available in
**Accounts** for a resend after mail is repaired; Muriel does not reveal the
setup link in SMTP mode.

## SMTP deployment

Keep mail values in `/etc/muriel/environment`, not commands, logs, or source
control. To use SMTP, set:

```text
PRECEPTOR_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
PRECEPTOR_DEFAULT_FROM_EMAIL=mailer@example.net
PRECEPTOR_EMAIL_HOST=smtp.example.net
PRECEPTOR_EMAIL_PORT=587
PRECEPTOR_EMAIL_HOST_USER=mailer-user
PRECEPTOR_EMAIL_HOST_PASSWORD=stored-secret
PRECEPTOR_EMAIL_USE_TLS=true
PRECEPTOR_EMAIL_USE_SSL=false
PRECEPTOR_EMAIL_TIMEOUT=10
PRECEPTOR_SCHEDULER_INVITATION_DELIVERY_MODE=smtp
```

`PRECEPTOR_EMAIL_PORT` accepts 1 through 65535 and
`PRECEPTOR_EMAIL_TIMEOUT` accepts 1 through 60 seconds. TLS defaults to true,
SSL defaults to false, and the two transport settings cannot both be true. The
default email backend is Django's console backend, and the default sender is
`muriel@localhost`; neither delivers production mail. Password-reset messages
use the configured Django email backend as well.

The production edge fixes the public hostname and HTTPS scheme. Set
`PRECEPTOR_PRODUCTION_ALLOWED_HOST` to that one hostname and
`PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN` to its exact `https://` origin; do
not use the development-only `PRECEPTOR_ALLOWED_HOSTS` or
`PRECEPTOR_TRUST_PROXY` settings in this deployment. Send a reset and an
invitation to controlled accounts and verify the sender, public HTTPS links,
and completion flows before opening access to users.

Tests use Django's in-memory email backend, so they never deliver network mail.
