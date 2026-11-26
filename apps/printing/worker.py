import ctypes
import fcntl
import logging
import os
import signal
import stat
import subprocess
import sys
import zlib
from collections import defaultdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, current_thread
from time import monotonic, sleep


LOGGER = logging.getLogger(__name__)
MAX_HTML_BYTES = 512 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
RENDER_TIMEOUT = 30
RENDER_COMMAND = (sys.executable, "-m", "apps.printing.renderer")
DEFAULT_LOCK_PATH = "/var/lib/erp/print/render.lock"
RETRY_AFTER = "5"
PR_SET_CHILD_SUBREAPER = 36
ASSETS = Path(__file__).with_name("assets")
CHROMIUM_EXECUTABLE = "/usr/bin/chromium"
ARABIC_FONT = Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_LOGO_BYTES = 4 * 1024 * 1024
_STOP = Event()
_REQUEST_LOCK = Lock()
_ACTIVE_REQUESTS = set()


class PrintBusy(Exception):
    pass


class RenderFailed(Exception):
    pass


class _StopServing(Exception):
    pass


def _valid_png(logo):
    if not logo.startswith(PNG_SIGNATURE + b"\x00\x00\x00\rIHDR"):
        return False
    offset = len(PNG_SIGNATURE)
    image_data = False
    while offset + 12 <= len(logo):
        size = int.from_bytes(logo[offset:offset + 4], "big")
        end = offset + size + 12
        if end > len(logo) or zlib.crc32(logo[offset + 4:end - 4]) != int.from_bytes(logo[end - 4:end], "big"):
            return False
        kind = logo[offset + 4:offset + 8]
        if kind == b"IEND":
            return image_data and size == 0 and end == len(logo)
        if kind == b"IDAT" and size:
            image_data = True
        offset = end
    return False


def read_logo():
    configured = os.environ.get("ERP_PRINT_LOGO")
    path = Path(configured) if configured is not None else ASSETS / "logo.png"
    try:
        info = path.stat()
    except FileNotFoundError:
        if configured is None:
            return None
        raise
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_LOGO_BYTES:
        raise ValueError("Invalid printing logo")
    with path.open("rb") as stream:
        logo = stream.read(MAX_LOGO_BYTES + 1)
    if len(logo) > MAX_LOGO_BYTES or not _valid_png(logo):
        raise ValueError("Invalid printing logo")
    return logo


def _assets_ready():
    try:
        if not (
            os.access(CHROMIUM_EXECUTABLE, os.X_OK)
            and ARABIC_FONT.is_file()
            and (ASSETS / "bootstrap.rtl.min.css").is_file()
        ):
            return False
        read_logo()
        return True
    except (OSError, ValueError):
        return False


