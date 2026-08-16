from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import DESTINATION_EXTENSIONS, REQUIRED_FFMPEG_MAJOR, SOURCE_EXTENSIONS
from .models import MediaInfo, MediaStream, natural_path_key


class ToolchainError(RuntimeError):
    pass


class MediaProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolVersion:
    raw: str
    major: int
    minor: int
    patch: int | None


@dataclass(frozen=True, slots=True)
class FFmpegToolchain:
    ffmpeg: Path
    ffprobe: Path
    version: ToolVersion


_VERSION_LINE = re.compile(r"^ff(?:mpeg|probe) version\s+(?P<raw>\S+)", re.IGNORECASE)
_SEMVER = re.compile(r"(?<!\d)(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?")


def no_window_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def parse_tool_version(output: str) -> ToolVersion:
    first_line = output.splitlines()[0].strip() if output.splitlines() else ""
    line_match = _VERSION_LINE.search(first_line)
    if not line_match:
        raise ToolchainError(f"Unrecognized FFmpeg version output: {first_line or '(empty)'}")
    raw = line_match.group("raw")
    semver = _SEMVER.search(raw)
    if not semver:
        raise ToolchainError(
            f"FFmpeg build '{raw}' does not identify a numbered release such as 9.0."
        )
    patch_text = semver.group("patch")
    return ToolVersion(
        raw=raw,
        major=int(semver.group("major")),
        minor=int(semver.group("minor")),
        patch=int(patch_text) if patch_text is not None else None,
    )


def _tool_output(path: Path) -> str:
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=no_window_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolchainError(f"Could not run {path}: {exc}") from exc
    output = completed.stdout or completed.stderr
    if completed.returncode != 0:
        raise ToolchainError(
            f"{path.name} exited with code {completed.returncode}: {output.strip()}"
        )
    return output


def _candidate_bin_directories() -> Iterable[Path]:
    seen: set[str] = set()
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "tools" / "ffmpeg" / "bin")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(str(meipass)) / "tools" / "ffmpeg" / "bin")
    source_root = Path(__file__).resolve().parents[2]
    candidates.append(source_root / "tools" / "ffmpeg" / "bin")
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            yield candidate


def discover_toolchain(required_major: int = REQUIRED_FFMPEG_MAJOR) -> FFmpegToolchain:
    failures: list[str] = []
    pairs: list[tuple[Path, Path]] = []
    executable_suffix = ".exe" if os.name == "nt" else ""
    for bin_dir in _candidate_bin_directories():
        pairs.append(
            (bin_dir / f"ffmpeg{executable_suffix}", bin_dir / f"ffprobe{executable_suffix}")
        )
    path_ffmpeg = shutil.which("ffmpeg")
    path_ffprobe = shutil.which("ffprobe")
    if path_ffmpeg and path_ffprobe:
        pairs.append((Path(path_ffmpeg), Path(path_ffprobe)))

    for ffmpeg, ffprobe in pairs:
        if not ffmpeg.is_file() or not ffprobe.is_file():
            failures.append(f"Missing pair: {ffmpeg} / {ffprobe}")
            continue
        try:
            ffmpeg_version = parse_tool_version(_tool_output(ffmpeg))
            ffprobe_version = parse_tool_version(_tool_output(ffprobe))
        except ToolchainError as exc:
            failures.append(str(exc))
            continue
        if ffmpeg_version.major != required_major or ffprobe_version.major != required_major:
            failures.append(
                f"Rejected {ffmpeg}: FFmpeg {ffmpeg_version.raw} and ffprobe "
                f"{ffprobe_version.raw}; version {required_major}.x is required."
            )
            continue
        return FFmpegToolchain(ffmpeg.resolve(), ffprobe.resolve(), ffmpeg_version)

    detail = "\n".join(f"• {failure}" for failure in failures[-6:])
    raise ToolchainError(
        f"FFmpeg/ffprobe {required_major}.x were not found. The portable release expects "
        "both files in tools\\ffmpeg\\bin.\n" + detail
    )


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def parse_probe_payload(path: Path, payload: dict[str, Any]) -> MediaInfo:
    raw_streams = payload.get("streams", [])
    streams: list[MediaStream] = []
    for raw in raw_streams if isinstance(raw_streams, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw["index"])
        except (KeyError, TypeError, ValueError):
            continue
        tags_raw = raw.get("tags")
        tags = (
            {str(key): str(value) for key, value in tags_raw.items()}
            if isinstance(tags_raw, dict)
            else {}
        )
        dispositions_raw = raw.get("disposition")
        dispositions = (
            frozenset(str(key) for key, value in dispositions_raw.items() if bool(value))
            if isinstance(dispositions_raw, dict)
            else frozenset()
        )
        channels_raw = raw.get("channels")
        try:
            channels = int(channels_raw) if channels_raw is not None else None
        except (TypeError, ValueError):
            channels = None
        streams.append(
            MediaStream(
                index=index,
                codec_type=str(raw.get("codec_type", "unknown")),
                codec_name=str(raw.get("codec_name", "unknown")),
                tags=tags,
                dispositions=dispositions,
                channels=channels,
                channel_layout=(
                    str(raw["channel_layout"]) if raw.get("channel_layout") is not None else None
                ),
                duration=_as_float(raw.get("duration")),
            )
        )

    format_raw = payload.get("format")
    format_dict = format_raw if isinstance(format_raw, dict) else {}
    duration = _as_float(format_dict.get("duration"))
    if duration is None:
        stream_durations = [stream.duration for stream in streams if stream.duration is not None]
        duration = max(stream_durations) if stream_durations else None
    stat = path.stat()
    return MediaInfo(
        path=path,
        streams=tuple(sorted(streams, key=lambda stream: stream.index)),
        format_name=str(format_dict.get("format_name", "unknown")),
        duration=duration,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def probe_media(ffprobe: Path, path: Path) -> MediaInfo:
    if not path.is_file():
        raise MediaProbeError(f"File no longer exists: {path}")
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=no_window_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError(f"Could not inspect {path.name}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "ffprobe returned no diagnostic."
        raise MediaProbeError(f"Could not inspect {path.name}: {detail[-1200:]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"ffprobe returned invalid JSON for {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MediaProbeError(f"ffprobe returned an unexpected result for {path.name}.")
    return parse_probe_payload(path, payload)


def collect_media_files(directory: Path, side: str) -> list[Path]:
    extensions = SOURCE_EXTENSIONS if side == "source" else DESTINATION_EXTENSIONS
    try:
        files = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() in extensions
        ]
    except OSError as exc:
        raise MediaProbeError(f"Could not read folder {directory}: {exc}") from exc
    return sorted(files, key=natural_path_key)


def stream_display_label(stream: MediaStream, type_ordinal: int) -> str:
    kind = "Audio" if stream.codec_type == "audio" else "Subtitle"
    pieces = [f"{kind} {type_ordinal}", stream.language]
    if stream.title:
        pieces.append(stream.title)
    pieces.append(stream.codec_name.upper())
    if stream.codec_type == "audio":
        if stream.channel_layout:
            pieces.append(stream.channel_layout)
        elif stream.channels is not None:
            pieces.append(f"{stream.channels} ch")
    if stream.is_default:
        pieces.append("current default")
    return " · ".join(pieces)
