from __future__ import annotations

from pathlib import Path

from audio_subtitle_batch_copy.models import MediaInfo, MediaStream


def touch_media(path: Path, content: bytes = b"fixture") -> Path:
    path.write_bytes(content)
    return path


def stream(
    index: int,
    kind: str,
    codec: str,
    *,
    language: str = "und",
    title: str = "",
    default: bool = False,
    dispositions: frozenset[str] = frozenset(),
    channels: int | None = None,
) -> MediaStream:
    flags = set(dispositions)
    if default:
        flags.add("default")
    tags = {"language": language}
    if title:
        tags["title"] = title
    return MediaStream(
        index=index,
        codec_type=kind,
        codec_name=codec,
        tags=tags,
        dispositions=frozenset(flags),
        channels=channels,
    )


def media_info(path: Path, streams: tuple[MediaStream, ...], duration: float = 2.0) -> MediaInfo:
    stat = path.stat()
    return MediaInfo(
        path=path,
        streams=streams,
        format_name="fixture",
        duration=duration,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
