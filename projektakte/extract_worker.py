"""Reading untrusted files in a worker process with operating-system limits.

PDF, Word and mail files are parsed with pypdf, Pillow (libjpeg, OpenJPEG), jbig2dec, python-docx and the
email package – also mail attachments, which arrive automatically. The bounds inside the extraction
(extract.py) keep crafted files cheap where they are known, but a file that finds a hole in them must
neither exhaust the memory of the app nor keep the indexer busy for hours. So while an indexing pass runs
with isolation switched on (``EXTRACT_ISOLATION``, on unless ``APP_ENV=test``; see :func:`isolation`),
:func:`documents.prepare_document` hands these files to one persistent worker process:

* started with the ``spawn`` method – it inherits none of the app's memory – on first use, reused for the
  following files and started again after it ended;
* its address space is limited (``RLIMIT_AS``, ``EXTRACT_MEMORY_MB``): an allocation beyond fails with
  MemoryError, which the extraction reports as "zu groß zum Lesen", without retry;
* its CPU time per file is limited (``RLIMIT_CPU``, raised before each file to the time used so far plus
  ``EXTRACT_CPU_SECONDS``) – CPU time, not wall time, so waiting for a slow vision model does not count.
  Beyond it the kernel ends the worker (SIGXCPU); the file is recorded as too costly, without retry (the
  file will not change), and the next file gets a new worker. A worker killed otherwise (SIGKILL from the
  kernel's out-of-memory killer, a crash in a decoder) is handled the same way;
* a wall-clock backstop (``EXTRACT_WALL_SECONDS``) ends a worker that hangs without using CPU; every
  answer of the vision model starts it again, so a long scan read page by page is not cut off. Running
  out of it is not deterministic (a slow model): the file is retried later with the usual back-off.

One file at a time: indexing runs in one background thread, and calls from other threads wait. The
vision model is rebuilt in the worker from the configuration of the app's LLMClient; with another model
object (test stubs) or isolation switched off, files are extracted in the app process as before.

The worker answers in JSON (results, log records, progress), never with pickled objects: the app does not
unpickle anything from the process that parses untrusted files.
"""
from __future__ import annotations

import atexit
import json
import logging
import math
import multiprocessing
import os
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .ai.http import seconds_text
from .extract import STRUCTURED_EXT, Extracted, Page

log = logging.getLogger("projektakte.extract_worker")

_START_SECONDS = 60.0  # a worker that is not ready by then (imports, limits) does not start
_STOP_SECONDS = 3.0  # a worker asked to stop that has not ended by then is killed
_SLICE_SECONDS = 0.5  # waiting for the worker is done in slices: a close() from another thread is noticed
_MAX_LOG_RECORDS = 200  # log records forwarded per file: a crafted file must not flood the app's log
_STOP = b'{"stop": true}'


@dataclass(frozen=True)
class Limits:
    """Operating-system limits of the worker process (see the module docstring)."""
    memory_mb: int = 2048
    cpu_seconds: float = 120.0
    wall_seconds: float = 3600.0

    @classmethod
    def of(cls, settings) -> "Limits":
        return cls(int(settings.extract_memory_mb), float(settings.extract_cpu_seconds),
                   float(settings.extract_wall_seconds))

    def cpu_text(self) -> str:
        return f"Datei zu aufwendig zum Lesen (über {seconds_text(self.cpu_seconds)} s Rechenzeit)"

    def memory_text(self) -> str:
        return f"{self.memory_mb:,} MB".replace(",", ".")

    def killed_text(self) -> str:
        return ("Datei zu groß zum Lesen (Leseprozess vom Betriebssystem beendet, vermutlich wegen der "
                f"Speichergrenze; erlaubt sind {self.memory_text()})")

    def wall_text(self) -> str:
        return (f"Datei nicht fertig gelesen (über {seconds_text(self.wall_seconds)} s ohne Fortschritt) – "
                "neuer Versuch später")


class ExtractionAborted(BaseException):
    """The worker was closed (app shutdown) while it read a file: nothing may be recorded for the file –
    it is read again by the next indexing pass. A cancellation like KeyboardInterrupt, not an error: the
    per-file and per-mail error handlers (``except Exception``) must not record it as a failure of the
    file; it ends the indexing pass."""


class ExtractWorkerError(RuntimeError):
    """The worker process cannot be started (e.g. EXTRACT_MEMORY_MB too small to load its libraries)."""


def failed(message: str, *, retry: bool) -> Extracted:
    """The result for a file the worker could not read: deterministic limits are not retried (the file
    will not change), anything else is retried with the usual back-off."""
    return Extracted([], status="error" if retry else "skipped", message=message, incomplete=True, retry=retry)


