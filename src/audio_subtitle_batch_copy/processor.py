from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from .isobmff import IsoBmffError, audio_track_enabled_flags, is_iso_bmff_path
from .media import FFmpegToolchain, MediaProbeError, no_window_creation_flags, probe_media
from .models import MediaInfo
from .planner import (
    PreparedJob,
    build_ffmpeg_command,
    expected_default_indices,
    mapped_streams,
)

JobStatus = Literal["success", "fallback", "failed", "skipped", "cancelled"]
ProgressCallback = Callable[[float], None]
LogCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class JobResult:
    row_number: int
    status: JobStatus
    output_path: Path | None
    message: str
    recovery_path: Path | None = None


@dataclass(frozen=True, slots=True)
class AttemptResult:
    succeeded: bool
    cancelled: bool
    message: str
    recovery_path: Path | None = None


def _fingerprint_matches(baseline: MediaInfo) -> bool:
    try:
        stat = baseline.path.stat()
    except OSError:
        return False
    return stat.st_size == baseline.size and stat.st_mtime_ns == baseline.mtime_ns


def _read_pipe(
    pipe: TextIO,
    channel: Literal["stdout", "stderr"],
    messages: queue.Queue[tuple[str, str | None]],
) -> None:
    try:
        for line in pipe:
            messages.put((channel, str(line).rstrip("\r\n")))
    finally:
        messages.put((channel, None))


def _safe_unlink(path: Path) -> bool:
    """Best-effort cleanup with a short retry window for Windows handle release."""
    for attempt in range(10):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if attempt < 9:
                time.sleep(0.1)
            continue
        return True
    try:
        return not path.exists()
    except OSError:
        return False


def _temporary_path(final_path: Path) -> Path:
    token = uuid.uuid4().hex
    return final_path.with_name(f".{final_path.stem}.abcopy-{token}{final_path.suffix}")


def _duration_for_progress(job: PreparedJob) -> float | None:
    durations = [
        duration
        for duration in (job.source.duration, job.destination.duration)
        if duration is not None and duration > 0
    ]
    return max(durations) if durations else None


def _validate_output(ffprobe: Path, job: PreparedJob, output_path: Path) -> str | None:
    try:
        actual = probe_media(ffprobe, output_path)
    except (MediaProbeError, OSError) as exc:
        return f"Completed mux could not be inspected: {exc}"
    expected = mapped_streams(job)
    expected_signature = [(item.stream.codec_type, item.stream.codec_name) for item in expected]
    actual_signature = [(stream.codec_type, stream.codec_name) for stream in actual.streams]
    if actual_signature != expected_signature:
        return (
            "Output stream inventory differs from the direct-copy plan. "
            f"Expected {expected_signature}; found {actual_signature}."
        )
    expected_audio_defaults, expected_subtitle_defaults = expected_default_indices(job)
    actual_audio_defaults = {
        ordinal for ordinal, stream in enumerate(actual.audio_streams) if stream.is_default
    }
    actual_subtitle_defaults = {
        ordinal for ordinal, stream in enumerate(actual.subtitle_streams) if stream.is_default
    }
    if actual_audio_defaults != expected_audio_defaults:
        return (
            "Output audio default disposition differs from the selection. "
            f"Expected {sorted(expected_audio_defaults)}; found {sorted(actual_audio_defaults)}."
        )
    if actual_subtitle_defaults != expected_subtitle_defaults:
        return (
            "Output subtitle default disposition differs from the selection. "
            f"Expected {sorted(expected_subtitle_defaults)}; "
            f"found {sorted(actual_subtitle_defaults)}."
        )
    if is_iso_bmff_path(output_path):
        try:
            audio_enabled_flags = audio_track_enabled_flags(output_path)
        except IsoBmffError as exc:
            return f"Completed ISO-BMFF track headers could not be inspected: {exc}"
        if len(audio_enabled_flags) != len(actual.audio_streams):
            return (
                "ISO-BMFF audio track-header inventory differs from ffprobe. "
                f"Expected {len(actual.audio_streams)}; found {len(audio_enabled_flags)}."
            )
        actual_audio_enabled = {
            ordinal for ordinal, enabled in enumerate(audio_enabled_flags) if enabled
        }
        if actual_audio_enabled != expected_audio_defaults:
            return (
                "ISO-BMFF audio tkhd Enabled flags differ from the selected default. "
                f"Expected {sorted(expected_audio_defaults)}; "
                f"found {sorted(actual_audio_enabled)}."
            )
    return None


def _ordinal_summary(indices: set[int]) -> str:
    return "none" if not indices else ", ".join(str(index + 1) for index in sorted(indices))


def _verified_defaults_summary(job: PreparedJob, output_path: Path) -> str:
    audio_defaults, subtitle_defaults = expected_default_indices(job)
    summary = (
        f"verified default audio track(s): {_ordinal_summary(audio_defaults)}; "
        f"default subtitle track(s): {_ordinal_summary(subtitle_defaults)}"
    )
    if is_iso_bmff_path(output_path):
        summary += "; ISO-BMFF audio tkhd Enabled flags match"
    return summary


