from backend.audio_capture import ingest_audio_text


def test_audio_text_is_normalized_masked_and_stored(conn):
    result = ingest_audio_text(
        conn,
        "今天 学了 洛必达法则 电话 13800138000",
        source="microphone",
        min_chars=5,
    )

    assert result.action == "written"
    assert result.segment_id is not None
    row = conn.execute("SELECT * FROM segment").fetchone()
    assert row["source"] == "microphone"
    assert row["processed"] == 0
    assert "今天学了" in row["text_masked"]
    assert "13800138000" not in row["text_masked"]
    assert "[手机1]" in row["text_masked"]


def test_audio_duplicate_across_mic_and_system_is_skipped(conn):
    text = "今天 学了 洛必达法则 它是 求解 极限 的 重要 方法"

    first = ingest_audio_text(
        conn, text, source="microphone", min_chars=5
    )
    second = ingest_audio_text(
        conn, text, source="system", min_chars=5
    )

    assert first.action == "written"
    assert second.action == "skipped_duplicate"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM segment"
    ).fetchone()["c"] == 1


def test_audio_short_text_is_skipped(conn):
    result = ingest_audio_text(
        conn, "嗯 好", source="microphone", min_chars=10
    )

    assert result.action == "skipped_short"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM segment"
    ).fetchone()["c"] == 0


def test_audio_dry_run_does_not_write(conn):
    result = ingest_audio_text(
        conn,
        "今天 学了 洛必达法则 它是 求解 极限 的 重要 方法",
        source="system",
        min_chars=5,
        dry_run=True,
    )

    assert result.action == "dry_run"
    assert result.masked_text
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM segment"
    ).fetchone()["c"] == 0


def test_audio_sensitive_line_is_dropped(conn):
    result = ingest_audio_text(
        conn,
        "今天 学了 洛必达法则 它是 求解 极限 的 重要 方法\n"
        "密码 是 hunter2",
        source="microphone",
        min_chars=5,
    )

    assert result.action == "written"
    assert result.dropped_lines == 1
    assert "password" not in result.masked_text.lower()
    assert "密码" not in result.masked_text