def portable(llm) -> bool:
    """Can the worker use this model? An LLMClient is rebuilt there from its configuration; no model
    needs nothing. Other objects (test stubs, subclasses with their own behaviour) cannot be."""
    from .ai.llm import LLMClient

    return llm is None or type(llm) is LLMClient


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"Signal {number}"


# -- the app's side ---------------------------------------------------------------------------------------
class ExtractWorker:
    """One worker process at a time, started on first use and again after it ended. Thread-safe: one
    file at a time."""

    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits()
        self._lock = threading.Lock()
        self._proc = None
        self._conn = None
        self._closed = False
        self.started = 0  # worker processes started (for tests and the log)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pid(self) -> int | None:
        """Process id of the running worker, None if none runs."""
        proc = self._proc
        try:
            return proc.pid if proc is not None and proc.is_alive() else None
        except ValueError:  # closed meanwhile by another thread
            return None

    def extract(self, filename: str, data: bytes, llm=None) -> Extracted:
        """Extract one file (``filename`` gives the format) in the worker. ``llm`` must be
        :func:`portable`. Raises ExtractionAborted if the worker is closed meanwhile, ExtractWorkerError
        if it cannot be started."""
        request = json.dumps({
            "filename": filename,
            "llm": llm.config() if llm is not None else None,
            "level": logging.getLogger().getEffectiveLevel(),
        }).encode()
        with self._lock:
            for attempt in (1, 2):
                if self._closed:
                    raise ExtractionAborted("Leseprozess wurde beendet")
                conn = self._running()
                try:
                    conn.send_bytes(request)
                    conn.send_bytes(data)
                    break
                except (OSError, EOFError, ValueError):  # ended while idle (e.g. killed): a new one
                    self._discard()
                    if attempt == 2:
                        raise ExtractWorkerError("Leseprozess nimmt keine Datei an") from None
            return self._result(filename, llm)

    def close(self) -> None:
        """Stop the worker (app shutdown). A file being read is abandoned: the reading thread gets
        ExtractionAborted. Later calls of :meth:`extract` raise ExtractionAborted."""
        self._closed = True
        if not self._lock.acquire(blocking=False):  # a file is being read: end the worker
            proc = self._proc
            if proc is not None:
                try:
                    proc.kill()
                except (OSError, ValueError, AttributeError):
                    pass
            if not self._lock.acquire(timeout=10):
                return  # the reading thread cleans up
        try:
            self._discard(graceful=True)
        finally:
            self._lock.release()

    # -- inside the lock --
    def _running(self):
        proc = self._proc
        if proc is not None and proc.is_alive():
            return self._conn
        self._discard()
        ctx = multiprocessing.get_context("spawn")
        conn, child = ctx.Pipe(duplex=True)
        proc = ctx.Process(target=_serve, args=(child, self.limits), name="projektakte-extract", daemon=True)
        try:
            proc.start()
        except OSError as exc:  # no process can be started (process or file limits of the system)
            conn.close()
            raise ExtractWorkerError(f"Leseprozess für Dateien startet nicht ({exc})") from exc
        finally:
            child.close()  # else the app would not notice when the worker ends
        self._proc, self._conn = proc, conn
        ready, deadline = False, time.monotonic() + _START_SECONDS
        try:
            while not ready and not self._closed and time.monotonic() < deadline:
                if not conn.poll(_SLICE_SECONDS):
                    continue
                message = json.loads(conn.recv_bytes())
                ready = bool(message.get("ready"))
                if message.get("kind") == "log":  # e.g. a warning while the libraries load
                    logging.getLogger(str(message["name"])).log(int(message["level"]), "%s", message["text"])
        except (OSError, EOFError, ValueError):
            ready = False
        if self._closed:
            self._discard()
            raise ExtractionAborted("Leseprozess wurde beendet")
        if not ready:
            proc.join(1)
            code = proc.exitcode
            self._discard()
            raise ExtractWorkerError(
                f"Leseprozess für Dateien startet nicht (Exitcode {code}) – EXTRACT_MEMORY_MB zu klein?")
        self.started += 1
        if self.started == 1:
            log.info("Dateien werden in einem eigenen Prozess gelesen (Grenzen je Datei: %s Speicher, %s s "
                     "Rechenzeit, %s s ohne Fortschritt)", self.limits.memory_text(),
                     seconds_text(self.limits.cpu_seconds), seconds_text(self.limits.wall_seconds))
        return conn

    def _result(self, filename: str, llm) -> Extracted:
        conn, wall = self._conn, self.limits.wall_seconds
        deadline = time.monotonic() + wall
        while True:
            left = deadline - time.monotonic()
            try:
                ready = left > 0 and conn.poll(min(left, _SLICE_SECONDS))
            except (OSError, EOFError, ValueError):
                ready = True  # recv below notices that the worker ended
            if not ready:
                if self._closed:
                    self._discard()
                    raise ExtractionAborted("Leseprozess wurde beendet")
                if left > _SLICE_SECONDS:
                    continue
                self._discard()
                log.warning("%s: %s", filename, self.limits.wall_text())
                return failed(self.limits.wall_text(), retry=True)
            try:
                message = json.loads(conn.recv_bytes())
            except (OSError, EOFError, ValueError):
                return self._ended(filename)
            kind = message.get("kind")
            if kind == "progress":  # an answer of the vision model: the work goes on
                deadline = time.monotonic() + wall
            elif kind == "log":
                logging.getLogger(str(message["name"])).log(int(message["level"]), "%s", message["text"])
            elif kind == "result":
                if message.get("llm_error") and llm is not None:
                    llm._note_error(str(message["llm_error"]))  # the status page shows the model's last error
                if message.get("recycle"):  # the worker ends itself after a file that took much memory
                    self._discard(graceful=True)
                return _extracted(message["result"])

    def _ended(self, filename: str) -> Extracted:
        """The worker ended while reading a file: why, as the file's result."""
        proc = self._proc
        if proc is not None:
            proc.join(5)
        code = proc.exitcode if proc is not None else None
        self._discard()
        if self._closed:
            raise ExtractionAborted("Leseprozess wurde beendet")
        if code == -signal.SIGXCPU:
            message, retry = self.limits.cpu_text(), False
        elif code == -signal.SIGKILL:
            message, retry = self.limits.killed_text(), False
        elif code is not None and code < 0:  # a crash in a decoder: the same file crashes it again
            message, retry = f"Datei nicht lesbar (Leseprozess abgestürzt: {_signal_name(-code)})", False
        else:
            message, retry = f"Datei nicht lesbar (Leseprozess unerwartet beendet, Exitcode {code})", True
        log.warning("%s: %s", filename, message)
        return failed(message, retry=retry)

    def _discard(self, *, graceful: bool = False) -> None:
        proc, conn = self._proc, self._conn
        self._proc = self._conn = None
        if proc is None:
            return
        if graceful and proc.is_alive():
            try:
                conn.send_bytes(_STOP)
            except (OSError, ValueError):
                pass
            proc.join(_STOP_SECONDS)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        conn.close()
        try:
            proc.close()  # releases the process object's resources
        except ValueError:  # still running (not reaped in time): left to the operating system
            pass


