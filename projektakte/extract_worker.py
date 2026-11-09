"""Reading untrusted files in a worker process with operating-system limits.

PDF, Word, OpenDocument and mail files are parsed with pypdf, Pillow (libjpeg, OpenJPEG), jbig2dec,
python-docx, zipfile/expat and the email package – also mail attachments, which arrive automatically. The
bounds inside the extraction (extract.py) keep crafted files cheap where they are known, but a file that
finds a hole in them must neither exhaust the memory of the app nor keep the indexer busy for hours. So
while an indexing pass runs with isolation switched on (``EXTRACT_ISOLATION``, on unless ``APP_ENV=test``;
see :func:`isolation`), :func:`documents.prepare_document` hands these files to one persistent worker process:

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
  out of it is not deterministic (a slow model): the file is retried later with the usual back-off;
* a result cut short by the memory or the CPU limit names that limit (``Extracted.limit``, stored with the
  document's profile): the file is read again once the limit is raised (``documents.reprocess_due``) –
  raising another limit, or lowering one, reads nothing again;
* it ends when the app ends, also when the app is killed (``kill -9``) and cannot stop it: on Linux the
  kernel signals it when its parent dies (``PR_SET_PDEATHSIG``); elsewhere, and as a fallback, it checks
  that the app is still its parent before every page and every call of the vision model and ends when
  the app no longer reads its messages – an orphaned worker must not keep calling the model;
* if it cannot be started (process limits of the system, too little memory for its libraries, a start
  timeout), the error is logged once per indexing pass and the pass goes on without it: PDF, Word,
  OpenDocument and mail files are recorded as not read and retried with the usual back-off, everything else (text files,
  images, mails, embeddings, assignment) is processed. They are deliberately not read in the app process
  instead: a worker that does not start usually means the system is short of processes or memory, and
  reading untrusted files without the limits is what the isolation exists to prevent.

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
import sys
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

    def memory_limit(self) -> str:
        """``Extracted.limit`` of a result cut short by the memory limit."""
        return f"memory={self.memory_mb}"

    def cpu_limit(self) -> str:
        """``Extracted.limit`` of a result cut short by the CPU limit."""
        value = float(self.cpu_seconds)
        short = f"{value:g}"  # "120", "1.5" – but exact where "%g" would round (100.0001 re-read every pass)
        return f"cpu={short if float(short) == value else repr(value)}"

    def lifts(self, limit: str) -> bool:
        """Are these limits higher than the one that cut a result short (``Extracted.limit``, e.g.
        "memory=256")? Then the file may be read now. Only that limit counts: another one raised, or this
        one lowered, reads nothing again. Unknown: yes (read once more)."""
        kind, _, value = limit.partition("=")
        try:
            before = float(value)
        except ValueError:
            return True
        if kind == "memory":
            return self.memory_mb > before
        if kind == "cpu":
            return self.cpu_seconds > before
        return True


class ExtractionAborted(BaseException):
    """The worker was closed (app shutdown) while it read a file: nothing may be recorded for the file –
    it is read again by the next indexing pass. A cancellation like KeyboardInterrupt, not an error: the
    per-file and per-mail error handlers (``except Exception``) must not record it as a failure of the
    file; it ends the indexing pass."""


class ExtractWorkerError(RuntimeError):
    """The worker process cannot be started (e.g. EXTRACT_MEMORY_MB too small to load its libraries, no
    process can be started). :meth:`ExtractWorker.extract` turns it into a result to be retried."""


NOT_STARTED_TEXT = "Datei nicht gelesen (Leseprozess für Dateien startet nicht) – neuer Versuch später"


def failed(message: str, *, retry: bool, limit: str | None = None) -> Extracted:
    """The result for a file the worker could not read: deterministic limits are not retried (the file
    will not change; ``limit`` names the limit, so that raising it reads the file again), anything else is
    retried with the usual back-off."""
    return Extracted([], status="error" if retry else "skipped", message=message, incomplete=True, retry=retry,
                     limit=limit)


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
    file at a time. If it cannot be started, files are not read until the next pass (:meth:`new_pass`)."""

    def __init__(self, limits: Limits | None = None):
        self.limits = limits or Limits()
        self._lock = threading.Lock()
        self._proc = None
        self._conn = None
        self._closed = False
        self._failure: str | None = None  # why the worker could not be started in this pass
        self.started = 0  # worker processes started (for tests and the log)

    def new_pass(self) -> None:
        """An indexing pass begins: a worker that could not be started is tried again (once)."""
        self._failure = None

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
        :func:`portable`. Raises ExtractionAborted if the worker is closed meanwhile. If the worker cannot
        be started, the error is logged once and this and every further file until the next pass are
        recorded as not read, to be retried later (see the module docstring)."""
        request = json.dumps({
            "filename": filename,
            "llm": llm.config() if llm is not None else None,
            "level": logging.getLogger().getEffectiveLevel(),
        }).encode()
        with self._lock:
            if self._closed:
                raise ExtractionAborted("Leseprozess wurde beendet")
            if self._failure is None:
                try:
                    self._hand_over(request, data)
                except ExtractWorkerError as exc:
                    self._failure = str(exc)
                    log.error("%s – PDF-, Word-, OpenDocument- und Mail-Dateien werden in diesem Durchlauf nicht "
                              "gelesen und später erneut versucht", exc)
                else:
                    return self._result(filename, llm)
            return failed(NOT_STARTED_TEXT, retry=True)

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
    def _hand_over(self, request: bytes, data: bytes) -> None:
        for _attempt in (1, 2):
            if self._closed:
                raise ExtractionAborted("Leseprozess wurde beendet")
            conn = self._running()
            try:
                conn.send_bytes(request)
                conn.send_bytes(data)
                return
            except (OSError, EOFError, ValueError):  # ended while idle (e.g. killed): a new one
                self._discard()
        raise ExtractWorkerError("Leseprozess für Dateien nimmt keine Datei an")

    def _running(self):
        proc = self._proc
        if proc is not None and proc.is_alive():
            return self._conn
        self._discard()
        ctx = multiprocessing.get_context("spawn")
        conn, child = ctx.Pipe(duplex=True)
        proc = ctx.Process(target=_serve, args=(child, self.limits, os.getpid()), name="projektakte-extract",
                           daemon=True)
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
            if code is None:  # still running: not ready in time (an overloaded system)
                raise ExtractWorkerError(f"Leseprozess für Dateien startet nicht (nicht bereit nach "
                                         f"{seconds_text(_START_SECONDS)} s)")
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
        limit = None
        if code == -signal.SIGXCPU:
            message, retry, limit = self.limits.cpu_text(), False, self.limits.cpu_limit()
        elif code == -signal.SIGKILL:  # presumably memory (see killed_text): more of it may read the file
            message, retry, limit = self.limits.killed_text(), False, self.limits.memory_limit()
        elif code is not None and code < 0:  # a crash in a decoder: the same file crashes it again
            message, retry = f"Datei nicht lesbar (Leseprozess abgestürzt: {_signal_name(-code)})", False
        else:
            message, retry = f"Datei nicht lesbar (Leseprozess unerwartet beendet, Exitcode {code})", True
        log.warning("%s: %s", filename, message)
        return failed(message, retry=retry, limit=limit)

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
                     retry=bool(data["retry"]), limit=str(data["limit"]) if data.get("limit") else None)


# -- the worker's side ------------------------------------------------------------------------------------
def _serve(conn, limits: Limits, parent: int) -> None:  # pragma: no cover - runs in the worker process
    """Main function of the worker process: read files until the app (process ``parent``) says stop or
    goes away."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C in a terminal reaches the whole process group
    _set_soft_limit("RLIMIT_CORE", 0)  # no core file when the CPU limit ends the worker
    _set_soft_limit("RLIMIT_AS", limits.memory_mb * 1024 * 1024)
    forward = _Forward(conn)
    root = logging.getLogger()
    root.handlers[:] = [forward]
    logging.captureWarnings(True)  # Pillow's and pypdf's warnings go to the app's log as well
    _end_with_app(parent)
    from . import extract
    from .ai.llm import LLMClient

    extract.MEMORY_LIMIT_MB = limits.memory_mb
    extract.PAGE_CHECK = lambda: _check_app(parent)
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
        llm = _Reporting(LLMClient.from_config(request["llm"]), conn, parent) if request["llm"] else None
        result = extract.extract(Path(request["filename"]), data, llm=llm)
        del data
        # memory a large file took is not always given back to the system: a new worker for the next file
        recycle = _resident_mb() > limits.memory_mb / 4
        _send(conn, {"kind": "result", "recycle": recycle, "llm_error": llm.error if llm else None,
                     "result": {"pages": [[p.number, p.text] for p in result.pages], "status": result.status,
                                "message": result.message, "method": result.method,
                                "incomplete": result.incomplete, "retry": result.retry,
                                "limit": result.limit}})
        if recycle:
            return


