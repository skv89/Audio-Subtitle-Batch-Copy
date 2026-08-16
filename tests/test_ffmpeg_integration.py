from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from audio_subtitle_batch_copy.app import _compatibility_self_test
from audio_subtitle_batch_copy.isobmff import audio_track_enabled_flags
from audio_subtitle_batch_copy.media import (
    FFmpegToolchain,
    discover_toolchain,
    no_window_creation_flags,
    probe_media,
)
from audio_subtitle_batch_copy.models import TrackSelection
from audio_subtitle_batch_copy.planner import PreparedJob, plan_output
from audio_subtitle_batch_copy.processor import BatchProcessor


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=no_window_creation_flags(),
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"Command failed ({completed.returncode}): {subprocess.list2cmdline(command)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return completed


def raw_probe(toolchain: FFmpegToolchain, path: Path) -> dict[str, Any]:
    completed = run_command(
        [
            str(toolchain.ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def stream_hash(toolchain: FFmpegToolchain, path: Path, specifier: str) -> str:
    completed = run_command(
        [
            str(toolchain.ffmpeg),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            f"0:{specifier}",
            "-c",
            "copy",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ]
    )
    return completed.stdout.strip()


@pytest.fixture(scope="module")
def toolchain() -> FFmpegToolchain:
    result = discover_toolchain()
    assert result.version.major == 9
    assert result.version.minor == 0
    return result


@pytest.fixture()
def controlled_media(toolchain: FFmpegToolchain, tmp_path: Path) -> tuple[Path, Path]:
    media_directory = tmp_path / "Média 日本語 files"
    media_directory.mkdir()
    subtitle_eng = media_directory / "english captions.srt"
    subtitle_spa = media_directory / "spanish captions.srt"
    subtitle_eng.write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nEnglish subtitle\n", encoding="utf-8"
    )
    subtitle_spa.write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nSpanish subtitle\n", encoding="utf-8"
    )
    source = media_directory / "source tracks.mkv"
    destination = media_directory / "destination video.mp4"

    run_command(
        [
            str(toolchain.ffmpeg),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1.2",
            "-i",
            str(subtitle_eng),
            "-i",
            str(subtitle_spa),
            "-map",
            "0:a:0",
            "-map",
            "1:a:0",
            "-map",
            "2:s:0",
            "-map",
            "3:s:0",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-c:s",
            "srt",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:0",
            "title=English source",
            "-metadata:s:a:1",
            "language=jpn",
            "-metadata:s:a:1",
            "title=Japanese source",
            "-metadata:s:s:0",
            "language=eng",
            "-metadata:s:s:0",
            "title=English captions",
            "-metadata:s:s:1",
            "language=spa",
            "-metadata:s:s:1",
            "title=Spanish forced",
            "-disposition:a:0",
            "0",
            "-disposition:a:1",
            "default",
            "-disposition:s:0",
            "default",
            "-disposition:s:1",
            "forced",
            "-t",
            "1.0",
            str(source),
        ]
    )
    run_command(
        [
            str(toolchain.ffmpeg),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=10:d=1.0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.0",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-metadata",
            "title=Destination metadata",
            "-metadata:s:a:0",
            "language=fra",
            "-metadata:s:a:0",
            "title=Destination audio",
            "-disposition:a:0",
            "default",
            "-shortest",
            str(destination),
        ]
    )
    return source, destination


def make_job(
    toolchain: FFmpegToolchain,
    source: Path,
    destination: Path,
    *,
    suffix: str,
    copy_subtitles: bool,
    keep_destination_audio: bool,
    selected_subtitle: TrackSelection | None,
    selected_audio: TrackSelection | None = None,
    keep_destination_subtitles: bool = False,
) -> PreparedJob:
    source_info = probe_media(toolchain.ffprobe, source)
    destination_info = probe_media(toolchain.ffprobe, destination)
    assert len(source_info.audio_streams) == 2
    assert len(source_info.subtitle_streams) == 2
    return PreparedJob(
        row_number=1,
        source=source_info,
        destination=destination_info,
        selected_audio=selected_audio
        or TrackSelection("source", source_info.audio_streams[0].index),
        selected_subtitle=selected_subtitle,
        copy_audio=True,
        copy_subtitles=copy_subtitles,
        keep_destination_audio=keep_destination_audio,
        keep_destination_subtitles=keep_destination_subtitles,
        output=plan_output(destination, suffix, None),
    )


@pytest.mark.integration
def test_release_compatibility_self_test_exercises_real_ffmpeg_mkv_default_path(
    toolchain: FFmpegToolchain,
) -> None:
    assert _compatibility_self_test(toolchain)


@pytest.mark.integration
def test_same_container_mp4_direct_copy_preserves_packets_metadata_and_selection(
    toolchain: FFmpegToolchain,
    controlled_media: tuple[Path, Path],
) -> None:
    source, destination = controlled_media
    source_before = probe_media(toolchain.ffprobe, source)
    assert [item.is_default for item in source_before.audio_streams] == [False, True]
    job = make_job(
        toolchain,
        source,
        destination,
        suffix="_direct",
        copy_subtitles=False,
        keep_destination_audio=False,
        selected_subtitle=None,
    )
    logs: list[str] = []
    result = BatchProcessor(toolchain).run_job(job, lambda _value: None, logs.append)
    assert result.status == "success"
    assert result.output_path == destination.with_name("destination video_direct.mp4")
    assert result.output_path.is_file()
    output = probe_media(toolchain.ffprobe, result.output_path)
    assert [(item.codec_type, item.codec_name) for item in output.streams] == [
        ("video", "mpeg4"),
        ("audio", "aac"),
        ("audio", "aac"),
    ]
    assert [item.language for item in output.audio_streams] == ["eng", "jpn"]
    assert output.audio_streams[0].title == "English source"
    assert [item.is_default for item in output.audio_streams] == [True, False]
    assert audio_track_enabled_flags(result.output_path) == (True, False)
    payload = raw_probe(toolchain, result.output_path)
    assert payload["format"]["tags"]["title"] == "Destination metadata"
    assert stream_hash(toolchain, destination, "v:0") == stream_hash(
        toolchain, result.output_path, "v:0"
    )
    assert stream_hash(toolchain, source, "a:0") == stream_hash(
        toolchain, result.output_path, "a:0"
    )
    assert any("-c copy" in line for line in logs)
    assert any("ISO-BMFF audio tkhd Enabled flags match" in line for line in logs)


@pytest.mark.integration
def test_incompatible_mp4_subtitles_retry_as_mkv_with_exact_defaults(
    toolchain: FFmpegToolchain,
    controlled_media: tuple[Path, Path],
) -> None:
    source, destination = controlled_media
    source_info = probe_media(toolchain.ffprobe, source)
    selected_subtitle = source_info.subtitle_streams[1].index
    job = make_job(
        toolchain,
        source,
        destination,
        suffix="_fallback",
        copy_subtitles=True,
        keep_destination_audio=True,
        selected_subtitle=TrackSelection("source", selected_subtitle),
    )
    logs: list[str] = []
    result = BatchProcessor(toolchain).run_job(job, lambda _value: None, logs.append)
    assert result.status == "fallback"
    assert result.output_path == destination.with_name("destination video_fallback.mkv")
    assert not destination.with_name("destination video_fallback.mp4").exists()
    output = probe_media(toolchain.ffprobe, result.output_path)
    assert [(item.codec_type, item.codec_name) for item in output.streams] == [
        ("video", "mpeg4"),
        ("audio", "aac"),
        ("audio", "aac"),
        ("audio", "aac"),
        ("subtitle", "subrip"),
        ("subtitle", "subrip"),
    ]
    assert [item.language for item in output.audio_streams] == ["eng", "fra", "jpn"]
    assert [item.is_default for item in output.audio_streams] == [True, False, False]
    assert [item.language for item in output.subtitle_streams] == ["spa", "eng"]
    assert [item.is_default for item in output.subtitle_streams] == [True, False]
    assert "forced" in output.subtitle_streams[0].dispositions
    payload = raw_probe(toolchain, result.output_path)
    assert payload["format"]["tags"]["title"] == "Destination metadata"
    assert stream_hash(toolchain, destination, "v:0") == stream_hash(
        toolchain, result.output_path, "v:0"
    )
    assert stream_hash(toolchain, destination, "a:0") == stream_hash(
        toolchain, result.output_path, "a:1"
    )
    assert stream_hash(toolchain, source, "a:0") == stream_hash(
        toolchain, result.output_path, "a:0"
    )
    assert any("retrying direct copy as MKV" in line for line in logs)

    no_default_job = make_job(
        toolchain,
        source,
        destination,
        suffix="_no_default",
        copy_subtitles=True,
        keep_destination_audio=False,
        selected_subtitle=None,
    )
    no_default_result = BatchProcessor(toolchain).run_job(
        no_default_job, lambda _value: None, lambda _line: None
    )
    assert no_default_result.status == "fallback"
    no_default_output = probe_media(toolchain.ffprobe, no_default_result.output_path)
    assert no_default_output.subtitle_streams
    assert not any(item.is_default for item in no_default_output.subtitle_streams)


@pytest.mark.integration
def test_retained_destination_audio_and_subtitle_can_be_selected_as_defaults(
    toolchain: FFmpegToolchain,
    controlled_media: tuple[Path, Path],
) -> None:
    source, mp4_destination = controlled_media
    destination = mp4_destination.with_name("destination with matching subtitle.mkv")
    run_command(
        [
            str(toolchain.ffmpeg),
            "-hide_banner",
            "-y",
            "-i",
            str(mp4_destination),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map",
            "1:s:0",
            "-map_metadata",
            "0",
            "-c",
            "copy",
            "-metadata:s:a:0",
            "title=English source",
            "-metadata:s:s:0",
            "title=English captions",
            "-disposition:a:0",
            "default",
            "-disposition:s:0",
            "default",
            str(destination),
        ]
    )
    destination_info = probe_media(toolchain.ffprobe, destination)
    assert len(destination_info.audio_streams) == 1
    assert len(destination_info.subtitle_streams) == 1

    job = make_job(
        toolchain,
        source,
        destination,
        suffix="_destination_defaults",
        copy_subtitles=True,
        keep_destination_audio=True,
        keep_destination_subtitles=True,
        selected_audio=TrackSelection(
            "destination", destination_info.audio_streams[0].index
        ),
        selected_subtitle=TrackSelection(
            "destination", destination_info.subtitle_streams[0].index
        ),
    )
    logs: list[str] = []
    result = BatchProcessor(toolchain).run_job(job, lambda _value: None, logs.append)

    assert result.status == "success"
    assert result.output_path == destination.with_name(
        "destination with matching subtitle_destination_defaults.mkv"
    )
    output = probe_media(toolchain.ffprobe, result.output_path)
    assert [(item.codec_type, item.codec_name) for item in output.streams] == [
        ("video", "mpeg4"),
        ("audio", "aac"),
        ("audio", "aac"),
        ("audio", "aac"),
        ("subtitle", "subrip"),
        ("subtitle", "subrip"),
        ("subtitle", "subrip"),
    ]
    assert [item.is_default for item in output.audio_streams] == [True, False, False]
    assert [item.is_default for item in output.subtitle_streams] == [True, False, False]
    assert output.audio_streams[0].title == "English source"
    assert output.audio_streams[1].title == "English source"
    assert output.subtitle_streams[0].title == "English captions"
    assert output.subtitle_streams[1].title == "English captions"
    assert stream_hash(toolchain, destination, "v:0") == stream_hash(
        toolchain, result.output_path, "v:0"
    )
    assert stream_hash(toolchain, destination, "a:0") == stream_hash(
        toolchain, result.output_path, "a:0"
    )
    assert stream_hash(toolchain, source, "a:0") == stream_hash(
        toolchain, result.output_path, "a:1"
    )
    assert stream_hash(toolchain, destination, "s:0") == stream_hash(
        toolchain, result.output_path, "s:0"
    )
    assert stream_hash(toolchain, source, "s:0") == stream_hash(
        toolchain, result.output_path, "s:1"
    )
    assert any("-c copy" in line for line in logs)


@pytest.mark.integration
def test_blank_suffix_replaces_destination_only_after_verified_temp_success(
    toolchain: FFmpegToolchain,
    controlled_media: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source, original_destination = controlled_media
    destination = tmp_path / "replace_me.mp4"
    destination.write_bytes(original_destination.read_bytes())
    original_video_hash = stream_hash(toolchain, destination, "v:0")
    job = make_job(
        toolchain,
        source,
        destination,
        suffix="",
        copy_subtitles=False,
        keep_destination_audio=False,
        selected_subtitle=None,
    )
    assert job.output.overwrites_destination
    job = replace(job, replace_primary=True)
    result = BatchProcessor(toolchain).run_job(job, lambda _value: None, lambda _line: None)
    assert result.status == "success"
    assert result.output_path == destination
    output = probe_media(toolchain.ffprobe, destination)
    assert len(output.video_streams) == 1
    assert len(output.audio_streams) == 2
    assert output.audio_streams[0].language == "eng"
    assert stream_hash(toolchain, destination, "v:0") == original_video_hash
    assert not list(tmp_path.glob(".*.abcopy-*"))


@pytest.mark.integration
def test_unapproved_existing_output_and_changed_input_fail_safe(
    toolchain: FFmpegToolchain,
    controlled_media: tuple[Path, Path],
) -> None:
    source, destination = controlled_media
    job = make_job(
        toolchain,
        source,
        destination,
        suffix="_exists",
        copy_subtitles=False,
        keep_destination_audio=False,
        selected_subtitle=None,
    )
    job.output.primary.write_bytes(b"do not replace")
    before = hashlib.sha256(job.output.primary.read_bytes()).hexdigest()
    skipped = BatchProcessor(toolchain).run_job(job, lambda _value: None, lambda _line: None)
    assert skipped.status == "skipped"
    assert hashlib.sha256(job.output.primary.read_bytes()).hexdigest() == before

    changed_job = make_job(
        toolchain,
        source,
        destination,
        suffix="_changed",
        copy_subtitles=False,
        keep_destination_audio=False,
        selected_subtitle=None,
    )
    source.write_bytes(source.read_bytes() + b"changed")
    failed = BatchProcessor(toolchain).run_job(changed_job, lambda _value: None, lambda _line: None)
    assert failed.status == "failed"
    assert "changed after track selection" in failed.message
    assert not changed_job.output.primary.exists()


@pytest.mark.integration
def test_cancel_before_row_starts_creates_no_output(
    toolchain: FFmpegToolchain,
    controlled_media: tuple[Path, Path],
) -> None:
    source, destination = controlled_media
    job = make_job(
        toolchain,
        source,
        destination,
        suffix="_cancelled",
        copy_subtitles=False,
        keep_destination_audio=False,
        selected_subtitle=None,
    )
    processor = BatchProcessor(toolchain)
    processor.cancel()
    result = processor.run_job(job, lambda _value: None, lambda _line: None)
    assert result.status == "cancelled"
    assert not job.output.primary.exists()


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-lock semantics")
def test_locked_existing_output_is_preserved_and_completed_temp_is_recoverable(
    toolchain: FFmpegToolchain,
    controlled_media: tuple[Path, Path],
) -> None:
    import ctypes
    from ctypes import wintypes

    source, destination = controlled_media
    job = make_job(
        toolchain,
        source,
        destination,
        suffix="_locked",
        copy_subtitles=False,
        keep_destination_audio=False,
        selected_subtitle=None,
    )
    original = b"locked existing output"
    job.output.primary.write_bytes(original)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(job.output.primary),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ; deliberately deny delete/write sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    assert handle != invalid_handle
    try:
        result = BatchProcessor(toolchain).run_job(
            replace(job, replace_primary=True), lambda _value: None, lambda _line: None
        )
        assert result.status == "failed"
        assert result.recovery_path is not None
        assert result.recovery_path.is_file()
        assert probe_media(toolchain.ffprobe, result.recovery_path).video_streams
        assert job.output.primary.read_bytes() == original
    finally:
        kernel32.CloseHandle(handle)
