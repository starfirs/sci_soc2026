from backend.screen_capture import (
    ExclusionRules,
    WindowInfo,
    drop_sensitive_lines,
    ingest_screen_text,
    is_excluded,
    normalize_ocr_text,
    text_similarity,
)


def test_normalize_ocr_text_removes_cjk_spaces():
    assert normalize_ocr_text("你 好 ， 世 界") == "你好，世界"
    assert normalize_ocr_text("SCREEN  OCR") == "SCREEN OCR"


def test_drop_sensitive_lines():
    text = (
        "正常内容\n"
        "password: hunter2\n"
        "sk-1234567890abcdef\n"
        "卡号 6222 0212 3456 7890\n"
        "另一行"
    )

    kept, dropped = drop_sensitive_lines(text)

    assert "正常内容" in kept
    assert "另一行" in kept
    assert "password" not in kept.lower()
    assert "sk-" not in kept
    assert "6222" not in kept
    assert dropped == 3


def test_is_excluded_by_title_or_process():
    rules = ExclusionRules(("incognito", "密码"), ("keepass",))
    excluded_title = WindowInfo(
        1, "New Incognito Tab", "chrome.exe", (0, 0, 100, 100)
    )
    excluded_process = WindowInfo(
        2, "KeePass", "KeePassXC.exe", (0, 0, 100, 100)
    )
    normal = WindowInfo(
        3, "Notes", "notepad.exe", (0, 0, 100, 100)
    )

    assert is_excluded(excluded_title, rules) is not None
    assert is_excluded(excluded_process, rules) is not None
    assert is_excluded(normal, rules) is None


def test_text_similarity_high_for_minor_change():
    assert text_similarity("今天学了洛必达法则", "今天学了洛必达法则。") > 0.8


def test_ingest_screen_text_writes_masked_segment(conn):
    result = ingest_screen_text(
        conn, "今天高数课学了洛必达法则，电话是 13800138000。"
    )

    assert result.action == "written"
    assert result.segment_id is not None
    row = conn.execute("SELECT * FROM segment").fetchone()
    assert row["source"] == "screen"
    assert row["processed"] == 0
    assert "13800138000" not in row["text_masked"]
    assert "[手机1]" in row["text_masked"]


def test_ingest_screen_text_skips_duplicate(conn):
    text = "今天高数课学了洛必达法则，它是求解极限的重要方法。"

    first = ingest_screen_text(conn, text)
    second = ingest_screen_text(conn, text)

    assert first.action == "written"
    assert second.action == "skipped_duplicate"
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM segment"
    ).fetchone()["c"]
    assert count == 1


def test_ingest_screen_text_skips_short_text(conn):
    result = ingest_screen_text(conn, "太短了")

    assert result.action == "skipped_short"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM segment"
    ).fetchone()["c"] == 0


def test_ingest_screen_text_dry_run_does_not_write(conn):
    result = ingest_screen_text(
        conn,
        "今天高数课学了洛必达法则，它是求解极限的重要方法。",
        dry_run=True,
    )

    assert result.action == "dry_run"
    assert result.masked_text
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM segment"
    ).fetchone()["c"] == 0


def test_ingest_screen_text_masks_known_person(conn):
    conn.execute(
        "INSERT INTO placeholder_map(placeholder, real_value, kind) "
        "VALUES ('[人物1]', '张三', 'person')"
    )
    conn.commit()

    result = ingest_screen_text(
        conn, "张三今天讲了洛必达法则，大家都听懂了。"
    )

    assert result.action == "written"
    assert "[人物1]" in result.masked_text
    assert "张三" not in result.masked_text


def test_ingest_screen_text_drops_sensitive_lines(conn):
    result = ingest_screen_text(
        conn,
        "今天高数课学了洛必达法则，它是求解极限的重要方法。\n"
        "password: hunter2",
    )

    assert result.action == "written"
    assert result.dropped_lines == 1
    assert "password" not in result.masked_text.lower()
