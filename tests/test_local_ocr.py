import pytest

from backend.local_ocr import (
    LocalOCREngine,
    LocalOCRError,
    parse_ocr_response,
    resolve_engine_dir,
)


def _touch_engine(directory, engine):
    directory.mkdir(parents=True, exist_ok=True)
    name = (
        "PaddleOCR-json.exe"
        if engine == "paddle"
        else "RapidOCR-json.exe"
    )
    (directory / name).touch()


def test_resolve_auto_prefers_paddle(tmp_path):
    _touch_engine(tmp_path, "paddle")
    _touch_engine(tmp_path, "rapid")

    engine, directory = resolve_engine_dir("auto", tmp_path)

    assert engine == "paddle"
    assert directory == tmp_path


def test_resolve_requested_engine(tmp_path):
    _touch_engine(tmp_path, "rapid")

    engine, directory = resolve_engine_dir("rapid", tmp_path)

    assert engine == "rapid"
    assert directory == tmp_path


def test_paddle_command_enables_mkldnn(tmp_path):
    _touch_engine(tmp_path, "paddle")
    engine = LocalOCREngine(
        "paddle", engine_dir=tmp_path, mkldnn=True, cpu_threads=4
    )

    command = engine._command()

    assert "--enable_mkldnn=true" in command
    assert "--cpu_threads=4" in command
    assert "--config_path=models/config_chinese.txt" in command


def test_paddle_command_can_disable_mkldnn(tmp_path):
    _touch_engine(tmp_path, "paddle")
    engine = LocalOCREngine(
        "paddle", engine_dir=tmp_path, mkldnn=False
    )

    assert "--enable_mkldnn=false" in engine._command()


def test_rapid_command_uses_chinese_models(tmp_path):
    _touch_engine(tmp_path, "rapid")
    engine = LocalOCREngine(
        "rapid", engine_dir=tmp_path, cpu_threads=2
    )

    command = engine._command()

    assert "--rec=rec_ch_PP-OCRv4_infer.onnx" in command
    assert "--keys=dict_chinese.txt" in command
    assert "--numThread=2" in command
    assert engine.info().mkldnn is None


def test_parse_text_and_dict_results():
    assert parse_ocr_response(
        {
            "code": 100,
            "data": [
                {"text": "第一行"},
                {"text": "第二行"},
            ],
        }
    ) == "第一行\n第二行"
    assert parse_ocr_response(
        {"code": 100, "data": "纯文本"}
    ) == "纯文本"
    assert parse_ocr_response({"code": 101, "data": ""}) == ""


def test_parse_error_result():
    with pytest.raises(LocalOCRError, match="引擎崩溃"):
        parse_ocr_response({"code": 902, "data": "引擎崩溃"})
