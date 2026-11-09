# Sourced only by production bootstrap/update; no protected configuration is read.
production_identity_pair_valid() (
    host=$1
    origin=$2
    case "$host" in
        ''|*[!abcdefghijklmnopqrstuvwxyz0123456789.-]*|.*|*.) return 1 ;;
    esac
    [ "${#host}" -le 253 ] || return 1
    remaining_labels=$host
    while :; do
        label=${remaining_labels%%.*}
        case "$label" in ''|-*|*-) return 1 ;; esac
        [ "${#label}" -le 63 ] || return 1
        [ "$remaining_labels" != "$label" ] || break
        remaining_labels=${remaining_labels#*.}
    done
    [ "$origin" = "https://$host" ]
)

primary_host=${PRECEPTOR_PRODUCTION_ALLOWED_HOST:-}
primary_origin=${PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN:-}
secondary_host=${PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST:-}
secondary_origin=${PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN:-}
production_identity_pair_valid "$primary_host" "$primary_origin" || {
    printf '%s\n' 'Production primary identity requires PRECEPTOR_PRODUCTION_ALLOWED_HOST (one exact lowercase ASCII DNS hostname) and PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN (exactly https://<host>).' >&2
    exit 2
}
production_identity_pair_valid "$secondary_host" "$secondary_origin" || {
    printf '%s\n' 'Production secondary identity requires PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST (one exact lowercase ASCII DNS hostname) and PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN (exactly https://<host>).' >&2
    exit 2
}
[ "$primary_host" != "$secondary_host" ] || {
    printf '%s\n' 'Production primary and secondary hostnames must be distinct.' >&2
    exit 2
}
