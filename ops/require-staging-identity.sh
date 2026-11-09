# Sourced by staging bootstrap/update entry points; accepts only nonsecret identity.
# Capture separately from the protected environment parser's scratch variables.
require_staging_identity() {
    staging_host=${PRECEPTOR_STAGING_ALLOWED_HOST:-}
    staging_origin=${PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN:-}
    case "$staging_host" in
        ''|*[!abcdefghijklmnopqrstuvwxyz0123456789.-]*|.*|*.) return 1 ;;
    esac
    [ "${#staging_host}" -le 253 ] || return 1
    remaining_labels=$staging_host
    while :; do
        label=${remaining_labels%%.*}
        case "$label" in ''|-*|*-) return 1 ;; esac
        [ "${#label}" -le 63 ] || return 1
        [ "$remaining_labels" != "$label" ] || break
        remaining_labels=${remaining_labels#*.}
    done
    [ "$staging_origin" = "https://$staging_host:10000" ]
}

require_staging_identity || {
    printf '%s\n' 'Staging identity requires PRECEPTOR_STAGING_ALLOWED_HOST (one exact lowercase ASCII DNS hostname) and PRECEPTOR_STAGING_CSRF_TRUSTED_ORIGIN (exactly https://<host>:10000).' >&2
    exit 2
}
