from __future__ import annotations

from pathlib import Path

import pytest

from audio_subtitle_batch_copy.isobmff import (
    IsoBmffError,
    audio_track_enabled_flags,
    inspect_iso_bmff_track_headers,
    is_iso_bmff_path,
)


def box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def track(track_id: int, handler_type: bytes, *, enabled: bool) -> bytes:
    flags = 3 if enabled else 2
    tkhd_payload = (
        bytes([0])
        + flags.to_bytes(3, "big")
        + (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + track_id.to_bytes(4, "big")
    )
    hdlr_payload = bytes(4) + bytes(4) + handler_type
    return box(b"trak", box(b"tkhd", tkhd_payload) + box(b"mdia", box(b"hdlr", hdlr_payload)))


def test_iso_bmff_parser_reads_audio_tkhd_enabled_flags_in_track_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixture.mp4"
    path.write_bytes(
        box(
            b"moov",
            track(1, b"vide", enabled=True)
            + track(2, b"soun", enabled=True)
            + track(3, b"soun", enabled=False),
        )
    )

    headers = inspect_iso_bmff_track_headers(path)

    assert [(item.track_id, item.handler_type, item.enabled) for item in headers] == [
        (1, "vide", True),
        (2, "soun", True),
        (3, "soun", False),
    ]
    assert audio_track_enabled_flags(path) == (True, False)
    assert is_iso_bmff_path(path)
    assert not is_iso_bmff_path(path.with_suffix(".mkv"))


def test_iso_bmff_parser_fails_closed_on_missing_or_truncated_moov(tmp_path: Path) -> None:
    missing = tmp_path / "missing.mp4"
    missing.write_bytes(box(b"free", b"payload"))
    with pytest.raises(IsoBmffError, match="no moov"):
        inspect_iso_bmff_track_headers(missing)

    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes((100).to_bytes(4, "big") + b"moov")
    with pytest.raises(IsoBmffError, match="Invalid ISO-BMFF box size"):
        inspect_iso_bmff_track_headers(truncated)
