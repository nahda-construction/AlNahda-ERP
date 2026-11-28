import argparse
import base64
import json
import os
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import monotonic
from urllib.parse import urljoin, urlsplit


ACCOUNTS = {"a": "s112_operator_a", "b": "s112_operator_b"}
PROJECTS = {
    "a": ("DEMO-A", "00000000-0000-4000-8000-000000000011", "00000000-0000-4000-8000-000000000022"),
    "b": ("DEMO-B", "00000000-0000-4000-8000-000000000012", "00000000-0000-4000-8000-000000000026"),
}
WAIT_MS = 45_000
LOGIN = "/employees/login/"
DOCS = "/api/v1/docs/"
SCHEMA = "/api/v1/openapi.json"


def password(actor):
    return os.environ[f"S1_12_PASSWORD_{actor.upper()}"]


def seed():
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group, Permission

    from apps.access_control.models import ProjectAccess
    from apps.access_control.policies import EMPLOYEE_GROUP
    from apps.access_control.services import grant_project_access
    from apps.projects.models import Project, WorkNode

    accounts = get_user_model().objects
    group = Group.objects.create(name=EMPLOYEE_GROUP)
    grantor = accounts.create_user("s112_synthetic_grantor")
    grantor.groups.add(group)
    grantor.user_permissions.add(Permission.objects.get(
        content_type__app_label="access_control", codename="manage_project_access",
    ))
    for actor, (code, project_id, node_id) in PROJECTS.items():
        project = Project.objects.get(code=code, pk=project_id)
        WorkNode.objects.get(project=project, pk=node_id)
        employee = accounts.create_user(ACCOUNTS[actor], password=password(actor))
        employee.groups.add(group)
        grant_project_access(actor=grantor, user=employee, project=project, role=ProjectAccess.Role.OPERATOR)
        if actor == "a":
            employee.user_permissions.add(Permission.objects.get(
                content_type__app_label="access_control", codename="view_internal_api_docs",
            ))
    print("Seeded two synthetic operators scoped to separate projects")


def ledger(title):
    from apps.test_transactions.models import Attachment, AuditEvent, IdempotencyRecord, TestTransaction, TestTransactionLine

    documents = TestTransaction.objects.filter(title=title)
    attachments = Attachment.objects.filter(transaction__in=documents)
    receipts = IdempotencyRecord.objects.filter(test_transaction__in=documents)
    result = {
        "title": title,
        "documents": documents.count(),
        "lines": TestTransactionLine.objects.filter(transaction__in=documents).count(),
        "attachments": attachments.count(),
        "events": AuditEvent.objects.filter(test_transaction__in=documents, event_type="CREATED").count(),
        "receipts": receipts.count(),
        "references": list(documents.values_list("document_no", flat=True)),
        "attachment_sha256": list(attachments.values_list("sha256", flat=True)),
        "attachment_names": list(attachments.values_list("original_name", flat=True)),
        "receipt_state": list(receipts.values_list("status", flat=True)),
        "receipt_status": list(receipts.values_list("response_status", flat=True)),
        "receipt_locations": [value["location"] for value in receipts.values_list("response_body", flat=True)],
    }
    print(json.dumps(result, ensure_ascii=False))


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def evidence(state, name, values):
    result = {"time_utc": timestamp(), **values}
    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    temporary = state / f".{name}.tmp"
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(state / f"{name}.json")
    print(text, flush=True)


def attachment_for(label):
    content = f"%PDF-1.4\n% synthetic S1-12 {label}\n".encode()
    offsets = []
    for number, obj in enumerate((
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] >>",
    ), start=1):
        offsets.append(len(content))
        content += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(content)
    content += b"xref\n0 4\n0000000000 65535 f \n"
    content += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    content += f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return {"buffer": content, "mimeType": "application/pdf", "name": f"{label.lower()}.pdf"}


def origin(host, port):
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or not 0 < port <= 65535:
        raise ValueError("Invalid staging DNS name or port")
    return f"https://{host}:{port}"


def sign_in(page, internal, actor):
    response = page.goto(internal + LOGIN, wait_until="domcontentloaded")
    assert response.status == 200
    certificate = response.security_details()
    assert certificate and certificate["protocol"].startswith("TLS")
    page.locator("#id_username").fill(ACCOUNTS[actor])
    page.locator("#id_password").fill(password(actor))
    with page.expect_navigation(wait_until="domcontentloaded") as navigation:
        page.locator("form button[type=submit]").click()
    assert navigation.value.status == 200
    assert urlsplit(page.url).path == "/employees/projects/"
    return {"tls": certificate["protocol"], "certificate_subject": certificate["subjectName"], "login": 200}


