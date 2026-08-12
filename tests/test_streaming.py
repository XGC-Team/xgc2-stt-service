from __future__ import annotations

from xgc2_stt.streaming import LeadingSilenceGate, normalize_transcript, parse_boolean_query


def pcm(sample: int, count: int) -> bytes:
    return sample.to_bytes(2, "little", signed=True) * count


def test_leading_silence_gate_drops_silence_and_keeps_bounded_preroll() -> None:
    gate = LeadingSilenceGate(threshold=250, pre_roll_bytes=8)

    assert gate.push(pcm(0, 8)) == b""
    speech = pcm(1000, 4)
    assert gate.push(speech) == pcm(0, 4) + speech
    assert gate.push(pcm(0, 2)) == pcm(0, 2)


def test_transcript_script_normalization_preserves_latin_text() -> None:
    assert normalize_transcript("軟體 API 測試", "simplified") == "软件 API 测试"
    assert normalize_transcript("軟體 API 測試", "original") == "軟體 API 測試"


def test_boolean_query_parser_is_strict() -> None:
    assert parse_boolean_query("true") is True
    assert parse_boolean_query("0") is False
    assert parse_boolean_query("sometimes") is None