def _send(conn, message: dict) -> None:
    """Send a message to the app; if the app no longer reads (it has gone), the worker ends."""
    try:
        conn.send_bytes(json.dumps(message).encode())
    except OSError:  # broken pipe: nobody would read the result of the work
        _leave()


_PR_SET_PDEATHSIG = 1  # prctl option (linux/prctl.h)


def _end_with_app(parent: int) -> None:
    """Make sure this worker ends when the app (process ``parent``) ends, also when the app is killed
    (``kill -9``) and cannot stop it. On Linux the kernel sends SIGUSR1 when the parent dies; the handler
    ends the worker if the app is no longer its parent – the signal also comes when merely the app's
    thread that started the worker ends (the kernel tracks the thread), and then the app lives on.
    Elsewhere, and as a fallback, :func:`_check_app` runs before every page and model call."""
    if sys.platform.startswith("linux"):
        def on_parent_death(_signum, _frame) -> None:
            _check_app(parent)

        try:
            import ctypes

            signal.signal(signal.SIGUSR1, on_parent_death)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGUSR1), 0, 0, 0) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        except (ImportError, AttributeError, OSError, ValueError) as exc:
            log.warning("Leseprozess: Ende mit der App nicht beim Betriebssystem anmeldbar (%s) – geprüft "
                        "wird vor jeder Seite und jedem Modellaufruf", exc)
    _check_app(parent)  # the app may have ended before the signal was set up


