from copy import deepcopy
from html.parser import HTMLParser
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings

from apps.access_control.models import ProjectAccess
from apps.access_control.services import grant_project_access
from apps.internal_api.access import MAX_JSON_BYTES
from apps.projects.models import Project, WorkNode
from apps.test_transactions.models import Attachment, AuditEvent, IdempotencyRecord, TestTransaction, TestTransactionLine

from .test_s1_05_http import (
    TEST_HASHERS,
    TEST_PASSWORD,
    TransactionHTTPTestsMixin,
    access_permission,
    csrf_headers,
    make_employee,
    make_grantor,
    private_root,
    transaction_url,
)


API_PREFIX = "/api/v1/"
INTERNAL_HOST = "employees.erp.test"
OWNER_HOST = "owners.erp.test"
EFFECT_MODELS = (TestTransaction, TestTransactionLine, Attachment, AuditEvent, IdempotencyRecord)
SNAPSHOT_FIELDS = {"id", "reference", "status", "title", "lines", "total", "created_at", "attachment_ids"}


def api_path(project_id, resource="test-transactions", item_id=None):
    collection = f"{API_PREFIX}projects/{project_id}/{resource}/"
    return collection if item_id is None else f"{collection}{item_id}/"


def draft_payload(node_id):
    return {"title": "معاملة API اصطناعية", "lines": [{
        "work_node_id": str(node_id), "description": "Synthetic API line", "qty": "3.0000", "unit_price": "12.3450",
    }]}


def assert_private(test, response):
    test.assertEqual(response.headers.get("Cache-Control"), "private, no-store")


def assert_api_error(test, response, status, code):
    test.assertEqual(response.status_code, status, response.content)
    assert_private(test, response)
    result = response.json()
    test.assertTrue({"code", "message", "correlation_id"} <= result.keys() <= {
        "code", "message", "field_errors", "correlation_id",
    })
    test.assertEqual(result["code"], code)
    test.assertTrue(result["message"])
    UUID(result["correlation_id"])
    return result


class DocsAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attributes):
        source = dict(attributes).get({"script": "src", "link": "href", "img": "src"}.get(tag, ""))
        if source:
            self.urls.append(source)


