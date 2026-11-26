import os
import selectors
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from struct import pack
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zlib import compress, crc32

from django.test import SimpleTestCase

from apps.printing import worker


SERVER = """
import sys
from http.server import ThreadingHTTPServer
from apps.printing import worker

worker._adopt_orphaned_children()
worker.RENDER_COMMAND = (sys.executable, '-c', sys.argv[1], sys.argv[2])
worker.RENDER_TIMEOUT = float(sys.argv[3])
with ThreadingHTTPServer(('127.0.0.1', 0), worker.PrinterHandler) as server:
    print(server.server_port, flush=True)
    worker._serve(server)
"""

RENDERER = """
import os
import signal
import subprocess
import sys
from pathlib import Path
from time import sleep

directory = Path(sys.argv[1])
body = sys.stdin.buffer.read()
(directory / 'started').write_text(str(os.getpid()))
if body == b'hold':
    while not (directory / 'release').exists():
        sleep(0.02)
if body == b'timeout':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    detached = subprocess.Popen((
        sys.executable, '-c',
        'import os, signal, sys, time; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(60)',
        str(directory / 'detached'),
    ), start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    detached.wait()
sys.stdout.buffer.write(b'%PDF-test renderer lifecycle\\n')
"""


def request_print(port, markup):
    request = Request(
        f"http://127.0.0.1:{port}/render", data=markup,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )
    try:
        response = urlopen(request, timeout=8)
    except HTTPError as error:
        response = error
    with response:
        return response.code, response.headers, response.read()


def process_birth(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def synthetic_png():
    def chunk(kind, data):
        return pack(">I", len(data)) + kind + data + pack(">I", crc32(kind + data))

    header = pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        worker.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", compress(b"\x00\x12\x34\x56"))
        + chunk(b"IEND", b"")
    )


class PrinterAssetTests(SimpleTestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.assets = Path(temporary)
        font = self.assets / "font.ttf"
        font.touch()
        (self.assets / "bootstrap.rtl.min.css").touch()
        self.enterContext(patch.multiple(
            worker, ASSETS=self.assets, ARABIC_FONT=font, CHROMIUM_EXECUTABLE=sys.executable,
        ))
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("ERP_PRINT_LOGO", None)

    def test_absent_default_logo_keeps_the_renderer_ready(self):
        self.assertTrue(worker._assets_ready())
        self.assertIsNone(worker.read_logo())

    def test_a_valid_packaged_logo_is_loaded(self):
        logo = synthetic_png()
        (self.assets / "logo.png").write_bytes(logo)
        self.assertTrue(worker._assets_ready())
        self.assertEqual(worker.read_logo(), logo)

    def test_a_missing_or_invalid_explicit_logo_disables_readiness(self):
        (self.assets / "logo.png").write_bytes(synthetic_png())
        explicit = self.assets / "explicit-logo.png"
        os.environ["ERP_PRINT_LOGO"] = str(explicit)
        self.assertFalse(worker._assets_ready())
        explicit.write_bytes(synthetic_png()[:-1])
        self.assertFalse(worker._assets_ready())
        explicit.write_bytes(synthetic_png())
        self.assertTrue(worker._assets_ready())
        self.assertEqual(worker.read_logo(), explicit.read_bytes())


class PrinterLifecycleTests(SimpleTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.lock = self.directory / "render.lock"

    def stop_supervisor(self, supervisor):
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=3)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=3)
        supervisor.stdout.close()

    def start_supervisor(self, name, *, render_timeout=6):
        state = self.directory / name
        state.mkdir()
        supervisor = subprocess.Popen(
            (sys.executable, "-c", SERVER, RENDERER, str(state), str(render_timeout)),
            env=os.environ | {"ERP_PRINT_LOCK": str(self.lock)}, stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.stop_supervisor, supervisor)
        with selectors.DefaultSelector() as selector:
            selector.register(supervisor.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), "Printer supervisor did not announce its port")
        return supervisor, int(supervisor.stdout.readline()), state

    def wait_for_marker(self, path):
        deadline = monotonic() + 4
        while monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return int(path.read_text())
            sleep(0.02)
        self.fail(f"Renderer did not write {path.name}")

    def kill_if_running(self, pid, original_birth):
        if original_birth is None or process_birth(pid) != original_birth:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_two_supervisors_share_one_fixed_lock_and_busy_request_never_renders(self):
        first_supervisor, first_port, first_state = self.start_supervisor("first")
        second_supervisor, second_port, second_state = self.start_supervisor("second")
        self.assertNotEqual(first_supervisor.pid, second_supervisor.pid)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(request_print, first_port, b"hold")
            try:
                self.wait_for_marker(first_state / "started")
                original_inode = self.lock.stat().st_ino
                started = monotonic()
                status, headers, _ = request_print(second_port, b"rejected")
                self.assertEqual(status, 429)
                self.assertEqual(headers["Retry-After"], "5")
                self.assertLess(monotonic() - started, 1)
                self.assertFalse((second_state / "started").exists())
                self.assertFalse(first.done())
            finally:
                (first_state / "release").touch()
            status, _, body = first.result(timeout=5)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"%PDF-"))
        self.assertEqual(request_print(second_port, b"retry")[0], 200)
        self.assertEqual(self.lock.stat().st_ino, original_inode)

    def test_timeout_reaps_renderer_and_detached_child_before_next_print(self):
        _, port, state = self.start_supervisor("timeout", render_timeout=1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            failed = executor.submit(request_print, port, b"timeout")
            renderer = self.wait_for_marker(state / "started")
            detached = self.wait_for_marker(state / "detached")
            self.addCleanup(self.kill_if_running, renderer, process_birth(renderer))
            self.addCleanup(self.kill_if_running, detached, process_birth(detached))
            self.assertNotEqual(os.getpgid(renderer), os.getpgid(detached))
            self.assertEqual(failed.result(timeout=6)[0], 503)
        for pid in (renderer, detached):
            self.assertFalse(Path(f"/proc/{pid}").exists())
        self.assertEqual(request_print(port, b"retry after timeout")[0], 200)

    def test_sigterm_waits_for_reaping_before_replacement_serves(self):
        supervisor, port, state = self.start_supervisor("stopping", render_timeout=6)
        with ThreadPoolExecutor(max_workers=1) as executor:
            failed = executor.submit(request_print, port, b"timeout")
            renderer = self.wait_for_marker(state / "started")
            detached = self.wait_for_marker(state / "detached")
            self.addCleanup(self.kill_if_running, renderer, process_birth(renderer))
            self.addCleanup(self.kill_if_running, detached, process_birth(detached))
            original_inode = self.lock.stat().st_ino
            supervisor.terminate()
            self.assertEqual(supervisor.wait(timeout=4), 0)
            self.assertEqual(failed.result(timeout=3)[0], 503)
        for pid in (renderer, detached):
            self.assertFalse(Path(f"/proc/{pid}").exists())
        _, replacement_port, _ = self.start_supervisor("replacement")
        self.assertEqual(request_print(replacement_port, b"new request")[0], 200)
        self.assertEqual(self.lock.stat().st_ino, original_inode)