def _check_app(parent: int) -> None:
    """End the worker if the app that started it has gone (the worker was handed to another parent)."""
    if os.getppid() != parent:
        _leave()


def _leave() -> None:
    """The app has gone: end the worker at once. Nothing is to be saved, and os._exit cannot be caught by
    an error handler of the extraction that would carry on with the next page."""
    os._exit(0)


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
            message = {"kind": "log", "level": record.levelno, "name": record.name, "text": text}
        except Exception:  # noqa: BLE001 – logging must never break the extraction
            self.handleError(record)
            return
        _send(self.conn, message)  # ends the worker if the app has gone


class _Reporting:
    """The vision model in the worker: tells the app after every answer that the work goes on (the
    wall-clock backstop starts again) and keeps the last error for the app's status page. Calls the model
    only while the app (process ``parent``) is there: an orphaned worker ends instead (a failed call
    would only be counted as a model failure and the next page read)."""

    def __init__(self, llm, conn, parent: int):
        self._llm, self._conn, self._parent = llm, conn, parent
        self.vision, self.model = llm.vision, llm.model
        self.error: str | None = None

    def describe_image(self, image: bytes, mime: str, prompt: str):
        _check_app(self._parent)
        try:
            return self._llm.describe_image(image, mime, prompt)
        except Exception as exc:
            self.error = str(exc)
            raise
        finally:
            _send(self._conn, {"kind": "progress"})  # ends the worker if the app has gone


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
    if worker is not None:
        worker.new_pass()
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


def _takes(worker: ExtractWorker | None, filename: str, llm) -> bool:
    """Does the worker read this file? PDF, Word, OpenDocument, mail – formats whose structure is parsed – with a model
    it can rebuild."""
    return worker is not None and Path(filename).suffix.lower() in STRUCTURED_EXT and portable(llm)


def extract_file(filename: str, data: bytes, llm, extract):
    """Extract one file: in the worker of the running pass if there is one and it takes the file (see
    _takes), else in this process with ``extract`` (the caller's, so tests can replace it)."""
    worker = _active
    if _takes(worker, filename, llm):
        return worker.extract(filename, data, llm)
    return extract(Path(filename), data, llm=llm)


def limits_for(filename: str, llm) -> Limits | None:
    """The operating-system limits the file is read under in the running pass: the worker's if it takes
    the file, None if it is read in this process, without such limits."""
    worker = _active
    return worker.limits if _takes(worker, filename, llm) else None


def shutdown() -> None:
    """Stop the worker process (app shutdown, process exit). A pass still running gets ExtractionAborted
    for its next file; the next pass starts a new worker."""
    with _lock:
        worker = _worker
    if worker is not None:
        worker.close()


atexit.register(shutdown)