def _extracted(data: dict) -> Extracted:
    return Extracted([Page(number, text) for number, text in data["pages"]], status=data["status"],
                     message=data["message"], method=data["method"], incomplete=bool(data["incomplete"]),
                     retry=bool(data["retry"]))


# -- the worker's side ------------------------------------------------------------------------------------
def _serve(conn, limits: Limits) -> None:  # pragma: no cover - runs in the worker process
    """Main function of the worker process: read files until the app says stop or goes away."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C in a terminal reaches the whole process group
    _set_soft_limit("RLIMIT_CORE", 0)  # no core file when the CPU limit ends the worker
    _set_soft_limit("RLIMIT_AS", limits.memory_mb * 1024 * 1024)
    forward = _Forward(conn)
    root = logging.getLogger()
    root.handlers[:] = [forward]
    logging.captureWarnings(True)  # Pillow's and pypdf's warnings go to the app's log as well
    from . import extract
    from .ai.llm import LLMClient

    extract.MEMORY_LIMIT_MB = limits.memory_mb
    _send(conn, {"ready": True})
    while True:
        try:
            request = json.loads(conn.recv_bytes())
            if request.get("stop"):
                return
            data = conn.recv_bytes()
        except (OSError, EOFError):
            return  # the app has gone
        root.setLevel(int(request["level"]))
        forward.left = _MAX_LOG_RECORDS
        _set_soft_limit("RLIMIT_CPU", math.ceil(_cpu_used() + limits.cpu_seconds))
        llm = _Reporting(LLMClient.from_config(request["llm"]), conn) if request["llm"] else None
        result = extract.extract(Path(request["filename"]), data, llm=llm)
        del data
        # memory a large file took is not always given back to the system: a new worker for the next file
        recycle = _resident_mb() > limits.memory_mb / 4
        _send(conn, {"kind": "result", "recycle": recycle, "llm_error": llm.error if llm else None,
                     "result": {"pages": [[p.number, p.text] for p in result.pages], "status": result.status,
                                "message": result.message, "method": result.method,
                                "incomplete": result.incomplete, "retry": result.retry}})
        if recycle:
            return


def _send(conn, message: dict) -> None:
    conn.send_bytes(json.dumps(message).encode())


def _set_soft_limit(name: str, value: int) -> None:
    """Lower or raise a soft limit, never above the hard limit (unprivileged processes cannot raise it)."""
    try:
        import resource

        which = getattr(resource, name)
        _soft, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(which, (value, hard))
    except (ImportError, AttributeError, ValueError, OSError) as exc:
        log.warning("Grenze %s für den Leseprozess nicht setzbar (%s)", name, exc)


def _cpu_used() -> float:
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _resident_mb() -> float:
    """Resident memory of this process now (Linux), 0 if unknown."""
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except (OSError, ValueError, IndexError):
        return 0.0


class _Forward(logging.Handler):
    """Hands the worker's log records to the app, which logs them as its own (same format and place)."""

    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.left = _MAX_LOG_RECORDS

    def emit(self, record: logging.LogRecord) -> None:
        if self.left <= 0:
            return
        self.left -= 1
        try:
            text = record.getMessage()
            if self.left == 0:
                text += " (weitere Meldungen zu dieser Datei unterdrückt)"
            _send(self.conn, {"kind": "log", "level": record.levelno, "name": record.name, "text": text})
        except Exception:  # noqa: BLE001 – logging must never break the extraction
            self.handleError(record)


