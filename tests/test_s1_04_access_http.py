from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from apps.access_control.models import ProjectAccess
from apps.access_control.policies import EMPLOYEE_GROUP
from apps.access_control.services import grant_project_access, revoke_project_access
from apps.projects.models import Project, WorkNode


TEST_URLCONF = "tests.s1_04_urlconf"
TEST_PASSWORD = "S1-04-only-synthetic-password"


@override_settings(ROOT_URLCONF=TEST_URLCONF)
class ProjectAccessHTTPTests(TestCase):
    fixtures = ["s1_03_demo"]

    @classmethod
    def setUpTestData(cls):
        cls.project_a = Project.objects.get(code="DEMO-A")
        cls.project_b = Project.objects.get(code="DEMO-B")
        cls.node_a = WorkNode.objects.get(code="A-ITEM")
        cls.node_b = WorkNode.objects.get(code="B-ITEM")
        group = Group.objects.create(name=EMPLOYEE_GROUP)
        accounts = get_user_model().objects
        cls.employee_a = accounts.create_user(username="http-user-a", password=TEST_PASSWORD)
        cls.employee_b = accounts.create_user(username="http-user-b", password=TEST_PASSWORD)
        cls.manager = accounts.create_user(username="http-manager", password=TEST_PASSWORD)
        cls.grantor = accounts.create_user(username="http-grantor")
        cls.external = accounts.create_user(username="http-owner", password=TEST_PASSWORD)
        group.user_set.add(cls.employee_a, cls.employee_b, cls.manager, cls.grantor)
        cls.grantor.user_permissions.add(Permission.objects.get(
            content_type__app_label="access_control",
            content_type__model="projectaccess",
            codename="manage_project_access",
        ))
        cls.access_a = grant_project_access(
            actor=cls.grantor, user=cls.employee_a, project=cls.project_a, role=ProjectAccess.Role.OPERATOR,
        )
        grant_project_access(
            actor=cls.grantor, user=cls.employee_b, project=cls.project_b, role=ProjectAccess.Role.VIEWER,
        )
        grant_project_access(
            actor=cls.grantor, user=cls.manager, project=cls.project_a, role=ProjectAccess.Role.MANAGER,
        )
        ProjectAccess.objects.create(
            user=cls.external, project=cls.project_a, role=ProjectAccess.Role.MANAGER,
            granted_by=cls.grantor, granted_at=timezone.now(),
        )

    def sign_in(self, actor):
        client = Client(enforce_csrf_checks=True)
        self.assertTrue(client.login(username=actor.username, password=TEST_PASSWORD))
        return client

    def test_account_lists_search_and_details_are_scoped(self):
        listing = reverse("s104-projects")
        for actor, own, other, node in (
            (self.employee_a, self.project_a, self.project_b, self.node_a),
            (self.employee_b, self.project_b, self.project_a, self.node_b),
        ):
            with self.subTest(account=actor.username):
                client = self.sign_in(actor)
                response = client.get(listing)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"count": 1, "projects": [own.code]})
                response = client.get(listing, {"q": other.code})
                self.assertEqual(response.json(), {"count": 0, "projects": []})
                response = client.get(reverse("s104-project", args=(own.pk,)))
                self.assertEqual(response.json(), {"project": own.code})
                response = client.get(reverse("s104-node", args=(own.pk, node.pk)))
                self.assertEqual(response.json(), {"node": node.code})

    def test_cross_scope_and_missing_have_same_404_for_http_and_htmx(self):
        client = self.sign_in(self.employee_a)
        pairs = (
            (reverse("s104-project", args=(self.project_b.pk,)), reverse("s104-project", args=(uuid4(),))),
            (
                reverse("s104-node", args=(self.project_a.pk, self.node_b.pk)),
                reverse("s104-node", args=(self.project_a.pk, uuid4())),
            ),
        )
        for headers in ({}, {"HX-Request": "true"}):
            for foreign, missing in pairs:
                with self.subTest(headers=headers, path=foreign):
                    denied = client.get(foreign, headers=headers)
                    absent = client.get(missing, headers=headers)
                    self.assertEqual((denied.status_code, absent.status_code), (404, 404))
                    self.assertEqual(denied.json(), {"message": "Resource unavailable."})
                    self.assertEqual(denied.content, absent.content)

    def test_external_anonymous_and_disabled_accounts_cannot_read(self):
        listing = reverse("s104-projects")
        self.assertEqual(self.client.get(listing).status_code, 401)
        outsider = self.sign_in(self.external)
        self.assertEqual(outsider.get(listing).status_code, 403)
        employee = self.sign_in(self.employee_a)
        self.assertEqual(employee.get(listing).status_code, 200)
        get_user_model().objects.filter(pk=self.employee_a.pk).update(is_active=False)
        self.assertEqual(employee.get(listing).status_code, 401)

    def test_open_session_observes_membership_revocation(self):
        employee = self.sign_in(self.employee_a)
        project = reverse("s104-project", args=(self.project_a.pk,))
        self.assertEqual(employee.get(project).status_code, 200)
        revoke_project_access(actor=self.grantor, membership_id=self.access_a.pk)
        self.assertEqual(employee.get(project).status_code, 404)
        self.assertEqual(employee.get(reverse("s104-projects")).json(), {"count": 0, "projects": []})

    @patch("tests.s1_04_urlconf._approval_effect")
    def test_approval_requires_correct_project_and_role_and_csrf(self, effect):
        allowed = reverse("s104-approve", args=(self.project_a.pk,))
        outside = reverse("s104-approve", args=(self.project_b.pk,))
        manager = self.sign_in(self.manager)
        operator = self.sign_in(self.employee_a)
        listing = reverse("s104-projects")
        manager.get(listing)
        operator.get(listing)
        manager_csrf = manager.cookies[settings.CSRF_COOKIE_NAME].value
        operator_csrf = operator.cookies[settings.CSRF_COOKIE_NAME].value

        self.assertEqual(manager.post(allowed).status_code, 403)
        self.assertEqual(manager.post(outside, headers={"X-CSRFToken": manager_csrf}).status_code, 404)
        self.assertEqual(operator.post(allowed, headers={"X-CSRFToken": operator_csrf}).status_code, 403)
        effect.assert_not_called()

        self.assertEqual(manager.post(allowed, headers={"X-CSRFToken": manager_csrf}).status_code, 204)
        effect.assert_called_once_with(self.project_a)


class ProjectAccessTestURLIsolationTests(SimpleTestCase):
    def test_all_probe_paths_are_absent_from_the_normal_urlconf(self):
        from .s1_04_urlconf import urlpatterns

        identifiers = {"project_id": uuid4(), "node_id": uuid4()}
        for pattern in urlpatterns:
            with self.subTest(route=pattern.name):
                kwargs = {key: identifiers[key] for key in pattern.pattern.converters}
                probe = reverse(pattern.name, kwargs=kwargs, urlconf=TEST_URLCONF)
                self.assertEqual(resolve(probe, urlconf=TEST_URLCONF).url_name, pattern.name)
                with self.assertRaises(Resolver404):
                    resolve(probe, urlconf=settings.ROOT_URLCONF)
