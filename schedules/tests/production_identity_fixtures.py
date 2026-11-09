"""Nonsecret identities shared by production entry-point fixtures."""

PRIMARY_IDENTITY = {
    "PRECEPTOR_PRODUCTION_ALLOWED_HOST": "funnel.example.test",
    "PRECEPTOR_PRODUCTION_CSRF_TRUSTED_ORIGIN": "https://funnel.example.test",
}
SECONDARY_IDENTITY = {
    "PRECEPTOR_PRODUCTION_SECONDARY_ALLOWED_HOST": "edge.example.test",
    "PRECEPTOR_PRODUCTION_SECONDARY_CSRF_TRUSTED_ORIGIN": "https://edge.example.test",
}
PRODUCTION_IDENTITY = PRIMARY_IDENTITY | SECONDARY_IDENTITY

# Both pairs have the same syntax; exercise these at each public entry point.
INVALID_PRODUCTION_PAIRS = (
    (None, "https://edge.example.test"),
    ("edge.example.test", None),
    ("", "https://edge.example.test"),
    ("edge.example.test", ""),
    *(
        (host, f"https://{host}")
        for host in (
            " edge.test",
            "edge.test ",
            "edge.test\n",
            "edge\ttest",
            "edge\rtest",
            "Edge.test",
            "édge.test",
            "*.test",
            ".edge.test",
            "edge.test.",
            "edge..test",
            "-edge.test",
            "edge-.test",
            "edge_test",
            "edge.test:443",
            "user@edge.test",
            "edge.test/path",
            "edge.test?query",
            "edge.test#fragment",
            "edge;id",
            "$(id)",
            "edge'test",
            "edge\\test",
            "edge\x01test",
            "a" * 64 + ".test",
            ("a" * 63 + ".") * 3 + "a" * 62,
        )
    ),
    *(
        ("edge.example.test", origin)
        for origin in (
            "https://another.test",
            "http://edge.example.test",
            "https://edge.example.test:443",
            "https://edge.example.test/",
            "https://edge.example.test/path",
            "https://edge.example.test?query",
            "https://edge.example.test#fragment",
            "https://user@edge.example.test",
            " https://edge.example.test",
            "https://edge.example.test\n",
            "https://edge.example.test;id",
        )
    ),
)