def create_location(internal, actor):
    return f"{internal}/employees/projects/{PROJECTS[actor][1]}/transactions/new/"


def form_values(page):
    if page.locator("#id_title").count() != 1:
        return None
    lines = page.locator("#id_lines-TOTAL_FORMS").input_value()
    fields = []
    for index in range(int(lines)):
        fields.append([
            page.locator(f"[name=lines-{index}-{field}]").input_value()
            for field in ("work_node", "description", "qty", "unit_price")
        ])
    return {
        "title": page.locator("#id_title").input_value(), "lines": fields,
        "key": page.locator("[name=idempotency_key]").input_value(),
    }


def read_file(page):
    return page.locator("#id_attachment").evaluate("""async input => {
      const file = input.files[0];
      if (!file) return null;
      const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
      return {name: file.name, sha256: Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')};
    }""")


def prepare_form(page, internal, actor, title, rows, attachment):
    assert page.goto(create_location(internal, actor), wait_until="domcontentloaded").status == 200
    page.locator("#id_title").fill(title)
    for index in range(rows):
        if index:
            page.locator("#add-line").click()
        prefix = f"[name=lines-{index}-"
        page.locator(prefix + "work_node]").select_option(PROJECTS[actor][2])
        page.locator(prefix + "description]").fill(f"اختبار PO-17 / ١٢٣ LINE-{index + 1:03d}")
        page.locator(prefix + "qty]").fill("8")
        page.locator(prefix + "unit_price]").fill("1.2500")
    page.locator("#id_attachment").set_input_files(attachment)
    result = form_values(page)
    assert result and result["key"] and len(result["lines"]) == rows
    assert read_file(page) == {"name": attachment["name"], "sha256": sha256(attachment["buffer"]).hexdigest()}
    return result


def is_submission(request, location):
    return request.url == location and request.method == "POST"


def submit(page, location):
    with page.expect_response(lambda response: is_submission(response.request, location)) as answer:
        page.locator("form button[type=submit]").click(no_wait_after=True)
    response = answer.value
    redirect = response.headers.get("location")
    assert response.status == 303 and redirect, response.status
    page.wait_for_url(urljoin(location, redirect), wait_until="domcontentloaded")
    return {"status": response.status, "location": redirect}


def browser_request(page, url, include_body=False):
    return page.evaluate("""async ({url, includeBody}) => {
      const response = await fetch(url, {credentials: 'same-origin'});
      let body = '';
      if (includeBody) {
        const bytes = new Uint8Array(await response.arrayBuffer());
        for (let i = 0; i < bytes.length; i += 8192) {
          body += String.fromCharCode(...bytes.subarray(i, i + 8192));
        }
        body = btoa(body);
      }
      return {status: response.status, type: response.headers.get('content-type'),
              retry_after: response.headers.get('retry-after'), body};
    }""", {"url": url, "includeBody": include_body})


def document_info(page, internal, detail, expected_sha):
    assert page.goto(urljoin(internal, detail), wait_until="domcontentloaded").status == 200
    reference = page.locator("main > p bdi").first.inner_text().strip()
    assert reference.startswith("TST-")
    file_url = page.locator("a[href*='/attachments/']").get_attribute("href")
    print_url = page.locator("a[href$='/print/']").get_attribute("href")
    attachment = browser_request(page, urljoin(internal, file_url), include_body=True)
    assert attachment["status"] == 200 and attachment["type"] == "application/pdf"
    digest = sha256(base64.b64decode(attachment["body"])).hexdigest()
    assert digest == expected_sha
    return {"detail": detail, "attachment": file_url, "print": print_url, "reference": reference, "attachment_sha256": digest}


