import json
import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
import shutil
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


TRANSPORT = Path(__file__).resolve().parents[1] / "ops/backup/transport.py"
RESTIC = shutil.which("restic")


def cutoff(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


@skipUnless(RESTIC, "requires the native Restic executable")
class BackupTransportTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="s113-restic-acceptance-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "local"
        self.destination = self.root / "external"
        self.state = self.root / "state"
        self.bundles = self.root / "bundles"
        self.bundles.mkdir(mode=0o700)
        self.source_secret = self.make_secret("source-key")
        self.destination_secret = self.make_secret("destination-key")
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("RESTIC_", "S1_13_"))
        }
        self.env.update(
            RESTIC_REPOSITORY=str(self.source),
            RESTIC_PASSWORD_FILE=str(self.source_secret),
            RESTIC_CACHE_DIR=str(self.root / "restic-cache"),
            S1_13_DEST_REPOSITORY=str(self.destination),
            S1_13_DEST_PASSWORD_FILE=str(self.destination_secret),
        )
        self.restic(self.source, self.source_secret, "init")

    def make_secret(self, name):
        path = self.root / name
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            stream.write(token_hex(24) + "\n")
        return path

    def restic(self, repository, secret, *arguments, cwd=None, successful=True):
        result = subprocess.run(
            [RESTIC, "--repo", str(repository), "--password-file", str(secret), *arguments],
            cwd=cwd, env=self.env, capture_output=True, text=True, timeout=90,
        )
        if successful:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def transport(self, *arguments, successful=True, extra_env=None):
        result = subprocess.run(
            [sys.executable, str(TRANSPORT), "--state", str(self.state), "--synthetic-verify", *map(str, arguments)],
            env=self.env | (extra_env or {}), capture_output=True, text=True, timeout=120,
        )
        if successful:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def init_destination(self):
        self.restic(self.destination, self.destination_secret, "init")

    def bundle(self, cut_id, cut_at, *, payload=b"Synthetic archive for transport acceptance\n", directory=None):
        bundle = self.bundles / (directory or cut_id)
        bundle.mkdir(mode=0o700)
        files = {
            "postgres.dump": payload,
            "attachments/synthetic.pdf": f"%PDF-1.4\n% synthetic Restic fixture {cut_id}\n".encode(),
        }
        for relative, data in files.items():
            path = bundle / relative
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(data)
        manifest = {
            "cut_id": cut_id, "cut_at": cut_at, "capture_state": "Finalized",
            "files": {relative: sha256(data).hexdigest() for relative, data in files.items()},
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        return bundle

    def queue(self):
        return json.loads(self.transport("status").stdout)

    def snapshots(self, repository, secret, cut_id):
        result = self.restic(repository, secret, "snapshots", "--tag", f"s1-13:{cut_id}", "--json")
        return json.loads(result.stdout) or []

    def copy_to_destination(self, source_id):
        self.restic(
            self.destination, self.destination_secret, "copy", "--from-repo", str(self.source),
            "--from-password-file", str(self.source_secret), source_id,
        )

    def crash_after_restic(self, command):
        shim_dir = self.root / f"after-{command}"
        shim_dir.mkdir(mode=0o700)
        wrapper = shim_dir / "restic"
        wrapper.write_text(f"#!{sys.executable}\n" + """
import os
from pathlib import Path
import signal
import subprocess
import sys

pid = os.getppid()
parent_args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0')
if os.environ['S113_TEST_TRANSPORT'].encode() not in parent_args:
    raise SystemExit('Unexpected Restic caller in fault fixture')
result = subprocess.run([os.environ['S113_TEST_RESTIC'], *sys.argv[1:]])
if result.returncode == 0 and sys.argv[1] == os.environ['S113_TEST_CRASH_COMMAND']:
    os.kill(pid, signal.SIGKILL)
raise SystemExit(result.returncode)
""")
        wrapper.chmod(0o700)
        return {
            "PATH": f"{shim_dir}{os.pathsep}{self.env.get('PATH', os.defpath)}",
            "S113_TEST_TRANSPORT": str(TRANSPORT), "S113_TEST_RESTIC": RESTIC,
            "S113_TEST_CRASH_COMMAND": command,
        }

    def test_rejects_changed_or_missing_bundle_data_without_creating_a_snapshot(self):
        changed = self.bundle("changed", cutoff(1))
        (changed / "attachments/synthetic.pdf").write_bytes(b"Different synthetic file")
        result = self.transport("capture", changed, successful=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs", result.stderr)
        self.assertEqual(self.snapshots(self.source, self.source_secret, "changed"), [])
        self.assertEqual(self.queue()["jobs"], [])

        missing = self.bundle("missing", cutoff(1))
        (missing / "postgres.dump").unlink()
        result = self.transport("capture", missing, successful=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absent", result.stderr)
        self.assertEqual(self.snapshots(self.source, self.source_secret, "missing"), [])
        self.assertEqual(self.queue()["jobs"], [])

        incomplete = self.bundle("incomplete", cutoff(1))
        manifest_path = incomplete / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["capture_state"] = "Cancelled"
        manifest_path.write_text(json.dumps(manifest))
        result = self.transport("capture", incomplete, successful=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshots(self.source, self.source_secret, "incomplete"), [])
        self.assertEqual(self.queue()["jobs"], [])

    def test_restarts_after_the_actual_local_snapshot_without_creating_a_duplicate(self):
        bundle = self.bundle("capture-crash", cutoff(1))
        failed = self.transport("capture", bundle, successful=False, extra_env=self.crash_after_restic("backup"))
        self.assertEqual(failed.returncode, -signal.SIGKILL, failed.stderr)
        [snapshot] = self.snapshots(self.source, self.source_secret, "capture-crash")
        [pending] = self.queue()["jobs"]
        self.assertEqual(pending["state"], "Capturing")

        recovered = json.loads(self.transport("capture", bundle).stdout)
        self.assertEqual(recovered["job_id"], pending["job_id"])
        self.assertEqual(recovered["state"], "Pending")
        self.assertEqual(recovered["source_snapshot_id"], snapshot["id"])
        self.assertEqual(len(self.snapshots(self.source, self.source_secret, "capture-crash")), 1)
        self.assertEqual(len(self.queue()["jobs"]), 1)

    def test_deferred_older_cut_does_not_block_newer_cut_and_rpo_uses_cut_time(self):
        old_cut = cutoff(48)
        new_cut = cutoff(2)
        old = json.loads(self.transport("capture", self.bundle("older", old_cut)).stdout)
        attempt = self.transport("transfer", successful=False)
        self.assertNotEqual(attempt.returncode, 0)
        [deferred] = self.queue()["jobs"]
        self.assertEqual((deferred["state"], deferred["attempts"]), ("Pending", 1))
        self.assertGreater(datetime.fromisoformat(deferred["next_attempt_at"]), datetime.now(timezone.utc))

        new = json.loads(self.transport("capture", self.bundle("newer", new_cut)).stdout)
        offline_status = self.queue()
        self.assertEqual([item["job_id"] for item in offline_status["jobs"]], [old["job_id"], new["job_id"]])
        self.assertIsNone(offline_status["latest_verified_cut_at"])
        self.assertTrue(offline_status["rpo_alert"])
        self.init_destination()
        status = json.loads(self.transport("transfer").stdout)
        self.assertEqual([item["state"] for item in status["jobs"]], ["Pending", "Verified"])
        self.assertEqual(status["latest_verified_cut_at"], new_cut)
        self.assertFalse(status["rpo_alert"])
        self.assertEqual(self.snapshots(self.destination, self.destination_secret, "older"), [])
        self.assertEqual(len(self.snapshots(self.destination, self.destination_secret, "newer")), 1)

        # Advance only the due time of the isolated deferred job while no worker is running.
        queue_path = self.state / "queue.json"
        delayed = json.loads(queue_path.read_text())
        delayed["jobs"][0]["next_attempt_at"] = cutoff(1)
        queue_path.write_text(json.dumps(delayed))
        status = json.loads(self.transport("transfer").stdout)
        self.assertEqual([item["state"] for item in status["jobs"]], ["Verified", "Verified"])
        self.assertEqual([item["job_id"] for item in status["jobs"]], [old["job_id"], new["job_id"]])
        self.assertGreater(status["jobs"][0]["verified_at"], status["jobs"][1]["verified_at"])
        self.assertEqual(status["latest_verified_cut_at"], new_cut)
        self.assertFalse(status["rpo_alert"])
        self.assertEqual(len(self.snapshots(self.destination, self.destination_secret, "older")), 1)

    def test_old_transferred_cut_still_raises_rpo_alert(self):
        self.init_destination()
        old_cut = cutoff(26)
        self.transport("capture", self.bundle("old-rpo", old_cut))
        status = json.loads(self.transport("transfer").stdout)
        [transferred] = status["jobs"]
        self.assertEqual(transferred["state"], "Verified")
        self.assertEqual(status["latest_verified_cut_at"], old_cut)
        self.assertTrue(status["rpo_alert"])

    def test_replacing_destination_repository_invalidates_its_rpo_evidence(self):
        self.init_destination()
        cut_at = cutoff(1)
        self.transport("capture", self.bundle("new-repository", cut_at))
        verified = json.loads(self.transport("transfer").stdout)
        self.assertEqual(verified["latest_verified_cut_at"], cut_at)
        self.assertFalse(verified["rpo_alert"])

        self.destination.rename(self.root / "old-external")
        self.init_destination()
        replaced_status = self.queue()
        self.assertIsNone(replaced_status["latest_verified_cut_at"])
        self.assertTrue(replaced_status["rpo_alert"])
        self.assertEqual(self.snapshots(self.destination, self.destination_secret, "new-repository"), [])

    def test_wrong_manifest_under_existing_destination_tag_is_never_verified(self):
        self.init_destination()
        cut_at = cutoff(1)
        self.transport("capture", self.bundle("mismatched", cut_at))
        wrong_bundle = self.bundle("mismatched", cut_at, payload=b"Wrong synthetic archive", directory="wrong-bundle")
        self.restic(self.destination, self.destination_secret, "backup", "--tag", "s1-13:mismatched", ".", cwd=wrong_bundle)

        failed = self.transport("transfer", successful=False)
        self.assertNotEqual(failed.returncode, 0)
        status = self.queue()
        [job] = status["jobs"]
        self.assertEqual(job["state"], "Pending")
        self.assertIn("manifest", job["last_error"].lower())
        self.assertIsNone(status["latest_verified_cut_at"])
        self.assertTrue(status["rpo_alert"])
        self.assertEqual(len(self.snapshots(self.destination, self.destination_secret, "mismatched")), 1)

    def test_corrupt_encrypted_destination_data_remains_pending(self):
        self.init_destination()
        captured = json.loads(self.transport("capture", self.bundle("corrupt-pack", cutoff(1))).stdout)
        self.copy_to_destination(captured["source_snapshot_id"])
        [snapshot] = self.snapshots(self.destination, self.destination_secret, "corrupt-pack")
        packages = [file for file in (self.destination / "data").glob("*/*") if file.is_file()]
        self.assertTrue(packages, "No encrypted Restic data files were copied")
        for package in packages:
            mode = stat.S_IMODE(package.stat().st_mode)
            package.chmod(mode | stat.S_IWUSR)
            try:
                with package.open("r+b") as stream:
                    first = stream.read(1)
                    stream.seek(0)
                    stream.write(bytes([first[0] ^ 1]))
            finally:
                package.chmod(mode)
        self.assertNotEqual(
            self.restic(self.destination, self.destination_secret, "check", "--read-data", successful=False).returncode, 0,
        )

        self.assertNotEqual(self.transport("transfer", successful=False).returncode, 0)
        status = self.queue()
        [job] = status["jobs"]
        self.assertEqual(job["state"], "Pending")
        self.assertEqual(job["attempts"], 1)
        self.assertNotIn("destination_snapshot_id", job)
        self.assertIsNone(status["latest_verified_cut_at"])
        self.assertEqual(self.snapshots(self.destination, self.destination_secret, "corrupt-pack")[0]["id"], snapshot["id"])

    def test_actual_copy_reconciles_after_process_kill_and_restores_without_source(self):
        self.init_destination()
        bundle = self.bundle("copy-crash", cutoff(1))
        manifest = (bundle / "manifest.json").read_bytes()
        captured = json.loads(self.transport("capture", bundle).stdout)
        failed = self.transport("transfer", successful=False, extra_env=self.crash_after_restic("copy"))
        self.assertEqual(failed.returncode, -signal.SIGKILL, failed.stderr)
        [snapshot] = self.snapshots(self.destination, self.destination_secret, "copy-crash")
        [pending] = self.queue()["jobs"]
        self.assertEqual(pending["state"], "Pending")
        self.assertEqual(pending["job_id"], captured["job_id"])

        status = json.loads(self.transport("transfer").stdout)
        [job] = status["jobs"]
        self.assertEqual(job["state"], "Verified")
        self.assertEqual(job["job_id"], captured["job_id"])
        self.assertEqual(job["destination_snapshot_id"], snapshot["id"])
        self.transport("transfer")
        self.assertEqual(len(self.snapshots(self.destination, self.destination_secret, "copy-crash")), 1)

        self.source.rename(self.root / "unreachable-source")
        self.bundles.rename(self.root / "unreachable-bundles")
        wrong_key = self.make_secret("invalid-recovery-key")
        denied = self.restic(self.destination, wrong_key, "snapshots", successful=False)
        self.assertNotEqual(denied.returncode, 0)
        restored = self.root / "restored-external"
        self.restic(self.destination, self.destination_secret, "restore", snapshot["id"], "--target", str(restored))
        [manifest_file] = restored.rglob("manifest.json")
        self.assertEqual(manifest_file.read_bytes(), manifest)
        for relative, digest in json.loads(manifest)["files"].items():
            self.assertEqual(sha256((manifest_file.parent / relative).read_bytes()).hexdigest(), digest)


def seed_document():
    """Store a real synthetic web receipt and private file as the source runtime role."""
    from uuid import uuid4

    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.urls import reverse

    from apps.test_transactions.models import Attachment
    from apps.test_transactions.services import create_draft_once
    from tests.s1_12_acceptance import ACCOUNTS, PROJECTS, attachment_for

    _, project_id, work_node_id = PROJECTS["a"]
    employee = get_user_model().objects.get(username=ACCOUNTS["a"])
    fixture = attachment_for("S1-13-synthetic")
    lines = [
        {
            "work_node": work_node_id, "description": f"اختبار PO-17 / ١٢٣ LINE-{index:03d}",
            "qty": "8.0000", "unit_price": "1.2500",
        }
        for index in range(1, 81)
    ]
    record = create_draft_once(
        actor=employee, project_id=project_id, key=uuid4(), title="S1-13 synthetic recovery fixture", lines=lines,
        uploaded_file=SimpleUploadedFile(fixture["name"], fixture["buffer"], content_type=fixture["mimeType"]),
    )
    document = record.test_transaction
    attachment = Attachment.objects.get(transaction=document)
    print(json.dumps({
        "detail": record.response_body["location"],
        "api": f"/api/v1/projects/{project_id}/test-transactions/{document.pk}/",
        "attachment": reverse("test_transactions:download", args=(project_id, document.pk, attachment.pk)),
        "print": reverse("test_transactions:print", args=(project_id, document.pk)),
        "reference": document.document_no, "created_at": document.created_at.isoformat(),
        "project_id": project_id, "status": document.status, "attachment_sha256": attachment.sha256,
        "storage_key": attachment.storage_key,
    }))


def verify_restored_web(port, fixture_file, print_file):
    from http.cookiejar import CookieJar
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

    from tests.s1_12_acceptance import ACCOUNTS, LOGIN, password, pdf_evidence

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    class Session:
        def __init__(self, actor=None):
            self.cookies = CookieJar()
            self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(self.cookies))
            if actor:
                assert self.request(LOGIN)[0] == 200
                csrf = next(cookie.value for cookie in self.cookies if cookie.name == "csrftoken")
                form = urlencode({"username": ACCOUNTS[actor], "password": password(actor)}).encode()
                assert self.request(LOGIN, form, {"X-CSRFToken": csrf})[0] in (302, 303)

        def request(self, path, body=None, headers=None):
            request = Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers or {})
            try:
                response = self.opener.open(request, timeout=45)
            except HTTPError as error:
                response = error
            with response:
                return response.code, response.headers, response.read()

    fixture = json.loads(Path(fixture_file).read_text())
    employee, outsider, guest = Session("a"), Session("b"), Session()
    assert guest.request("/healthz/")[0] == 200
    status, _, detail = employee.request(fixture["detail"])
    assert status == 200 and fixture["reference"].encode() in detail
    status, _, representation = employee.request(fixture["api"])
    assert status == 200
    document = json.loads(representation)
    assert document["reference"] == fixture["reference"]
    assert document["status"] == fixture["status"] and document["total"] == "800.00"
    assert datetime.fromisoformat(document["created_at"]) == datetime.fromisoformat(fixture["created_at"])
    status, headers, payload = employee.request(fixture["attachment"])
    assert status == 200 and headers.get_content_type() == "application/pdf"
    assert sha256(payload).hexdigest() == fixture["attachment_sha256"]
    status, headers, rendered = employee.request(fixture["print"])
    assert status == 200 and headers.get_content_type() == "application/pdf"
    pdf = Path(print_file)
    pdf.write_bytes(rendered)
    for endpoint in ("detail", "attachment", "print"):
        status, _, content = outsider.request(fixture[endpoint])
        assert status == 404 and fixture["reference"].encode() not in content
        status, _, content = guest.request(fixture[endpoint])
        assert status in (302, 401, 403, 404) and fixture["reference"].encode() not in content
        assert not content.startswith(b"%PDF-")
    print(json.dumps({
        "reference": fixture["reference"], "created_at": fixture["created_at"], "project_id": fixture["project_id"],
        "status": fixture["status"], "attachment_sha256": fixture["attachment_sha256"],
        "restored_print": pdf_evidence(pdf, "a"), "owner_outside_project_denied": True, "anonymous_denied": True,
    }))
