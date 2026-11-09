# Sourced by configured service helpers; validates their original arguments.
[ "$#" -eq 1 ] || {
    printf '%s\n' "Usage: $0 TARGET (must equal MURIEL_SSH_TARGET)" >&2
    exit 2
}

# Explicit ASCII sets avoid locale-dependent character ranges.
case ${MURIEL_SSH_TARGET:-} in
    ''|[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789]*|*[!abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-]*)
        printf '%s\n' 'MURIEL_SSH_TARGET is required and must be an SSH alias: an ASCII letter or digit followed by ASCII letters, digits, dots, underscores or hyphens.' >&2
        exit 2
        ;;
esac
[ "$1" = "$MURIEL_SSH_TARGET" ] || {
    printf '%s\n' 'TARGET must exactly match MURIEL_SSH_TARGET.' >&2
    exit 2
}
