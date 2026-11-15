from backend.scrub import mask


def test_masks_phone_email_and_known_name():
    text = "张三的手机是 13800138000，邮箱 zhangsan@example.com"
    masked, mapping = mask(text, known_names=["张三"])

    for raw in ("张三", "13800138000", "zhangsan@example.com"):
        assert raw not in masked
    assert "[人物1]" in masked
    assert "[手机1]" in masked
    assert "[邮箱1]" in masked
    assert mapping["[人物1]"] == "张三"


def test_same_entity_gets_same_placeholder():
    masked, _ = mask("张三 和 张三", known_names=["张三"])

    assert masked.count("[人物1]") == 2
    assert "[人物2]" not in masked


def test_existing_mapping_is_reused():
    masked, new_mapping = mask(
        "张三", known_names=["张三"], existing={"[人物3]": "张三"}
    )

    assert "[人物3]" in masked
    assert new_mapping == {}


def test_bank_card_is_dropped():
    masked, _ = mask("卡号 6222021234567890123")

    assert "6222021234567890123" not in masked


def test_id_card_is_masked_before_bank_card_pattern():
    masked, mapping = mask("身份证 110101199003071234")

    assert "110101199003071234" not in masked
    assert "[身份证1]" in masked
    assert mapping["[身份证1]"] == "110101199003071234"