class _Reporting:
    """The vision model in the worker: tells the app after every answer that the work goes on (the
    wall-clock backstop starts again) and keeps the last error for the app's status page."""

    def __init__(self, llm, conn):
        self._llm, self._conn = llm, conn
        self.vision, self.model = llm.vision, llm.model
        self.error: str | None = None

    def describe_image(self, image: bytes, mime: str, prompt: str):
        try:
            return self._llm.describe_image(image, mime, prompt)
        except Exception as exc:
            self.error = str(exc)
            raise
        finally:
            _send(self._conn, {"kind": "progress"})


# -- the worker of the app --------------------------------------------------------------------------------
_lock = threading.Lock()
_worker: ExtractWorker | None = None  # kept between indexing passes
_active: ExtractWorker | None = None  # the worker of the running pass, None: extract in-process


# The limits need POSIX resource limits and signals (Linux, the Docker image; also macOS, where the address
# space limit may not be enforced).
SUPPORTED = os.name == "posix" and hasattr(signal, "SIGXCPU")
_unsupported_warned = False


@contextmanager
def isolation(settings):
    """Extract in the worker process during the block (an indexing pass) if the settings switch isolation
    on (``extract_isolation``). The worker process outlives the block: the next pass reuses it. Yields the
    worker, or None."""
    global _worker, _active, _unsupported_warned
    worker = old = None
    wanted = bool(getattr(settings, "extract_isolation", False))
    if wanted and not SUPPORTED and not _unsupported_warned:
        _unsupported_warned = True
        log.warning("EXTRACT_ISOLATION: auf diesem System ohne POSIX-Ressourcengrenzen nicht möglich – Dateien "
                    "werden im Prozess der App gelesen")
    if wanted and SUPPORTED:
        limits = Limits.of(settings)
        with _lock:
            if _worker is None or _worker.closed or _worker.limits != limits:
                old, _worker = _worker, ExtractWorker(limits)
            worker = _worker
    if old is not None:
        old.close()
    with _lock:
        previous, _active = _active, worker
    try:
        yield worker
    finally:
        with _lock:
            _active = previous


def current() -> ExtractWorker | None:
    """The worker the running indexing pass extracts with, None outside a pass with isolation."""
    return _active


def extract_file(filename: str, data: bytes, llm, extract):
    """Extract one file: in the worker of the running pass if there is one and it can take the file
    (PDF, Word, mail – formats whose structure is parsed – and a model it can rebuild), else in this
    process with ``extract`` (the caller's, so tests can replace it)."""
    worker = _active
    if worker is not None and Path(filename).suffix.lower() in STRUCTURED_EXT and portable(llm):
        return worker.extract(filename, data, llm)
    return extract(Path(filename), data, llm=llm)


def shutdown() -> None:
    """Stop the worker process (app shutdown, process exit). A pass still running gets ExtractionAborted
    for its next file; the next pass starts a new worker."""
    with _lock:
        worker = _worker
    if worker is not None:
        worker.close()


atexit.register(shutdown)
