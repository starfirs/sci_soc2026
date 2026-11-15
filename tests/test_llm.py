import pytest

from backend.llm import parse_json


def test_parses_plain_json():
    assert parse_json('{"decisions": []}') == {"decisions": []}


def test_parses_fenced_json():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extracts_json_from_surrounding_prose():
    text = '好的，结果如下：\n{"candidates": [{"title": "x"}]}\n希望有帮助。'

    assert parse_json(text)["candidates"][0]["title"] == "x"


def test_handles_nested_structures_and_escaped_quotes():
    text = '{"a": [{"b": "含 \\" 引号"}]}'

    assert parse_json(text)["a"][0]["b"] == '含 " 引号'


def test_raises_when_no_json_present():
    with pytest.raises(ValueError):
        parse_json("没有 JSON")