def pdf_evidence(pdf, actor):
    assert pdf.read_bytes().startswith(b"%PDF-")
    data = {"pdf_sha256": sha256(pdf.read_bytes()).hexdigest(), "bytes": pdf.stat().st_size}
    if actor == "a":
        text = subprocess.check_output(("pdftotext", "-layout", str(pdf), "-"), text=True)
        text = "".join(char for char in text if unicodedata.category(char) != "Cf")
        pages = [page for page in text.split("\f") if page.strip()]
        assert len(pages) > 1 and all("السطر" in page and "المبلغ" in page for page in pages)
        assert sorted(re.findall(r"\bLINE-(\d{3})\b", text)) == [f"{index:03d}" for index in range(1, 81)]
        assert text.count("PO-17") == text.count("١٢٣") == 80
        assert text.count("800.00") == 1 and "800.00" in pages[-1]
        data.update(pages=len(pages), lines=80, total="800.00", repeated_headers=True)
    return data


def hold_for(page, state, marker):
    deadline = monotonic() + WAIT_MS / 1000
    while monotonic() < deadline:
        if (state / marker).is_file():
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"Missing acceptance control marker {marker}")


def failed_submit(page, location, timeout=WAIT_MS):
    status = page.locator("#transaction-save-status")
    predicate = lambda request: is_submission(request, location)
    started = monotonic()
    with page.expect_event("requestfailed", predicate=predicate, timeout=timeout) as failed:
        page.locator("form button[type=submit]").click(no_wait_after=True)
    request = failed.value
    assert request.failure, "No browser network failure reported"
    status.wait_for(state="visible", timeout=3_000)
    message = status.inner_text()
    return {
        "client_network_error": request.failure,
        "elapsed_seconds": round(monotonic() - started, 3),
        "visible_error": "تعذّر تأكيد الحفظ" in message,
        "client_message": message,
        "success_page": page.url != location,
    }


def recover_form(page, original, attachment):
    observed = form_values(page)
    preserved = {
        "restoration": "same_document",
        "title_retained": bool(observed and observed["title"] == original["title"]),
        "lines_retained": bool(observed and observed["lines"] == original["lines"]),
        "key_retained": bool(observed and observed["key"] == original["key"]),
    }
    if all(preserved[name] for name in ("title_retained", "lines_retained", "key_retained")):
        selected = read_file(page)
        preserved["file_reselected"] = selected is None
        if selected is None:
            page.locator("#id_attachment").set_input_files(attachment)
            selected = read_file(page)
        preserved["file_match"] = selected == {
            "name": attachment["name"], "sha256": sha256(attachment["buffer"]).hexdigest(),
        }
    return preserved


def outage(page, internal, state, phase):
    title = f"S1-12-{phase.upper()[:-1]}{phase[-1]}"
    attachment = attachment_for(title)
    before = prepare_form(page, internal, "a", title, 2, attachment)
    endpoint = create_location(internal, "a")
    first = {}
    route_errors = []
    cdp = None

    def withhold_reply(event):
        request_id = event["requestId"]
        if event["request"]["method"] != "POST":
            cdp.send("Fetch.continueResponse", {"requestId": request_id})
            return
        try:
            headers = {entry["name"].lower(): entry["value"] for entry in event.get("responseHeaders", ())}
            first.update(status=event.get("responseStatusCode"), location=headers.get("location"))
            evidence(state, "a04b-first", {
                "title": title, **first, "attachment_sha256": sha256(attachment["buffer"]).hexdigest(),
                "attachment_name": attachment["name"],
            })
            assert first["status"] == 303 and first["location"]
            hold_for(page, state, "a04b-commit")
        except Exception as error:
            route_errors.append(type(error).__name__)
            cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "ConnectionFailed"})

    evidence(state, phase + "-ready", {"title": title, "lines": len(before["lines"])})
    if phase == "a04a":
        hold_for(page, state, "a04a-cut")
    else:
        cdp = page.context.new_cdp_session(page)
        cdp.on("Fetch.requestPaused", withhold_reply)
        cdp.send("Fetch.enable", {"patterns": [{"urlPattern": endpoint, "requestStage": "Response"}]})
    try:
        # In A04b the response stays withheld until the application's own deadline cancels it.
        failure = failed_submit(page, endpoint, timeout=75_000 if phase == "a04b" else WAIT_MS)
    finally:
        if cdp is not None:
            cdp.send("Fetch.disable")
            cdp.detach()
    evidence(state, phase + "-failed", {"title": title, **failure, "route_errors": route_errors})
    if phase == "a04a":
        hold_for(page, state, "a04a-resume")
    assert page.url == endpoint
    if phase == "a04a":
        # Exercise optional file re-selection without restoring any other field.
        page.locator("#id_attachment").set_input_files([])
    recovered = recover_form(page, before, attachment)
    evidence(state, phase + "-retained", {"title": title, **recovered})
    assert failure["visible_error"] and not failure["success_page"]
    assert not route_errors, route_errors
    if phase == "a04b":
        assert failure["elapsed_seconds"] >= 59, failure
    assert all(recovered.get(field) for field in ("title_retained", "lines_retained", "key_retained", "file_match")), recovered
    retry = submit(page, endpoint)
    if phase == "a04b":
        assert retry == first, {"first": first, "retry": retry}
        page.go_back(wait_until="domcontentloaded")
        assert page.url == endpoint
        assert page.locator("form button[type=submit]").is_enabled()
        evidence(state, "a04b-history", {"returned_to_form": True, "submit_enabled": True})
    saved = document_info(page, internal, retry["location"], sha256(attachment["buffer"]).hexdigest())
    evidence(state, phase + "-result", {"title": title, "retry": retry, "first": first or None, **saved})


