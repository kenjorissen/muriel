"""Nonsecret invalid input cases shared by the staging shell entry-point tests."""

STAGING_HOST = "staging.example.test"
STAGING_ORIGIN = "https://staging.example.test:10000"
INVALID_STAGING_PAIRS = (
    (None, STAGING_ORIGIN),
    (STAGING_HOST, None),
    ("", STAGING_ORIGIN),
    (STAGING_HOST, ""),
    *(
        (host, f"https://{host}:10000")
        for host in (
            " stage.test",
            "stage.test ",
            "stage.test\n",
            "stage\ttest",
            "stage\rtest",
            "Stage.test",
            "stâge.test",
            "*.test",
            ".stage.test",
            "stage.test.",
            "stage..test",
            "-stage.test",
            "stage-.test",
            "stage_test",
            "stage.test:10000",
            "user@stage.test",
            "stage.test/path",
            "stage;id",
            "$(id)",
            "stage'test",
            "stage\\test",
            "stage\x01test",
            "a" * 64 + ".test",
            ("a" * 63 + ".") * 3 + "a" * 62,
        )
    ),
    *(
        (STAGING_HOST, origin)
        for origin in (
            "https://another.test:10000",
            "http://staging.example.test:10000",
            "https://staging.example.test",
            "https://staging.example.test:443",
            "https://staging.example.test:10000/",
            "https://staging.example.test:10000/path",
            "https://staging.example.test:10000?query",
            "https://staging.example.test:10000#fragment",
            "https://user@staging.example.test:10000",
            " https://staging.example.test:10000",
            "https://staging.example.test:10000\n",
            "https://staging.example.test:10000;id",
        )
    ),
)