def _adopt_orphaned_children():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@contextmanager
def _print_slot():
    path = os.environ.get("ERP_PRINT_LOCK", DEFAULT_LOCK_PATH)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RenderFailed("Print lock is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PrintBusy from exc
        yield
    finally:
        os.close(fd)


def _descendants():
    by_parent = defaultdict(list)
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / "stat").read_bytes().rsplit(b")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        by_parent[int(fields[1])].append(int(path.name))

    pending = by_parent[os.getpid()]
    while pending:
        pid = pending.pop()
        pending.extend(by_parent[pid])
        yield pid


def _signal_render(group, signum):
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        pass
    # Chromium and its driver can create their own process groups.
    for pid in _descendants():
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _all_reaped(process):
    if process.poll() is None:
        return False
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def _finish_render(process):
    if _all_reaped(process):
        return
    for signum, grace in ((signal.SIGTERM, 0.75), (signal.SIGKILL, 1.5)):
        deadline = monotonic() + grace
        while monotonic() < deadline:
            _signal_render(process.pid, signum)
            if _all_reaped(process):
                return
            sleep(0.02)
    if not _all_reaped(process):
        raise RuntimeError("Printing processes did not exit")


def _read_render(process, markup):
    deadline = monotonic() + RENDER_TIMEOUT
    while not _STOP.is_set():
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RenderFailed("Printing timed out")
        try:
            result, _stderr = process.communicate(markup, timeout=min(0.1, remaining))
            return result
        except subprocess.TimeoutExpired:
            # Popen keeps both partial output and the remaining input across retries.
            markup = None
    raise RenderFailed("Printer is shutting down")


@contextmanager
def _admitted_request():
    request = current_thread()
    with _REQUEST_LOCK:
        if _STOP.is_set():
            raise RenderFailed("Printer is shutting down")
        _ACTIVE_REQUESTS.add(request)
    try:
        yield
    finally:
        with _REQUEST_LOCK:
            _ACTIVE_REQUESTS.remove(request)


def _render(markup):
    with _print_slot(), TemporaryDirectory(prefix="erp-pdf-") as working_directory:
        if _STOP.is_set():
            raise RenderFailed("Printer is shutting down")
        process = subprocess.Popen(
            RENDER_COMMAND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=dict(os.environ, TMPDIR=working_directory),
        )
        started = monotonic()
        LOGGER.info("PDF render started child_pid=%d", process.pid)
        try:
            result = _read_render(process, markup)
        finally:
            try:
                _finish_render(process)
            except Exception:
                LOGGER.critical("PDF process cleanup failed child_pid=%d; restarting", process.pid)
                # Keep the slot until exiting the foreground process. The container
                # runtime tears down its PID namespace before restarting it.
                os._exit(1)
            for stream in (process.stdin, process.stdout):
                stream.close()
            LOGGER.info("PDF processes reaped child_pid=%d elapsed=%.3fs", process.pid, monotonic() - started)

        if process.returncode or len(result) > MAX_PDF_BYTES or not result.startswith(b"%PDF-"):
            raise RenderFailed("Printing failed")
        return result


class PrinterHandler(BaseHTTPRequestHandler):
    timeout = 3

    def log_message(self, format, *args):
        pass

    def _respond(self, status, body, media_type="text/plain; charset=utf-8", **headers):
        try:
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # The job and its cleanup do not depend on the web caller staying connected.
            pass

    def do_GET(self):
        if self.path in ("/healthz", "/healthz/"):
            if not _STOP.is_set() and _assets_ready():
                self._respond(200, b"ok\n")
            else:
                self._respond(503, b"Printer is unavailable\n")
        else:
            self._respond(404, b"Not found\n")

    def _print(self, markup):
        try:
            result = _render(markup)
        except PrintBusy:
            self._respond(429, "الطباعة مشغولة. أعد المحاولة بعد خمس ثوانٍ.\n".encode(), **{"Retry-After": RETRY_AFTER})
        except (OSError, RenderFailed):
            self._respond(503, "تعذّرت الطباعة مؤقتًا.\n".encode())
        else:
            self._respond(200, result, "application/pdf")

    def do_POST(self):
        if self.path != "/render":
            self._respond(404, b"Not found\n")
            return
        if _STOP.is_set():
            self._respond(503, b"Printer is unavailable\n")
            return
        if self.headers.get_content_type() != "text/html":
            self._respond(415, b"Expected UTF-8 HTML\n")
            return
        sizes = self.headers.get_all("Content-Length", [])
        if (
            len(sizes) != 1
            or not sizes[0].isascii()
            or not sizes[0].isdecimal()
            or self.headers.get("Transfer-Encoding")
        ):
            self._respond(400, b"Invalid content length\n")
            return
        if len(sizes[0]) > len(str(MAX_HTML_BYTES)):
            self._respond(413, b"Invalid document size\n")
            return
        size = int(sizes[0])
        if not 0 < size <= MAX_HTML_BYTES:
            self._respond(413, b"Invalid document size\n")
            return
        try:
            markup = self.rfile.read(size)
            if len(markup) != size:
                self._respond(400, b"Incomplete document\n")
                return
            with _admitted_request():
                self._print(markup)
        except (OSError, RenderFailed):
            self._respond(503, "تعذّرت الطباعة مؤقتًا.\n".encode())


def _shutdown(_signum, _frame):
    if not _STOP.is_set():
        _STOP.set()
        raise _StopServing


def _serve(server):
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        server.serve_forever()
    except _StopServing:
        pass
    finally:
        _STOP.set()
        # New requests cannot be admitted after STOP. Finish existing responses,
        # including the render teardown and lock release, before exiting.
        with _REQUEST_LOCK:
            requests = tuple(_ACTIVE_REQUESTS)
        for request in requests:
            request.join()


def serve():
    if os.getpid() != 1:
        raise RuntimeError("The print worker must be PID 1 in its own container")
    _adopt_orphaned_children()
    with ThreadingHTTPServer(("0.0.0.0", 8010), PrinterHandler) as server:
        _serve(server)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve()