@override_settings(
    ALLOWED_HOSTS=[INTERNAL_HOST, OWNER_HOST],
    ERP_INTERNAL_HOSTS=(INTERNAL_HOST,),
    ERP_OWNER_HOSTS=(OWNER_HOST,),
    PASSWORD_HASHERS=TEST_HASHERS,
)
class InternalAPIHTTPTests(TransactionHTTPTestsMixin, TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        cls.project_a = Project.objects.get(code="DEMO-A")
        cls.project_b = Project.objects.get(code="DEMO-B")
        cls.node_a = WorkNode.objects.get(code="A-ITEM")
        cls.node_b = WorkNode.objects.get(code="B-ITEM")
        cls.operator = make_employee("s111-operator")
        cls.viewer = make_employee("s111-viewer")
        cls.manager = make_employee("s111-manager")
        cls.docs_employee = make_employee("s111-docs-only")
        cls.docs_employee.user_permissions.add(access_permission("view_internal_api_docs"))
        cls.owner = get_user_model().objects.create_user("s111-synthetic-owner", password=TEST_PASSWORD)
        grantor = make_grantor()
        for actor, role in (
            (cls.operator, ProjectAccess.Role.OPERATOR),
            (cls.viewer, ProjectAccess.Role.VIEWER),
            (cls.manager, ProjectAccess.Role.MANAGER),
        ):
            grant_project_access(actor=grantor, user=actor, project=cls.project_a, role=role)

    def setUp(self):
        self.root = private_root(self)
        self.path = api_path(self.project_a.pk)
        self.payload = draft_payload(self.node_a.pk)

    def client_for(self, host=INTERNAL_HOST):
        return Client(enforce_csrf_checks=True, headers={"Host": host})

    def sign_in(self, actor):
        client = self.client_for()
        login = transaction_url("login")
        self.assertEqual(client.get(login).status_code, 200)
        response = client.post(login, {"username": actor.username, "password": TEST_PASSWORD},
                               headers=csrf_headers(client))
        self.assertIn(response.status_code, (302, 303))
        for cookie in (settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME):
            self.assertFalse(response.cookies[cookie]["domain"])
        return client

    def post_json(self, client, data, *, key, path=None, csrf=True):
        headers = csrf_headers(client) if csrf else {}
        if key is not None:
            headers["Idempotency-Key"] = str(key)
        return client.post(path or self.path, data, content_type="application/json", headers=headers)

    def counts(self):
        return tuple(model.objects.count() for model in EFFECT_MODELS)

    def test_project_and_work_node_pages_are_scoped_and_bounded(self):
        client = self.sign_in(self.operator)
        listing = client.get(f"{API_PREFIX}projects/")
        self.assertEqual(listing.status_code, 200)
        assert_private(self, listing)
        self.assertEqual(listing.json(), {
            "items": [{"id": str(self.project_a.pk), "code": self.project_a.code,
                       "name": self.project_a.name, "status": self.project_a.status}],
            "page": 1, "page_size": 25, "total": 1,
        })
        empty = client.get(f"{API_PREFIX}projects/?page=2&page_size=1")
        self.assertEqual(empty.json(), {"items": [], "page": 2, "page_size": 1, "total": 1})
        self.assertEqual(client.get(f"{API_PREFIX}projects/?page_size=100").status_code, 200)
        nodes = client.get(api_path(self.project_a.pk, "work-nodes"), {"type": "ITEM", "page_size": 1})
        self.assertEqual(nodes.status_code, 200, nodes.content)
        self.assertEqual(nodes.json(), {
            "items": [{"id": str(self.node_a.pk), "parent_id": str(self.node_a.parent_id),
                       "type": "ITEM", "code": self.node_a.code, "name": self.node_a.name}],
            "page": 1, "page_size": 1, "total": 1,
        })
        for path in (f"{API_PREFIX}projects/?page=0", f"{API_PREFIX}projects/?page_size=101",
                     api_path(self.project_a.pk, "work-nodes") + "?type=INVALID"):
            assert_api_error(self, client.get(path), 422, "validation_error")
        assert_api_error(self, client.get(api_path(self.project_b.pk, "work-nodes")), 404, "not_found")

    def test_document_detail_projection_and_attached_file_permission(self):
        client = self.sign_in(self.operator)
        document = self.create_draft(client, self.project_a, (self.node_a, "Web trial", "3", "12.3450"))
        foreign = TestTransaction.objects.create(project=self.project_b, title="Foreign", created_by=self.owner)
        attachment = document.attachments.get()
        path = api_path(self.project_a.pk, item_id=document.pk)
        detail = client.get(path)
        self.assertEqual(detail.status_code, 200)
        assert_private(self, detail)
        self.assertEqual(set(detail.json()), SNAPSHOT_FIELDS)
        self.assertEqual(detail.json()["attachment_ids"], [str(attachment.pk)])
        self.assertNotIn(attachment.storage_key.encode(), detail.content)
        self.assertEqual(self.sign_in(self.viewer).get(path).json()["attachment_ids"], [])
        self.viewer.user_permissions.add(access_permission("download_test_attachment"))
        self.assertEqual(self.sign_in(self.viewer).get(path).json()["attachment_ids"], [str(attachment.pk)])
        errors = []
        for project, identifier in ((self.project_b, foreign.pk), (self.project_a, foreign.pk),
                                    (self.project_a, uuid4())):
            result = assert_api_error(self, client.get(api_path(project.pk, item_id=identifier)), 404, "not_found")
            errors.append({key: value for key, value in result.items() if key != "correlation_id"})
        self.assertEqual(errors, [errors[0]] * 3)

    def test_auth_csrf_and_input_errors_do_not_write(self):
        employee = self.sign_in(self.operator)
        owner = self.client_for()
        owner.force_login(self.owner)
        assert_api_error(self, self.post_json(self.client_for(), self.payload, key=uuid4(), csrf=False),
                         401, "authentication_required")
        assert_api_error(self, self.post_json(owner, self.payload, key=uuid4(), csrf=False), 403, "forbidden")
        assert_api_error(self, self.post_json(employee, self.payload, key=uuid4(), csrf=False), 403, "csrf_failed")
        assert_api_error(self, self.post_json(self.sign_in(self.viewer), self.payload, key=uuid4()), 403, "forbidden")
        assert_api_error(self, self.post_json(employee, self.payload, key=uuid4(), path=api_path(self.project_b.pk)),
                         404, "not_found")
        for key in (None, "not-a-uuid"):
            assert_api_error(self, self.post_json(employee, self.payload, key=key), 422, "validation_error")

        for field, value in (("status", "APPROVED"), ("created_by", self.operator.pk), ("total", "37.04"),
                             ("project_id", str(self.project_a.pk)), ("attachment", "ignored")):
            with self.subTest(field=field):
                assert_api_error(self, self.post_json(employee, self.payload | {field: value}, key=uuid4()),
                                 422, "validation_error")
        original = self.payload["lines"][0]
        for changes in ({"amount": "1.00"}, {"qty": 3}, {"qty": "NaN"}, {"qty": "3.00001"},
                        {"qty": "0"}, {"unit_price": "-1"}, {"work_node_id": str(self.node_b.pk)}):
            with self.subTest(changes=changes):
                invalid = self.payload | {"lines": [original | changes]}
                assert_api_error(self, self.post_json(employee, invalid, key=uuid4()), 422, "validation_error")
        for invalid in (self.payload | {"title": "x" * 161}, self.payload | {"lines": []},
                        self.payload | {"lines": [original] * 101}, b'{"title":"one","title":"two"}',
                        b"[]", b" " * (MAX_JSON_BYTES + 1)):
            assert_api_error(self, self.post_json(employee, invalid, key=uuid4()), 422, "validation_error")

        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
        self.assertEqual(list(self.root.iterdir()), [])
        get_user_model().objects.filter(pk=self.operator.pk).update(is_active=False)
        assert_api_error(self, employee.get(f"{API_PREFIX}projects/"), 401, "authentication_required")

    def test_create_at_line_limit_and_exact_replay_after_approval(self):
        client = self.sign_in(self.operator)
        key = uuid4()
        data = self.payload | {"lines": [self.payload["lines"][0]] * 100}
        success = self.post_json(client, data, key=key)
        self.assertEqual(success.status_code, 201, success.content)
        assert_private(self, success)
        snapshot = success.json()
        self.assertEqual(set(snapshot), SNAPSHOT_FIELDS)
        self.assertEqual((snapshot["status"], snapshot["total"], snapshot["attachment_ids"]), ("DRAFT", "3704.00", []))
        self.assertEqual([line["line_no"] for line in snapshot["lines"]], list(range(1, 101)))
        first_line = snapshot["lines"][0]
        self.assertEqual((first_line["qty"], first_line["unit_price"], first_line["amount"]), ("3.0000", "12.3450", "37.04"))
        receipt = IdempotencyRecord.objects.get(key=key)
        self.assertEqual((receipt.status, receipt.response_status), (IdempotencyRecord.Status.COMPLETED, 201))
        self.assertEqual(receipt.response_body, snapshot)
        self.assertEqual(self.counts(), (1, 100, 0, 1, 1))
        self.assertEqual(list(self.root.iterdir()), [])

        manager = self.sign_in(self.manager)
        path = transaction_url("approve", self.project_a.pk, receipt.test_transaction_id)
        self.assertEqual(manager.post(path, headers=csrf_headers(manager)).status_code, 302)
        live_detail = client.get(api_path(self.project_a.pk, item_id=receipt.test_transaction_id))
        self.assertEqual(live_detail.json()["status"], "APPROVED")

        equivalent = deepcopy(data)
        equivalent["lines"][0]["qty"] = "3"
        replay = self.post_json(self.sign_in(self.operator), equivalent, key=key)
        self.assertEqual((replay.status_code, replay.content), (201, success.content))
        assert_api_error(self, self.post_json(client, data | {"title": "Changed"}, key=key), 409, "conflict")
        receipt.refresh_from_db()
        self.assertEqual(receipt.response_body["status"], "DRAFT")
        self.assertEqual(self.counts(), (1, 100, 0, 2, 1))

    def test_mixed_channels_keep_original_receipts_and_fingerprints(self):
        client = self.sign_in(self.operator)
        web_key, api_key = uuid4(), uuid4()
        line = self.payload["lines"][0]

        def web_post(key, *, attach=True):
            fields = self.draft_data(client, self.project_a,
                                     (self.node_a, line["description"], line["qty"], line["unit_price"]),
                                     title=self.payload["title"])
            fields["idempotency_key"] = str(key)
            if not attach:
                fields.pop("attachment")
            return client.post(transaction_url("create", self.project_a.pk), fields, headers=csrf_headers(client))

        self.assertEqual(web_post(web_key, attach=False).status_code, 400)
        web = web_post(web_key)
        self.assertEqual(web.status_code, 303, web.content)
        assert_api_error(self, self.post_json(client, self.payload, key=web_key), 409, "conflict")
        api = self.post_json(client, self.payload, key=api_key)
        self.assertEqual(api.status_code, 201, api.content)
        self.assertEqual(web_post(api_key).status_code, 409)
        web_replay = web_post(web_key)
        self.assertEqual((web_replay.status_code, web_replay["Location"]), (303, web["Location"]))
        api_replay = self.post_json(client, self.payload, key=api_key)
        self.assertEqual((api_replay.status_code, api_replay.content), (201, api.content))
        receipt = IdempotencyRecord.objects.get(key=web_key)
        self.assertEqual(receipt.response_body, {"transaction_id": str(receipt.test_transaction_id),
                                                "document_no": receipt.test_transaction.document_no, "location": web["Location"]})
        self.assertEqual(self.counts(), (2, 2, 1, 2, 2))
        self.assertEqual(list(self.root.glob("*.pdf")), [self.root / Attachment.objects.get().storage_key])

    def test_local_openapi_is_permissioned_and_never_routed_on_owner_host(self):
        docs_path, schema_path = f"{API_PREFIX}docs/", f"{API_PREFIX}openapi.json"
        authorized = self.sign_in(self.docs_employee)
        docs = authorized.get(docs_path)
        schema = authorized.get(schema_path)
        self.assertEqual((docs.status_code, schema.status_code), (200, 200))
        assert_private(self, docs)
        assert_private(self, schema)
        self.assertEqual(set(schema.json()["paths"]), {
            f"{API_PREFIX}projects/", f"{API_PREFIX}projects/{{project_id}}/work-nodes/",
            f"{API_PREFIX}projects/{{project_id}}/test-transactions/",
            f"{API_PREFIX}projects/{{project_id}}/test-transactions/{{transaction_id}}/",
        })
        self.assertEqual(authorized.get(f"{API_PREFIX}projects/").json()["items"], [])
        asset_parser = DocsAssets()
        asset_parser.feed(docs.content.decode())
        assets = set(asset_parser.urls)
        self.assertTrue(any(asset.endswith(".css") for asset in assets))
        self.assertTrue(any(asset.endswith(".js") for asset in assets))
        for asset in assets:
            self.assertTrue(asset.startswith(f"{API_PREFIX}docs/assets/"), asset)
            resource = authorized.get(asset)
            self.assertEqual(resource.status_code, 200)
            self.assertTrue(b"".join(resource.streaming_content))
        for path in (docs_path, schema_path, *assets):
            assert_api_error(self, self.client_for().get(path), 401, "authentication_required")
            assert_api_error(self, self.sign_in(self.operator).get(path), 403, "forbidden")

        owner = self.client_for(OWNER_HOST)
        owner.force_login(self.owner)
        session = Session.objects.get(session_key=owner.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertEqual(session.get_decoded()["_auth_user_id"], str(self.owner.pk))
        forwarded = {"Host": OWNER_HOST, "X-Forwarded-Host": INTERNAL_HOST,
                     "X-Internal-Host": INTERNAL_HOST, "Forwarded": f"host={INTERNAL_HOST}"}
        for client in (owner, self.client_for(OWNER_HOST), authorized):
            for path in (docs_path, schema_path, *assets, "/admin/", f"{API_PREFIX}projects/", self.path):
                with self.subTest(path=path):
                    denied = client.get(path, headers=forwarded)
                    self.assertEqual(denied.status_code, 404)
                    self.assertNotIn(b"\"openapi\"", denied.content)
        spoofed_write = authorized.post(self.path, self.payload, content_type="application/json",
                                        headers=forwarded | csrf_headers(authorized) | {"Idempotency-Key": str(uuid4())})
        self.assertEqual(spoofed_write.status_code, 404)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
