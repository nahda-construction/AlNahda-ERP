# سجل قبول تاريخي — S1-04-acceptance.log

نص مسترجع من سجل التشغيل الأصلي؛ ليس إعادة تشغيل على الرأس الحالي.

```text
linux ["name=seccomp,profile=builtin","name=rootless","name=cgroupns"]
Docker Compose version v5.8.0
Switched to a new branch 's1/04-project-access'
Applying: S1-04 enforce project-scoped employee access
Commit on Ubuntu: 0e8d11bfed5826156547cc386a2f6772fb653a84
Reviewed tree: 0f25a492a4207686af4a7fd05228f463724482f2
## s1/04-project-access
 apps/access_control/__init__.py                |   0
 apps/access_control/apps.py                    |   5 +
 apps/access_control/migrations/0001_initial.py | 123 +++++++++++++++++
 apps/access_control/migrations/__init__.py     |   0
 apps/access_control/models.py                  |  69 ++++++++++
 apps/access_control/policies.py                |  86 ++++++++++++
 apps/access_control/services.py                |  62 +++++++++
 config/settings.py                             |   1 +
 tests/s1_04_urlconf.py                         |  91 +++++++++++++
 tests/test_s1_04_access_http.py                | 149 ++++++++++++++++++++
 tests/test_s1_04_access_models.py              | 136 ++++++++++++++++++
 tests/test_s1_04_access_policy.py              | 182 +++++++++++++++++++++++++
 12 files changed, 904 insertions(+)
postgres (PostgreSQL) 18.7 (Debian 18.7-1.pgdg13+5)
 Image erp-test-web Building 
#1 [internal] load local bake definitions
#1 reading from stdin 498B done
#1 DONE 0.0s

#2 [internal] load build definition from Dockerfile
#2 transferring dockerfile: 529B done
#2 DONE 0.3s

#3 [internal] load metadata for docker.io/library/python:3.12-slim-bookworm
#3 DONE 1.8s

#4 [internal] load .dockerignore
#4 transferring context: 107B done
#4 DONE 0.3s

#5 [internal] load build context
#5 transferring context: 81.89kB done
#5 DONE 0.4s

#6 [1/9] FROM docker.io/library/python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64
#6 resolve docker.io/library/python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64
#6 resolve docker.io/library/python:3.12-slim-bookworm@sha256:44cf1793efa7c7a476bb0f2cff81443f4a072c90c1cb127e2924933daa3e8c64 0.4s done
#6 DONE 0.5s

#7 [2/9] WORKDIR /app
#7 CACHED

#8 [3/9] COPY requirements.txt ./
#8 CACHED

#9 [4/9] RUN pip install --no-cache-dir --requirement requirements.txt
#9 CACHED

#10 [5/9] RUN useradd --system --uid 10001 --no-create-home erp
#10 CACHED

#11 [6/9] COPY manage.py ./
#11 CACHED

#12 [7/9] COPY config/ ./config/
#12 DONE 0.9s

#13 [8/9] COPY apps/ ./apps/
#13 DONE 1.0s

#14 [9/9] COPY tests/ ./tests/
#14 DONE 1.0s

#15 exporting to image
#15 exporting layers
#15 exporting layers 2.9s done
#15 exporting manifest sha256:08ebe551452d88c3b10d188f26bc1e42d4a8ee5f9b34ad190361bfcb0c8c744d
#15 exporting manifest sha256:08ebe551452d88c3b10d188f26bc1e42d4a8ee5f9b34ad190361bfcb0c8c744d 0.3s done
#15 exporting config sha256:500b472415394ce11354f1fcd9790742fd482c7a367bc673fa836378641997b4
#15 exporting config sha256:500b472415394ce11354f1fcd9790742fd482c7a367bc673fa836378641997b4 0.4s done
#15 exporting attestation manifest sha256:7b8a3df9e42cb062957f9ec5705b3f7b561a0aec53cb305ae3a14289530a2d0d
#15 exporting attestation manifest sha256:7b8a3df9e42cb062957f9ec5705b3f7b561a0aec53cb305ae3a14289530a2d0d 0.4s done
#15 exporting manifest list sha256:1eeb1603dcbe6c3f2f3e5e17d76576f69e11d184e017ab466411fb503f41cd61
#15 exporting manifest list sha256:1eeb1603dcbe6c3f2f3e5e17d76576f69e11d184e017ab466411fb503f41cd61 0.3s done
#15 naming to docker.io/library/erp-test-web:latest
#15 naming to docker.io/library/erp-test-web:latest 0.1s done
#15 unpacking to docker.io/library/erp-test-web:latest
#15 unpacking to docker.io/library/erp-test-web:latest 1.2s done
#15 DONE 6.0s

#16 resolving provenance for metadata file
#16 DONE 0.0s
 Image erp-test-web Built 
 Container erp-test-web-run-f0ffbe1e8322 Creating 
 Container erp-test-web-run-f0ffbe1e8322 Created 
Operations to perform:
  Apply all migrations: access_control, auth, contenttypes, contracts, projects, sessions
Running migrations:
  Applying projects.0001_initial... OK
  Applying projects.0002_worknode_parent_fk... OK
  Applying contenttypes.0001_initial... OK
  Applying auth.0001_initial... OK
  Applying access_control.0001_initial... OK
  Applying contenttypes.0002_remove_content_type_name... OK
  Applying auth.0002_alter_permission_name_max_length... OK
  Applying auth.0003_alter_user_email_max_length... OK
  Applying auth.0004_alter_user_username_opts... OK
  Applying auth.0005_alter_user_last_login_null... OK
  Applying auth.0006_require_contenttypes_0002... OK
  Applying auth.0007_alter_validators_add_error_messages... OK
  Applying auth.0008_alter_user_username_max_length... OK
  Applying auth.0009_alter_user_last_name_max_length... OK
  Applying auth.0010_alter_group_name_max_length... OK
  Applying auth.0011_update_proxy_permissions... OK
  Applying auth.0012_alter_user_first_name_max_length... OK
  Applying contracts.0001_initial... OK
  Applying sessions.0001_initial... OK
 Container erp-test-web-run-551bc208347a Creating 
 Container erp-test-web-run-551bc208347a Created 
System check identified no issues (0 silenced).
 Container erp-test-web-run-7fdec5bda722 Creating 
 Container erp-test-web-run-7fdec5bda722 Created 
No changes detected
 Container erp-test-web-run-43f71017f054 Creating 
 Container erp-test-web-run-43f71017f054 Created 
Creating test database for alias 'default' ('test_s104_fresh_cd572bafb30990c8')...
Found 30 test(s).
Operations to perform:
  Apply all migrations: access_control, auth, contenttypes, contracts, projects, sessions
Running migrations:
  Applying projects.0001_initial... OK
  Applying projects.0002_worknode_parent_fk... OK
  Applying contenttypes.0001_initial... OK
  Applying auth.0001_initial... OK
  Applying access_control.0001_initial... OK
  Applying contenttypes.0002_remove_content_type_name... OK
  Applying auth.0002_alter_permission_name_max_length... OK
  Applying auth.0003_alter_user_email_max_length... OK
  Applying auth.0004_alter_user_username_opts... OK
  Applying auth.0005_alter_user_last_login_null... OK
  Applying auth.0006_require_contenttypes_0002... OK
  Applying auth.0007_alter_validators_add_error_messages... OK
  Applying auth.0008_alter_user_username_max_length... OK
  Applying auth.0009_alter_user_last_name_max_length... OK
  Applying auth.0010_alter_group_name_max_length... OK
  Applying auth.0011_update_proxy_permissions... OK
  Applying auth.0012_alter_user_first_name_max_length... OK
  Applying contracts.0001_initial... OK
  Applying sessions.0001_initial... OK
System check identified no issues (0 silenced).
test_health_uses_the_configured_database (tests.test_health.HealthTests.test_health_uses_the_configured_database) ... ok
test_approval_preserves_two_rows_and_blocks_changes_to_history (tests.test_s1_03_contracts.ContractSchemaTests.test_approval_preserves_two_rows_and_blocks_changes_to_history) ... ok
test_postgresql_rejects_invalid_pricing_and_duplicate_codes (tests.test_s1_03_contracts.ContractSchemaTests.test_postgresql_rejects_invalid_pricing_and_duplicate_codes) ... ok
test_pricing_methods_preserve_their_independent_values (tests.test_s1_03_contracts.ContractSchemaTests.test_pricing_methods_preserve_their_independent_values) ... ok
test_project_has_only_one_owner_contract (tests.test_s1_03_contracts.ContractSchemaTests.test_project_has_only_one_owner_contract) ... ok
test_archiving_retains_protected_references (tests.test_s1_03_projects.ProjectSchemaTests.test_archiving_retains_protected_references) ... ok
test_fixture_contains_both_projects_and_optional_activity_levels (tests.test_s1_03_projects.ProjectSchemaTests.test_fixture_contains_both_projects_and_optional_activity_levels) ... ok
test_postgresql_contains_the_parent_target_fk_and_party_indexes (tests.test_s1_03_projects.ProjectSchemaTests.test_postgresql_contains_the_parent_target_fk_and_party_indexes) ... ok
test_postgresql_rejects_invalid_tree_writes (tests.test_s1_03_projects.ProjectSchemaTests.test_postgresql_rejects_invalid_tree_writes) ... ok
test_second_company_rejected_and_external_parties_remain_distinct (tests.test_s1_03_projects.ProjectSchemaTests.test_second_company_rejected_and_external_parties_remain_distinct) ... ok
test_account_lists_search_and_details_are_scoped (tests.test_s1_04_access_http.ProjectAccessHTTPTests.test_account_lists_search_and_details_are_scoped) ... ok
test_approval_requires_correct_project_and_role_and_csrf (tests.test_s1_04_access_http.ProjectAccessHTTPTests.test_approval_requires_correct_project_and_role_and_csrf) ... ok
test_cross_scope_and_missing_have_same_404_for_http_and_htmx (tests.test_s1_04_access_http.ProjectAccessHTTPTests.test_cross_scope_and_missing_have_same_404_for_http_and_htmx) ... ok
test_external_anonymous_and_disabled_accounts_cannot_read (tests.test_s1_04_access_http.ProjectAccessHTTPTests.test_external_anonymous_and_disabled_accounts_cannot_read) ... ok
test_open_session_observes_membership_revocation (tests.test_s1_04_access_http.ProjectAccessHTTPTests.test_open_session_observes_membership_revocation) ... ok
test_invalid_role_and_duplicate_role_are_rejected_but_another_role_is_allowed (tests.test_s1_04_access_models.ProjectAccessSchemaTests.test_invalid_role_and_duplicate_role_are_rejected_but_another_role_is_allowed) ... ok
test_postgresql_accepts_active_and_revoked_membership (tests.test_s1_04_access_models.ProjectAccessSchemaTests.test_postgresql_accepts_active_and_revoked_membership) ... ok
test_postgresql_has_expected_unique_check_and_foreign_keys (tests.test_s1_04_access_models.ProjectAccessSchemaTests.test_postgresql_has_expected_unique_check_and_foreign_keys) ... ok
test_postgresql_rejects_each_invalid_revocation_state (tests.test_s1_04_access_models.ProjectAccessSchemaTests.test_postgresql_rejects_each_invalid_revocation_state) ... ok
test_protects_every_membership_reference (tests.test_s1_04_access_models.ProjectAccessSchemaTests.test_protects_every_membership_reference) ... ok
test_protects_the_event_membership_and_the_separate_revocation_actor (tests.test_s1_04_access_models.ProjectAccessSchemaTests.test_protects_the_event_membership_and_the_separate_revocation_actor) ... ok
test_access_and_documentation_do_not_follow_superuser_or_an_unrelated_permission (tests.test_s1_04_access_policy.ProjectAccessPolicyTests.test_access_and_documentation_do_not_follow_superuser_or_an_unrelated_permission) ... ok
test_actions_use_roles_only_within_the_same_project (tests.test_s1_04_access_policy.ProjectAccessPolicyTests.test_actions_use_roles_only_within_the_same_project) ... ok
test_event_failure_rolls_back_membership_writes (tests.test_s1_04_access_policy.ProjectAccessPolicyTests.test_event_failure_rolls_back_membership_writes) ... ok
test_grant_and_revoke_preserve_actors_and_do_not_duplicate_events (tests.test_s1_04_access_policy.ProjectAccessPolicyTests.test_grant_and_revoke_preserve_actors_and_do_not_duplicate_events) ... ok
test_project_manager_has_no_implicit_membership_management (tests.test_s1_04_access_policy.ProjectAccessPolicyTests.test_project_manager_has_no_implicit_membership_management) ... ok
test_projects_and_nested_nodes_use_memberships (tests.test_s1_04_access_policy.ProjectAccessPolicyTests.test_projects_and_nested_nodes_use_memberships) ... ok
test_viewer_requires_explicit_attachment_and_print_permissions_within_scope (tests.test_s1_04_access_policy.ProjectAccessPolicyTests.test_viewer_requires_explicit_attachment_and_print_permissions_within_scope) ... ok
test_database_failure_returns_a_generic_response (tests.test_health.UnavailableDatabaseTests.test_database_failure_returns_a_generic_response) ... ok
test_all_probe_paths_are_absent_from_the_normal_urlconf (tests.test_s1_04_access_http.ProjectAccessTestURLIsolationTests.test_all_probe_paths_are_absent_from_the_normal_urlconf) ... ok

----------------------------------------------------------------------
Ran 30 tests in 8.503s

OK
Destroying test database for alias 'default' ('test_s104_fresh_cd572bafb30990c8')...
S1-04_FRESH_SCHEMA_AND_TESTS_OK
 Container erp-test-web-run-83afeaac0e4e Creating 
 Container erp-test-web-run-83afeaac0e4e Created 
Operations to perform:
  Apply all migrations: auth
Running migrations:
  Applying contenttypes.0001_initial... OK
  Applying auth.0001_initial... OK
  Applying contenttypes.0002_remove_content_type_name... OK
  Applying auth.0002_alter_permission_name_max_length... OK
  Applying auth.0003_alter_user_email_max_length... OK
  Applying auth.0004_alter_user_username_opts... OK
  Applying auth.0005_alter_user_last_login_null... OK
  Applying auth.0006_require_contenttypes_0002... OK
  Applying auth.0007_alter_validators_add_error_messages... OK
  Applying auth.0008_alter_user_username_max_length... OK
  Applying auth.0009_alter_user_last_name_max_length... OK
  Applying auth.0010_alter_group_name_max_length... OK
  Applying auth.0011_update_proxy_permissions... OK
  Applying auth.0012_alter_user_first_name_max_length... OK
 Container erp-test-web-run-f50cf75cf692 Creating 
 Container erp-test-web-run-f50cf75cf692 Created 
Operations to perform:
  Apply all migrations: sessions
Running migrations:
  Applying sessions.0001_initial... OK
 Container erp-test-web-run-61b22fb528e6 Creating 
 Container erp-test-web-run-61b22fb528e6 Created 
Operations to perform:
  Target specific migration: 0002_worknode_parent_fk, from projects
Running migrations:
  Applying projects.0001_initial... OK
  Applying projects.0002_worknode_parent_fk... OK
 Container erp-test-web-run-9aae0df1933f Creating 
 Container erp-test-web-run-9aae0df1933f Created 
Operations to perform:
  Target specific migration: 0001_initial, from contracts
Running migrations:
  Applying contracts.0001_initial... OK
 Container erp-test-web-run-31b81fd19bb7 Creating 
 Container erp-test-web-run-31b81fd19bb7 Created 
Installed 21 object(s) from 1 fixture(s)
 Container erp-test-web-run-826ef564cc88 Creating 
 Container erp-test-web-run-826ef564cc88 Created 
Operations to perform:
  Apply all migrations: access_control, auth, contenttypes, contracts, projects, sessions
Running migrations:
  Applying access_control.0001_initial... OK
S1-04_BASELINE_UPGRADE_AND_DATA_OK
 Container erp-test-web-run-646bc2683fdf Creating 
 Container erp-test-web-run-646bc2683fdf Created 
Operations to perform:
  Unapply all migrations: access_control
Running migrations:
  Rendering model states... DONE
  Unapplying access_control.0001_initial... OK
 Container erp-test-web-run-56f274c5f69a Creating 
 Container erp-test-web-run-56f274c5f69a Created 
Operations to perform:
  Apply all migrations: access_control, auth, contenttypes, contracts, projects, sessions
Running migrations:
  Applying access_control.0001_initial... OK
S1-04_ROLLBACK_REAPPLY_AND_DATA_OK
 Container erp-test-web-run-e0f51c167c3e Creating 
 Container erp-test-web-run-e0f51c167c3e Created 
S1-04_HEALTHZ_HTTP_200
## s1/04-project-access
S1-04_ACCEPT_OK
```