def permissions(page, internal, owner, state, actor):
    mapping = {}
    for path in (DOCS, SCHEMA):
        status = page.goto(internal + path, wait_until="networkidle").status
        assert status == (200 if actor == "a" else 403), (path, status)
        mapping[f"internal:{path}"] = status
    page.set_extra_http_headers({"X-Forwarded-Host": urlsplit(internal).hostname, "X-Forwarded-Proto": "http"})
    try:
        for path in (LOGIN, DOCS, SCHEMA, "/api/v1/projects/", "/admin/"):
            status = page.goto(owner + path, wait_until="domcontentloaded").status
            assert status == 404, (path, status)
            mapping[f"owner:{path}"] = status
    finally:
        page.set_extra_http_headers({})

    if actor == "a":
        page.set_extra_http_headers({"X-Forwarded-Host": urlsplit(owner).hostname, "X-Forwarded-Proto": "http"})
        try:
            status = page.goto(internal + DOCS, wait_until="domcontentloaded").status
            assert status == 200, f"Forwarded headers changed the internal route: HTTP {status}"
            mapping["internal:untrusted-forwarded-headers"] = status
        finally:
            page.set_extra_http_headers({})

    create_path = create_location(internal, actor)
    prepare_form(page, internal, actor, "S1-12-CSRF-REJECT", 1, attachment_for("S1-12-CSRF"))
    form_token = page.locator("input[name=csrfmiddlewaretoken]").input_value()
    page.locator("input[name=csrfmiddlewaretoken]").evaluate("(input) => input.remove()")
    with page.expect_response(lambda response: is_submission(response.request, create_path)) as response:
        page.locator("form button[type=submit]").click()
    missing = response.value.status
    evidence(state, f"csrf-missing-{actor}", {"status": missing})
    assert missing == 403, f"Missing CSRF expected HTTP 403, got {missing}"
    status = page.locator("#transaction-save-status")
    status.wait_for(state="visible")
    assert "رُفض الطلب" in status.inner_text()

    # A real same-site cross-origin navigation supplies the browser's Origin header.
    page.goto(owner + LOGIN, wait_until="domcontentloaded")
    with page.expect_response(lambda response: is_submission(response.request, create_path)) as response:
        page.evaluate("""({target, token}) => {
          const form = document.createElement('form');
          form.action = target;
          form.method = 'POST';
          const input = document.createElement('input');
          input.name = 'csrfmiddlewaretoken';
          input.value = token;
          form.append(input);
          document.body.append(form);
          form.submit();
        }""", {"target": create_path, "token": form_token})
    foreign = response.value.status
    observed_origin = response.value.request.header_value("origin")
    evidence(state, f"permissions-{actor}", {
        "paths": mapping, "no_csrf": missing, "foreign_origin": foreign, "observed_origin": observed_origin,
    })
    assert observed_origin == owner, f"Unexpected cross-origin request: {observed_origin}"
    assert foreign == 403, f"Foreign Origin expected HTTP 403, got {foreign}"

    before = prepare_form(
        page, internal, actor, "S1-12-SESSION-EXPIRED", 1, attachment_for("S1-12-SESSION-EXPIRED"),
    )
    page.context.clear_cookies()
    with page.expect_response(lambda response: is_submission(response.request, create_path)) as response:
        page.locator("form button[type=submit]").click()
    assert response.value.status == 302
    status = page.locator("#transaction-save-status")
    status.wait_for(state="visible")
    assert "تحقق من الجلسة" in status.inner_text()
    assert page.url == create_path and form_values(page) == before
    evidence(state, f"session-redirect-{actor}", {"status": 302, "draft_retained": True})