class BatchProcessor:
    def __init__(self, toolchain: FFmpegToolchain) -> None:
        self.toolchain = toolchain
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def _fresh_job(self, job: PreparedJob) -> PreparedJob:
        if not _fingerprint_matches(job.source):
            raise MediaProbeError(
                f"Source changed after track selection: {job.source.path}. Re-add or re-probe it."
            )
        if not _fingerprint_matches(job.destination):
            raise MediaProbeError(
                "Destination changed after it was added: "
                f"{job.destination.path}. Re-add or re-probe it."
            )
        source = probe_media(self.toolchain.ffprobe, job.source.path)
        destination = probe_media(self.toolchain.ffprobe, job.destination.path)
        return job.with_fresh_media(source, destination)

    def run_job(
        self,
        job: PreparedJob,
        progress: ProgressCallback,
        log: LogCallback,
    ) -> JobResult:
        if self.is_cancelled:
            return JobResult(job.row_number, "cancelled", None, "Cancelled before row started.")
        try:
            fresh_job = self._fresh_job(job)
        except (MediaProbeError, OSError) as exc:
            return JobResult(job.row_number, "failed", None, str(exc))

        primary = fresh_job.output.primary
        if primary.exists() and not fresh_job.replace_primary:
            return JobResult(
                job.row_number,
                "skipped",
                None,
                f"Skipped because output exists and replacement was not approved: {primary}",
            )
        if fresh_job.output.compatibility_reason is not None:
            log(
                f"Row {job.row_number}: {fresh_job.output.compatibility_reason}. "
                "All streams remain direct copies; no re-encoding is used."
            )
        first = self._safe_attempt(
            fresh_job,
            primary,
            allow_replace=fresh_job.replace_primary,
            progress=progress,
            log=log,
        )
        if first.cancelled:
            return JobResult(
                job.row_number,
                "cancelled",
                None,
                first.message,
                recovery_path=first.recovery_path,
            )
        if first.succeeded:
            if fresh_job.output.compatibility_reason is not None:
                return JobResult(
                    job.row_number,
                    "fallback",
                    primary,
                    f"Saved as MKV for reliable default-track playback: {primary.name}",
                )
            return JobResult(job.row_number, "success", primary, first.message)
        if first.recovery_path is not None:
            return JobResult(
                job.row_number,
                "failed",
                None,
                first.message,
                recovery_path=first.recovery_path,
            )

        fallback = fresh_job.output.fallback
        if fallback is None:
            return JobResult(job.row_number, "failed", None, first.message)
        if fallback.exists() and not fresh_job.replace_fallback:
            return JobResult(
                job.row_number,
                "failed",
                None,
                f"Same-container attempt failed: {first.message}\n"
                f"MKV fallback was not allowed to replace existing file: {fallback}",
            )
        log(f"Row {job.row_number}: same-container failure: {first.message}")
        log(f"Row {job.row_number}: retrying direct copy as MKV.")
        second = self._safe_attempt(
            fresh_job,
            fallback,
            allow_replace=fresh_job.replace_fallback,
            progress=progress,
            log=log,
        )
        if second.cancelled:
            return JobResult(
                job.row_number,
                "cancelled",
                None,
                second.message,
                recovery_path=second.recovery_path,
            )
        if second.succeeded:
            return JobResult(
                job.row_number,
                "fallback",
                fallback,
                f"Saved as MKV after the destination container rejected direct copy: {fallback.name}",
            )
        return JobResult(
            job.row_number,
            "failed",
            None,
            f"Same-container attempt failed: {first.message}\nMKV fallback failed: {second.message}",
            recovery_path=second.recovery_path,
        )

    def _safe_attempt(
        self,
        job: PreparedJob,
        final_path: Path,
        *,
        allow_replace: bool,
        progress: ProgressCallback,
        log: LogCallback,
    ) -> AttemptResult:
        temp_path = _temporary_path(final_path)
        try:
            return self._run_attempt(
                job,
                final_path,
                temp_path,
                allow_replace=allow_replace,
                progress=progress,
                log=log,
            )
        except Exception as exc:  # Final worker boundary: keep the batch/UI recoverable.
            message = f"Unexpected processing error ({type(exc).__name__}): {exc}"
            removed = _safe_unlink(temp_path)
            recovery_path = None
            if not removed:
                message += f". Temporary output could not be removed: {temp_path}"
                recovery_path = temp_path
            log(f"Row {job.row_number}: {message}")
            return AttemptResult(False, False, message, recovery_path=recovery_path)

    def _run_attempt(
        self,
        job: PreparedJob,
        final_path: Path,
        temp_path: Path,
        *,
        allow_replace: bool,
        progress: ProgressCallback,
        log: LogCallback,
    ) -> AttemptResult:
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return AttemptResult(False, False, f"Could not create output folder: {exc}")
        command = build_ffmpeg_command(self.toolchain.ffmpeg, job, temp_path)
        log(f"Row {job.row_number}: {subprocess.list2cmdline(command)}")
        stderr_tail: deque[str] = deque(maxlen=80)
        duration = _duration_for_progress(job)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=no_window_creation_flags(),
            )
        except OSError as exc:
            removed = _safe_unlink(temp_path)
            message = f"Could not start FFmpeg: {exc}"
            if not removed:
                message += f". Temporary output could not be removed: {temp_path}"
            return AttemptResult(
                False,
                False,
                message,
                recovery_path=None if removed else temp_path,
            )
        assert process.stdout is not None
        assert process.stderr is not None
        messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threads = [
            threading.Thread(
                target=_read_pipe,
                args=(process.stdout, "stdout", messages),
                daemon=True,
            ),
            threading.Thread(
                target=_read_pipe,
                args=(process.stderr, "stderr", messages),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        closed_channels: set[str] = set()
        try:
            while process.poll() is None or len(closed_channels) < 2:
                if self.is_cancelled and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    continue
                try:
                    channel, line = messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    closed_channels.add(channel)
                    continue
                if channel == "stderr":
                    stderr_tail.append(line)
                    if line.strip():
                        log(f"FFmpeg: {line}")
                elif line.startswith("out_time_us=") and duration:
                    try:
                        seconds = int(line.partition("=")[2]) / 1_000_000
                    except ValueError:
                        continue
                    progress(max(0.0, min(0.99, seconds / duration)))
        finally:
            for thread in threads:
                thread.join(timeout=1)
            process.stdout.close()
            process.stderr.close()

        if self.is_cancelled:
            removed = _safe_unlink(temp_path)
            message = (
                "Cancelled; incomplete temporary output was removed."
                if removed
                else f"Cancelled; incomplete temporary output could not be removed: {temp_path}"
            )
            return AttemptResult(
                False,
                True,
                message,
                recovery_path=None if removed else temp_path,
            )
        if process.returncode != 0:
            removed = _safe_unlink(temp_path)
            diagnostic = "\n".join(stderr_tail).strip()
            if len(diagnostic) > 5000:
                diagnostic = diagnostic[-5000:]
            message = diagnostic or f"FFmpeg exited with code {process.returncode}."
            if not removed:
                message += f"\nTemporary output could not be removed: {temp_path}"
            return AttemptResult(
                False,
                False,
                message,
                recovery_path=None if removed else temp_path,
            )
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            removed = _safe_unlink(temp_path)
            message = "FFmpeg reported success but produced no output file."
            if not removed:
                message += f" Temporary output could not be removed: {temp_path}"
            return AttemptResult(
                False,
                False,
                message,
                recovery_path=None if removed else temp_path,
            )

        validation_error = _validate_output(self.toolchain.ffprobe, job, temp_path)
        if validation_error:
            removed = _safe_unlink(temp_path)
            if not removed:
                validation_error += f" Temporary output could not be removed: {temp_path}"
            return AttemptResult(
                False,
                False,
                validation_error,
                recovery_path=None if removed else temp_path,
            )
        log(f"Row {job.row_number}: {_verified_defaults_summary(job, temp_path)}.")
        progress(1.0)

        if final_path.exists() and not allow_replace:
            removed = _safe_unlink(temp_path)
            message = f"Output appeared during processing and was not replaced: {final_path}"
            if not removed:
                message += f". Temporary output could not be removed: {temp_path}"
            return AttemptResult(
                False,
                False,
                message,
                recovery_path=None if removed else temp_path,
            )
        try:
            os.replace(temp_path, final_path)
        except OSError as exc:
            return AttemptResult(
                False,
                False,
                f"Direct copy completed, but Windows could not install the output at "
                f"{final_path}: {exc}. The completed recovery file is {temp_path}",
                recovery_path=temp_path,
            )
        return AttemptResult(True, False, f"Saved {final_path}")

    def run_batch(
        self,
        jobs: list[PreparedJob],
        row_started: Callable[[int], None],
        row_progress: Callable[[int, float], None],
        row_finished: Callable[[JobResult], None],
        log: LogCallback,
    ) -> list[JobResult]:
        results: list[JobResult] = []
        for job in jobs:
            if self.is_cancelled:
                result = JobResult(
                    job.row_number,
                    "cancelled",
                    None,
                    "Cancelled before row started.",
                )
                results.append(result)
                row_finished(result)
                continue
            row_started(job.row_number)

            def report_progress(value: float, *, row: int = job.row_number) -> None:
                row_progress(row, value)

            result = self.run_job(
                job,
                progress=report_progress,
                log=log,
            )
            results.append(result)
            row_finished(result)
            if result.status == "cancelled":
                self._cancel.set()
        return results
