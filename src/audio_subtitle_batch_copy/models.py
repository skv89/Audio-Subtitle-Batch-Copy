from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeVar, cast

from .constants import MAX_FILES_PER_SIDE


def new_entry_id() -> str:
    return uuid.uuid4().hex


TrackOrigin = Literal["source", "destination"]


@dataclass(frozen=True, slots=True)
class TrackSelection:
    """Unambiguous identity for one default-track choice in a paired row."""

    origin: TrackOrigin
    stream_index: int

    def to_token(self) -> str:
        """Return a Qt/frozen-build-safe scalar representation."""
        return f"{self.origin}:{self.stream_index}"

    @classmethod
    def from_token(cls, token: str) -> TrackSelection:
        origin_text, separator, index_text = token.partition(":")
        if separator != ":" or origin_text not in {"source", "destination"}:
            raise ValueError(f"Invalid track-selection token: {token!r}")
        try:
            stream_index = int(index_text)
        except ValueError as exc:
            raise ValueError(f"Invalid track-selection token: {token!r}") from exc
        if stream_index < 0:
            raise ValueError(f"Invalid track-selection token: {token!r}")
        return cls(cast(TrackOrigin, origin_text), stream_index)


@dataclass(frozen=True, slots=True)
class MediaStream:
    index: int
    codec_type: str
    codec_name: str
    tags: dict[str, str] = field(default_factory=dict)
    dispositions: frozenset[str] = frozenset()
    channels: int | None = None
    channel_layout: str | None = None
    duration: float | None = None

    @property
    def language(self) -> str:
        return self.tags.get("language", "und") or "und"

    @property
    def title(self) -> str:
        return (
            self.tags.get("title", "")
            or self.tags.get("name", "")
            or self.tags.get("handler_name", "")
        ).strip()

    @property
    def is_default(self) -> bool:
        return "default" in self.dispositions


@dataclass(frozen=True, slots=True)
class MediaInfo:
    path: Path
    streams: tuple[MediaStream, ...]
    format_name: str
    duration: float | None
    size: int
    mtime_ns: int

    @property
    def video_streams(self) -> tuple[MediaStream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "video")

    @property
    def audio_streams(self) -> tuple[MediaStream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "audio")

    @property
    def subtitle_streams(self) -> tuple[MediaStream, ...]:
        return tuple(stream for stream in self.streams if stream.codec_type == "subtitle")

    def stream_by_index(self, index: int) -> MediaStream | None:
        return next((stream for stream in self.streams if stream.index == index), None)


@dataclass(slots=True)
class SourceEntry:
    path: Path
    id: str = field(default_factory=new_entry_id)
    info: MediaInfo | None = None
    probe_error: str | None = None
    probing: bool = True
    selected_audio_index: int | None = None
    selected_subtitle_index: int | None = None
    copy_audio: bool = True
    copy_subtitles: bool = True

    def apply_probe(self, info: MediaInfo) -> None:
        self.info = info
        self.probe_error = None
        self.probing = False
        audio = info.audio_streams
        subtitles = info.subtitle_streams
        if self.selected_audio_index not in {stream.index for stream in audio}:
            if audio:
                default_audio = next((stream for stream in audio if stream.is_default), audio[0])
                self.selected_audio_index = default_audio.index
            else:
                self.selected_audio_index = None
        if self.selected_subtitle_index not in {stream.index for stream in subtitles}:
            default_subtitle = next((stream for stream in subtitles if stream.is_default), None)
            self.selected_subtitle_index = default_subtitle.index if default_subtitle else None

    def apply_probe_error(self, message: str) -> None:
        self.info = None
        self.probe_error = message
        self.probing = False
        self.selected_audio_index = None
        self.selected_subtitle_index = None


@dataclass(slots=True)
class DestinationEntry:
    path: Path
    id: str = field(default_factory=new_entry_id)
    info: MediaInfo | None = None
    probe_error: str | None = None
    probing: bool = True
    keep_audio: bool = False
    keep_subtitles: bool = False

    def apply_probe(self, info: MediaInfo) -> None:
        self.info = info
        self.probe_error = None
        self.probing = False

    def apply_probe_error(self, message: str) -> None:
        self.info = None
        self.probe_error = message
        self.probing = False


