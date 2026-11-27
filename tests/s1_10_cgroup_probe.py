import argparse
import json
import os
import re
import subprocess
import unicodedata
from hashlib import sha256
from http.cookiejar import CookieJar
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


USERNAME = "s110_cgroup_employee"
LINE_COUNT = 80


def seed():
    from decimal import Decimal

    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group, Permission
    from django.urls import reverse

    from apps.access_control.models import ProjectAccess
    from apps.access_control.policies import EMPLOYEE_GROUP
    from apps.access_control.services import grant_project_access
    from apps.projects.models import Project, WorkNode
    from apps.test_transactions.models import AuditEvent, TestTransaction, TestTransactionLine

    employee_group = Group.objects.create(name=EMPLOYEE_GROUP)
    accounts = get_user_model().objects
    grantor = accounts.create_user("s110_cgroup_grantor")
    employee = accounts.create_user(USERNAME, password=os.environ["QA_PRINT_TEST_PASSWORD"])
    employee_group.user_set.add(grantor, employee)
    grantor.user_permissions.add(Permission.objects.get(
        content_type__app_label="access_control", codename="manage_project_access",
    ))
    project = Project.objects.get(code="DEMO-A")
    node = WorkNode.objects.get(project=project, code="A-ITEM")
    grant_project_access(actor=grantor, user=employee, project=project, role=ProjectAccess.Role.OPERATOR)
    document = TestTransaction.objects.create(project=project, created_by=employee, title="اختبار الطباعة والذاكرة")
    TestTransactionLine.objects.bulk_create([
        TestTransactionLine(
            transaction=document, project=project, work_node=node, line_no=number,
            description=f"اختبار PO-17 / ١٢٣ LINE-{number:03d}",
            qty=Decimal("8.0000"), unit_price=Decimal("1.2500"), amount=Decimal("10.00"),
        )
        for number in range(1, LINE_COUNT + 1)
    ])
    AuditEvent.objects.create(
        project=project, actor=employee, test_transaction=document,
        event_type=AuditEvent.EventType.CREATED, next_state=TestTransaction.Status.DRAFT,
    )
    print(reverse("test_transactions:print", args=(project.pk, document.pk)))


def database():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, current_setting('server_version_num'), count(*) FROM test_transactions_testtransaction")
        user, version, documents = cursor.fetchone()
    assert user == os.environ["POSTGRES_USER"], user
    assert int(version) // 10000 == 18, version
    assert documents == 1, documents
    print(f"Web PostgreSQL {version}, restricted role {user}, synthetic documents {documents}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Session:
    def __init__(self, port):
        self.base_url = f"http://127.0.0.1:{port}"
        cookies = CookieJar()
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(cookies))
        login = "/employees/login/"
        assert self.request(login)[0] == 200
        csrf = next(cookie.value for cookie in cookies if cookie.name == "csrftoken")
        form = urlencode({"username": USERNAME, "password": os.environ["QA_PRINT_TEST_PASSWORD"]}).encode()
        assert self.request(login, form, {"X-CSRFToken": csrf})[0] in (302, 303)

    def request(self, path, data=None, headers=None):
        request = Request(self.base_url + path, data=data, headers=headers or {})
        try:
            response = self.opener.open(request, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read()


def verify_pdf(path, content, headers):
    assert headers.get_content_type() == "application/pdf"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert content.startswith(b"%PDF-")
    path.write_bytes(content)
    text = subprocess.check_output(("pdftotext", "-layout", str(path), "-"), text=True)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    pages = [page for page in text.split("\f") if page.strip()]
    assert len(pages) > 1 and all("السطر" in page and "المبلغ" in page for page in pages)
    assert sorted(re.findall(r"\bLINE-(\d{3})\b", text)) == [f"{line:03d}" for line in range(1, LINE_COUNT + 1)]
    assert text.count("PO-17") == text.count("١٢٣") == LINE_COUNT
    assert text.count("800.00") == 1 and "800.00" in pages[-1]
    return {"bytes": len(content), "pages": len(pages), "lines": LINE_COUNT, "total": "800.00", "sha256": sha256(content).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--path-file", type=Path, required=True)
    parser.add_argument("--expect", choices=(200, 503), type=int, required=True)
    parser.add_argument("--pdf", type=Path)
    args = parser.parse_args()
    client = Session(args.port)
    path = args.path_file.read_text().strip()
    assert path.startswith("/employees/projects/") and path.endswith("/print/"), path
    start = monotonic()
    status, headers, content = client.request(path)
    elapsed = round(monotonic() - start, 3)
    print(json.dumps({"print_status": status, "seconds": elapsed}), flush=True)
    assert status == args.expect, status
    assert "no-store" in headers.get("Cache-Control", "")
    if status == 200:
        assert args.pdf is not None
        print(json.dumps(verify_pdf(args.pdf, content, headers)))
    else:
        assert headers.get_content_type() == "text/plain"
        assert "تعذّرت الطباعة" in content.decode()


if __name__ == "__main__":
    main()
