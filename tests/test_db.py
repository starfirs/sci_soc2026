def test_all_tables_created(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    assert {
        "segment",
        "daily_summary",
        "node",
        "edge",
        "review_log",
        "placeholder_map",
        "consolidation_report",
    } <= tables


def test_schema_version_recorded(conn):
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()

    assert row["value"] == "1"