EntryT = TypeVar("EntryT", SourceEntry, DestinationEntry)


def _move_existing_entry(entries: list[EntryT], entry_id: str, insertion_index: int) -> int | None:
    old_index = next(
        (index for index, entry in enumerate(entries) if entry.id == entry_id),
        None,
    )
    if old_index is None:
        return None
    clamped_index = max(0, min(insertion_index, len(entries)))
    entry = entries.pop(old_index)
    if clamped_index > old_index:
        clamped_index -= 1
    new_index = max(0, min(clamped_index, len(entries)))
    entries.insert(new_index, entry)
    return new_index


@dataclass(frozen=True, slots=True)
class AddResult:
    added: int
    invalid: tuple[Path, ...]
    over_limit: tuple[Path, ...]


_NATURAL_PART = re.compile(r"(\d+)")


def natural_path_key(path: Path) -> tuple[tuple[tuple[int, int | str], ...], str]:
    parts: list[tuple[int, int | str]] = []
    for part in _NATURAL_PART.split(path.name.casefold()):
        if not part:
            continue
        parts.append((0, int(part)) if part.isdigit() else (1, part))
    return tuple(parts), str(path).casefold()


class BatchState:
    def __init__(self) -> None:
        self.sources: list[SourceEntry] = []
        self.destinations: list[DestinationEntry] = []

    @property
    def row_count(self) -> int:
        return max(len(self.sources), len(self.destinations))

    def add_paths(self, side: Literal["source", "destination"], paths: list[Path]) -> AddResult:
        entries: list[SourceEntry] | list[DestinationEntry]
        entries = self.sources if side == "source" else self.destinations
        invalid: list[Path] = []
        valid: list[Path] = []
        for raw_path in paths:
            path = raw_path.expanduser()
            if not path.is_file():
                invalid.append(path)
            else:
                valid.append(path.resolve(strict=False))
        available = max(0, MAX_FILES_PER_SIDE - len(entries))
        accepted, over_limit = valid[:available], valid[available:]
        if side == "source":
            self.sources.extend(SourceEntry(path=path) for path in accepted)
        else:
            self.destinations.extend(DestinationEntry(path=path) for path in accepted)
        return AddResult(len(accepted), tuple(invalid), tuple(over_limit))

    def find_source(self, entry_id: str) -> SourceEntry | None:
        return next((entry for entry in self.sources if entry.id == entry_id), None)

    def find_destination(self, entry_id: str) -> DestinationEntry | None:
        return next((entry for entry in self.destinations if entry.id == entry_id), None)

    def sort_sources(self, ascending: bool) -> None:
        self.sources.sort(key=lambda entry: natural_path_key(entry.path), reverse=not ascending)

    def sort_destinations(self, ascending: bool) -> None:
        self.destinations.sort(
            key=lambda entry: natural_path_key(entry.path), reverse=not ascending
        )

    def move_entry(
        self,
        side: Literal["source", "destination"],
        entry_id: str,
        insertion_index: int,
    ) -> int | None:
        """Move one side's existing entry and return its new row.

        ``insertion_index`` is the gap before which the entry is dropped and may
        range from zero through the original list length. Keeping the existing
        entry object preserves every track selection and side-specific checkbox.
        """
        if side == "source":
            return _move_existing_entry(self.sources, entry_id, insertion_index)
        return _move_existing_entry(self.destinations, entry_id, insertion_index)

    def remove_rows(self, side: Literal["source", "destination"], rows: list[int]) -> None:
        entries = self.sources if side == "source" else self.destinations
        for row in sorted({row for row in rows if 0 <= row < len(entries)}, reverse=True):
            entries.pop(row)

    def clear(self) -> None:
        self.sources.clear()
        self.destinations.clear()
