from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence

_CHUNK_SIZE = 64 * 1024
_DEFAULT_MAX_OUTPUT = 64 * 1024 * 1024
_DEFAULT_TAIL = 64 * 1024


@dataclass(frozen=True)
class StreamCapture:
    path: Path
    bytes_seen: int
    bytes_stored: int
    truncated: bool
    tail: str


@dataclass(frozen=True)
class ProcessResult:
    command: list[str]
    returncode: int
    stdout: StreamCapture
    stderr: StreamCapture
    error_kind: str | None
    timed_out: bool
    process_tree_terminated: bool
    started_monotonic: float
    finished_monotonic: float

    @property
    def duration_seconds(self) -> float:
        return self.finished_monotonic - self.started_monotonic


class _Drain:
    def __init__(self, path: Path, max_bytes: int, tail_bytes: int):
        self.path = path
        self.max_bytes = max_bytes
        self.tail_bytes = tail_bytes
        self.bytes_seen = 0
        self.bytes_stored = 0
        self.truncated = False
        self._tail = bytearray()
        self._error: BaseException | None = None

    def run(self, stream: BinaryIO) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("wb") as output:
                while True:
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.bytes_seen += len(chunk)
                    if self.tail_bytes:
                        self._tail.extend(chunk)
                        if len(self._tail) > self.tail_bytes:
                            del self._tail[: len(self._tail) - self.tail_bytes]
                    remaining = self.max_bytes - self.bytes_stored
                    if remaining > 0:
                        written = chunk[:remaining]
                        output.write(written)
                        self.bytes_stored += len(written)
                    if len(chunk) > max(remaining, 0):
                        self.truncated = True
                if self.truncated:
                    output.write(
                        b"\n[re-harness output truncated after configured byte limit]\n"
                    )
        except BaseException as exc:  # pragma: no cover - surfaced by finish
            self._error = exc
        finally:
            stream.close()

    def finish(self) -> StreamCapture:
        if self._error is not None:
            raise self._error
        return StreamCapture(
            path=self.path,
            bytes_seen=self.bytes_seen,
            bytes_stored=self.bytes_stored,
            truncated=self.truncated,
            tail=bytes(self._tail).decode("utf-8", errors="replace"),
        )


def _terminate_process_tree(process: subprocess.Popen[bytes], grace_seconds: float) -> bool:
    if process.poll() is not None:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
        return True

    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return False
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    return True


def run_streamed(
    command: Sequence[str],
    *,
    cwd: Path,
    output_dir: Path,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT,
    tail_bytes: int = _DEFAULT_TAIL,
    termination_grace_seconds: float = 2.0,
) -> ProcessResult:
    """Run a command with bounded disk streaming and process-tree timeout cleanup."""
    if not command:
        raise ValueError("Command cannot be empty.")
    if max_output_bytes < 0 or tail_bytes < 0:
        raise ValueError("Output limits cannot be negative.")
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_drain = _Drain(output_dir / "stdout.log", max_output_bytes, tail_bytes)
    stderr_drain = _Drain(output_dir / "stderr.log", max_output_bytes, tail_bytes)
    started = time.monotonic()
    creationflags = 0
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except OSError as exc:
        stdout_drain.path.write_bytes(b"")
        stderr_drain.path.write_text(str(exc), encoding="utf-8")
        finished = time.monotonic()
        empty = StreamCapture(stdout_drain.path, 0, 0, False, "")
        error_text = str(exc)
        error_capture = StreamCapture(
            stderr_drain.path,
            len(error_text.encode()),
            len(error_text.encode()),
            False,
            error_text,
        )
        return ProcessResult(
            command=list(command),
            returncode=127,
            stdout=empty,
            stderr=error_capture,
            error_kind="execution_error",
            timed_out=False,
            process_tree_terminated=False,
            started_monotonic=started,
            finished_monotonic=finished,
        )

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(target=stdout_drain.run, args=(process.stdout,), daemon=True)
    stderr_thread = threading.Thread(target=stderr_drain.run, args=(process.stderr,), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    terminated = False
    error_kind = None
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        error_kind = "timeout"
        terminated = _terminate_process_tree(process, termination_grace_seconds)
    finally:
        stdout_thread.join()
        stderr_thread.join()
    finished = time.monotonic()
    return ProcessResult(
        command=list(command),
        returncode=124 if timed_out else int(process.returncode),
        stdout=stdout_drain.finish(),
        stderr=stderr_drain.finish(),
        error_kind=error_kind,
        timed_out=timed_out,
        process_tree_terminated=terminated,
        started_monotonic=started,
        finished_monotonic=finished,
    )