def run(args, page, internal, owner):
    state = args.state
    actor = args.actor
    phase = args.phase
    flow = state / f"flow-{actor}.json"
    if phase == "store":
        title = f"S1-12-FLOW-{actor.upper()}"
        attachment = attachment_for(title)
        prepare_form(page, internal, actor, title, 80 if actor == "a" else 2, attachment)
        receipt = submit(page, create_location(internal, actor))
        info = document_info(page, internal, receipt["location"], sha256(attachment["buffer"]).hexdigest())
        evidence(state, f"flow-{actor}", {"title": title, "receipt": receipt, **info})
    elif phase == "reopen":
        original = json.loads(flow.read_text())
        current = document_info(page, internal, original["detail"], original["attachment_sha256"])
        assert all(current[key] == original[key] for key in current)
        evidence(state, f"reopen-{actor}", current)
    elif phase == "cross":
        assert args.peer is not None
        peer = json.loads(args.peer.read_text())
        statuses = {name: browser_request(page, urljoin(internal, peer[name]))["status"] for name in ("detail", "attachment")}
        assert statuses == {"detail": 404, "attachment": 404}, statuses
        evidence(state, f"cross-{actor}", statuses)
    elif phase == "print":
        start = monotonic()
        response = browser_request(page, urljoin(internal, json.loads(flow.read_text())["print"]), include_body=True)
        result = {"status": response["status"], "duration_seconds": round(monotonic() - start, 4), "retry_after": response["retry_after"]}
        evidence(state, f"print-{actor}-{args.expect}", result)
        assert response["status"] == args.expect, result
        if args.expect == 429:
            assert response["retry_after"] == "5", result
        else:
            assert response["type"] == "application/pdf"
            pdf = state / f"print-{actor}.pdf"
            content = base64.b64decode(response["body"])
            assert content.startswith(b"%PDF-")
            pdf.write_bytes(content)
            evidence(state, f"pdf-{actor}", {"pdf_sha256": sha256(content).hexdigest(), "bytes": len(content)})
    elif phase == "permissions":
        permissions(page, internal, owner, state, actor)
    elif phase in ("a04a", "a04b"):
        assert actor == "a"
        outage(page, internal, state, phase)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("login", "store", "reopen", "print", "cross", "permissions", "a04a", "a04b", "inspect-pdf"))
    parser.add_argument("--actor", choices=("a", "b"), default="a")
    parser.add_argument("--state", type=Path, default=Path("/tmp/s1-12-state"))
    parser.add_argument("--peer", type=Path)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--expect", type=int, choices=(200, 429), default=200)
    args = parser.parse_args()
    if args.phase == "inspect-pdf":
        if args.pdf is None:
            parser.error("--pdf is required for inspect-pdf")
        print(json.dumps(pdf_evidence(args.pdf, args.actor)))
        return

    from playwright.sync_api import sync_playwright

    args.state.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("S1_12_PORT", "8443"))
    internal = origin(os.environ["ERP_STAGING_INTERNAL_HOST"], port)
    owner = origin(os.environ["ERP_STAGING_OWNER_HOST"], port)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path="/usr/bin/chromium", args=["--disable-background-networking"],
            ignore_default_args=["--disable-back-forward-cache"],
        )
        context = browser.new_context(ignore_https_errors=False)
        used_origins = set()
        def record_origin(request):
            url = urlsplit(request.url)
            if url.scheme in {"http", "https"}:
                used_origins.add(url.netloc)

        context.on("request", record_origin)
        page = context.new_page()
        page.set_default_timeout(15_000)
        try:
            result = sign_in(page, internal, args.actor)
            evidence(args.state, f"login-{args.actor}", {**result, "chromium": browser.version})
            run(args, page, internal, owner)
            assert used_origins <= {urlsplit(internal).netloc, urlsplit(owner).netloc}
        except Exception as error:
            evidence(args.state, f"failure-{args.actor}-{args.phase}", {
                "actor": args.actor, "phase": args.phase, "error_type": type(error).__name__,
                "path": urlsplit(page.url).path,
            })
            raise SystemExit(1) from None
        finally:
            evidence(args.state, f"origins-{args.actor}-{args.phase}", {"origins": sorted(used_origins)})
            browser.close()


if __name__ == "__main__":
    main()
