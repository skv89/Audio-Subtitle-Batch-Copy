from __future__ import annotations

from pathlib import Path

import pytest

from audio_subtitle_batch_copy.media import (
    ToolchainError,
    parse_probe_payload,
    parse_tool_version,
    stream_display_label,
)


def test_parse_release_version_9() -> None:
    version = parse_tool_version("ffmpeg version 9.0-essentials_build-www.gyan.dev\n")
    assert (version.major, version.minor, version.patch) == (9, 0, None)


def test_git_date_build_is_not_misidentified_as_release() -> None:
    with pytest.raises(ToolchainError, match="does not identify a numbered release"):
        parse_tool_version("ffmpeg version 2026-08-03-git-01a25f74cc-full_build\n")


def test_probe_parser_preserves_tags_dispositions_and_duration(tmp_path: Path) -> None:
    path = tmp_path / "media.mkv"
    path.write_bytes(b"x")
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 6,
                "channel_layout": "5.1",
                "tags": {"language": "jpn", "title": "Main"},
                "disposition": {"default": 1, "forced": 0, "original": 1},
            }
        ],
        "format": {"format_name": "matroska,webm", "duration": "12.5"},
    }
    info = parse_probe_payload(path, payload)
    assert info.duration == 12.5
    assert info.audio_streams[0].dispositions == frozenset({"default", "original"})
    assert stream_display_label(info.audio_streams[0], 1) == (
        "Audio 1 · jpn · Main · AAC · 5.1 · current default"
    )


def test_probe_parser_tolerates_missing_disposition_object(tmp_path: Path) -> None:
    path = tmp_path / "media.wav"
    path.write_bytes(b"x")
    info = parse_probe_payload(
        path,
        {"streams": [{"index": 0, "codec_type": "audio", "codec_name": "pcm_s16le"}]},
    )
    assert info.audio_streams[0].dispositions == frozenset()
