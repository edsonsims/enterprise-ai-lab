"""Real SQLite authorization, atomic commands, isolation and recovery boundaries."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from enterprise_ai_lab.admin.auth import AdminPrincipal
from enterprise_ai_lab.admin.core import (
    DEFAULT_CONFIG,
    AdminError,
    AdminStore,
    _failure,
    canonical,
)
from enterprise_ai_lab.mcp_gateway.store import ReadOnlyStore, initialize_fixture

SCOPES = ("admin:read", "admin:configure", "admin:audit")


@contextmanager
def connection(path, **kwargs):
    db = sqlite3.connect(path, **kwargs)
    try:
        with db:
            yield db
    finally:
        db.close()


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_path = self.root / "source.sqlite"
        initialize_fixture(self.source_path)
        self.source = ReadOnlyStore(self.source_path)
        self.path = self.root / "configuration.sqlite"
        self.now = 1000
        self.store = AdminStore(self.path, self.source, clock=lambda: self.now)
        self.alpha = AdminPrincipal(
            "alpha", ("config_admin", "reader"), "admin-alpha", expires_at=2000
        )
        self.beta = AdminPrincipal(
            "beta", ("config_admin", "reader"), "admin-beta", expires_at=2000
        )
        self.auditor = AdminPrincipal(
            "alpha", ("config_auditor", "reader"), "auditor-alpha", expires_at=2000
        )

    def tearDown(self):
        self.store.close()
        self.source.close()
        self.temp.cleanup()

    def update(self, identifier="change-one", revision=0, size=1, status="closed"):
        return {
            "command_id": identifier,
            "expected_revision": revision,
            "action": "update",
            "config": {"work_order_page_size": size, "default_status": status},
        }

    def error(self, code, function, *args):
        with self.assertRaises(AdminError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_default_view_and_changes_use_real_tenant_role_prefilter(self):
        initial = self.store.work_orders(self.alpha, SCOPES)
        self.assertEqual(initial["configuration"]["revision"], 0)
        self.assertEqual([x["work_order_id"] for x in initial["items"]], ["WO-0001"])
        self.store.apply(self.alpha, SCOPES, self.update())
        changed = self.store.work_orders(self.alpha, SCOPES)
        self.assertEqual([x["work_order_id"] for x in changed["items"]], ["WO-0003"])
        self.assertEqual(changed["next_after"], "WO-0003")
        self.assertTrue(all(x["tenant_id"] == "alpha" for x in changed["items"]))
        # Alpha analyst-only open rows4/7 never fill the reader page.
        self.store.apply(self.alpha, SCOPES, self.update("open-ten", 1, 10, "open"))
        self.assertEqual(
            [
                x["work_order_id"]
                for x in self.store.work_orders(self.alpha, SCOPES)["items"]
            ],
            ["WO-0001"],
        )

    def test_configuration_receipt_and_audit_persist_across_reopen(self):
        receipt = self.store.apply(self.alpha, SCOPES, self.update())
        self.store.close()
        self.store = AdminStore(self.path, self.source, clock=lambda: self.now)
        self.assertEqual(self.store.apply(self.alpha, SCOPES, self.update()), receipt)
        self.assertEqual(self.store.snapshot(self.alpha, SCOPES)["revision"], 1)
        audit = self.store.audit(self.alpha, SCOPES)
        self.assertEqual(audit["total"], 1)
        self.assertEqual(audit["events"][0]["audit_hash"], receipt["audit_hash"])
        self.assertEqual(
            audit["events"][0]["configuration_sha256"], receipt["configuration_sha256"]
        )

    def test_identical_replay_after_newer_revision_does_not_restore_old_settings(self):
        one = self.store.apply(self.alpha, SCOPES, self.update())
        self.store.apply(
            self.alpha, SCOPES, self.update("change-two", 1, 8, "in_progress")
        )
        self.assertEqual(self.store.apply(self.alpha, SCOPES, self.update()), one)
        self.assertEqual(self.store.snapshot(self.alpha, SCOPES)["revision"], 2)
        self.assertEqual(
            self.store.snapshot(self.alpha, SCOPES)["config"]["work_order_page_size"], 8
        )
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 2)

    def test_same_id_cannot_change_actor_valid_body_or_expected_revision(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        alternate = AdminPrincipal(
            "alpha", ("config_admin", "reader"), "another-admin", expires_at=2000
        )
        for principal, body in [
            (alternate, self.update()),
            (self.alpha, self.update(size=2)),
            (self.alpha, self.update(revision=1)),
        ]:
            with self.subTest(subject=principal.subject, body=body):
                self.error("conflict", self.store.apply, principal, SCOPES, body)
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 1)

    def test_tenant_local_command_ids_revision_and_audit_are_isolated(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        self.assertEqual(self.store.snapshot(self.beta, SCOPES)["revision"], 0)
        self.store.apply(self.beta, SCOPES, self.update(size=7, status="in_progress"))
        self.assertEqual(
            self.store.snapshot(self.alpha, SCOPES)["config"]["work_order_page_size"], 1
        )
        self.assertEqual(
            self.store.snapshot(self.beta, SCOPES)["config"]["work_order_page_size"], 7
        )
        for identity in [self.alpha, self.beta]:
            audit = self.store.audit(identity, SCOPES)
            self.assertEqual(audit["total"], 1)
            self.assertTrue(
                all(x["tenant_id"] == identity.tenant_id for x in audit["events"])
            )
            view = self.store.work_orders(identity, SCOPES)
            self.assertTrue(
                all(x["tenant_id"] == identity.tenant_id for x in view["items"])
            )

    def test_role_and_scope_are_both_required_including_exact_replay(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        self.error("forbidden", self.store.apply, self.auditor, SCOPES, self.update())
        self.error(
            "forbidden", self.store.apply, self.alpha, ("admin:read",), self.update()
        )
        reader = AdminPrincipal("alpha", ("reader",), "reader-alpha", expires_at=2000)
        self.error("forbidden", self.store.snapshot, reader, SCOPES)
        self.error("forbidden", self.store.audit, self.alpha, ("admin:read",))
        self.assertEqual(
            self.store.snapshot(self.auditor, ("admin:read",))["revision"], 1
        )
        self.assertEqual(self.store.audit(self.auditor, ("admin:audit",))["total"], 1)

    def test_admin_role_does_not_implicitly_grant_work_order_row_permission(self):
        admin = AdminPrincipal(
            "alpha", ("config_admin",), "noreader-admin", expires_at=2000
        )
        self.assertEqual(self.store.snapshot(admin, SCOPES)["revision"], 0)
        self.error("forbidden", self.store.work_orders, admin, SCOPES)

    def test_expired_and_missing_expiry_are_rejected_before_read_or_replay(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        self.now = 2000
        self.error("unauthenticated", self.store.snapshot, self.alpha, SCOPES)
        self.error(
            "unauthenticated", self.store.apply, self.alpha, SCOPES, self.update()
        )
        missing = AdminPrincipal("alpha", ("config_admin",), "no-expiry")
        self.error("unauthenticated", self.store.snapshot, missing, SCOPES)

    def test_expiry_during_before_commit_rolls_back_every_record(self):
        def expire(phase):
            if phase == "before_commit":
                self.now = 2000

        self.store.fault_hook = expire
        self.error(
            "unauthenticated", self.store.apply, self.alpha, SCOPES, self.update()
        )
        self.now = 1000
        self.store.fault_hook = None
        self.assertEqual(self.store.snapshot(self.alpha, SCOPES)["revision"], 0)
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 0)
        self.assertEqual(
            self.store.apply(self.alpha, SCOPES, self.update())["revision"], 1
        )

    def test_before_commit_failure_rolls_back_then_same_id_succeeds_once(self):
        def fail(phase):
            if phase == "before_commit":
                raise OSError("private diagnostic must never become a public error")

        self.store.fault_hook = fail
        self.error(
            "storage_unavailable", self.store.apply, self.alpha, SCOPES, self.update()
        )
        self.store.fault_hook = None
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 0)
        self.assertEqual(
            self.store.apply(self.alpha, SCOPES, self.update())["revision"], 1
        )

    def test_lost_post_commit_response_preserves_exact_receipt(self):
        def fail(phase):
            if phase == "after_commit":
                raise OSError("simulated lost local response")

        self.store.fault_hook = fail
        self.error(
            "storage_unavailable", self.store.apply, self.alpha, SCOPES, self.update()
        )
        self.store.fault_hook = None
        self.assertEqual(self.store.snapshot(self.alpha, SCOPES)["revision"], 1)
        receipt = self.store.apply(self.alpha, SCOPES, self.update())
        self.assertEqual(self.store.apply(self.alpha, SCOPES, self.update()), receipt)
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 1)

    def test_rollback_is_new_revision_and_preserves_previous_receipts(self):
        one = self.store.apply(self.alpha, SCOPES, self.update())
        self.store.apply(self.alpha, SCOPES, self.update("two", 1, 4, "in_progress"))
        rollback = {
            "action": "rollback",
            "command_id": "undo",
            "expected_revision": 2,
            "target_revision": 0,
        }
        result = self.store.apply(self.alpha, SCOPES, rollback)
        self.assertEqual(result["revision"], 3)
        self.assertEqual(
            self.store.snapshot(self.alpha, SCOPES)["config"], DEFAULT_CONFIG
        )
        self.assertEqual(self.store.apply(self.alpha, SCOPES, rollback), result)
        self.assertEqual(self.store.apply(self.alpha, SCOPES, self.update()), one)
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 3)
        self.error(
            "conflict",
            self.store.apply,
            self.beta,
            SCOPES,
            {
                "action": "rollback",
                "command_id": "foreign",
                "expected_revision": 0,
                "target_revision": 1,
            },
        )

    def test_stale_and_unknown_rollback_do_not_allocate_revision_or_receipt(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        self.error(
            "conflict", self.store.apply, self.alpha, SCOPES, self.update("stale")
        )
        self.error(
            "conflict",
            self.store.apply,
            self.alpha,
            SCOPES,
            {
                "action": "rollback",
                "command_id": "unknown",
                "expected_revision": 1,
                "target_revision": 99,
            },
        )
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 1)

    def test_nonblocking_admission_and_real_sqlite_busy_leave_no_effect(self):
        self.store.lock.acquire()
        try:
            started = time.monotonic()
            self.error("busy", self.store.apply, self.alpha, SCOPES, self.update())
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            self.store.lock.release()
        with connection(self.path, isolation_level=None) as outer:
            outer.execute("BEGIN IMMEDIATE")
            self.error("busy", self.store.apply, self.alpha, SCOPES, self.update())
            outer.execute("ROLLBACK")
        self.assertEqual(self.store.snapshot(self.alpha, SCOPES)["revision"], 0)

    def test_legacy_exact_busy_fallback_does_not_reflect_arbitrary_text(self):
        for text in ("database is locked", "database table is locked"):
            self.assertEqual(_failure(sqlite3.OperationalError(text)).code, "busy")
        for exc in (
            sqlite3.OperationalError("private unknown failure"),
            RuntimeError("database is locked"),
        ):
            mapped = _failure(exc)
            self.assertEqual(mapped.code, "storage_unavailable")
            self.assertNotIn(str(exc), mapped.safe_message)

    def test_two_real_connections_compare_and_swap_same_revision(self):
        other = AdminStore(self.path, self.source, clock=lambda: self.now)
        barrier = threading.Barrier(2)
        results = []

        def change(store, identifier):
            barrier.wait()
            try:
                results.append(store.apply(self.alpha, SCOPES, self.update(identifier)))
            except AdminError as exc:
                results.append(exc.code)

        try:
            threads = [
                threading.Thread(target=change, args=(self.store, "first")),
                threading.Thread(target=change, args=(other, "second")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len([x for x in results if isinstance(x, dict)]), 1)
            self.assertIn(
                next(x for x in results if isinstance(x, str)), ("conflict", "busy")
            )
            self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 1)
        finally:
            other.close()

    def test_closing_or_constructing_store_does_not_drop_foreign_sqlite_lock(self):
        def child():
            code = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=.05,isolation_level=None)\ntry:\n c.execute('BEGIN IMMEDIATE');print('admitted');c.execute('ROLLBACK')\nexcept sqlite3.OperationalError:\n print('busy')\nfinally:\n c.close()"
            proc = subprocess.run(
                [sys.executable, "-c", code, str(self.path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        outer = sqlite3.connect(self.path, isolation_level=None)
        try:
            outer.execute("BEGIN IMMEDIATE")
            self.assertEqual(child(), "busy")
            other = AdminStore(self.path, self.source, clock=lambda: self.now)
            try:
                self.assertEqual(child(), "busy")
            finally:
                other.close()
            self.assertTrue(outer.in_transaction)
            self.assertEqual(child(), "busy")
            self.store.close()
            self.assertTrue(outer.in_transaction)
            self.assertEqual(child(), "busy")
            outer.execute("ROLLBACK")
            self.assertEqual(child(), "admitted")
        finally:
            outer.close()

    def test_bool_receipt_tampering_is_not_accepted_as_integer_one(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        with connection(self.path) as db:
            row = json.loads(
                db.execute("SELECT receipt_json FROM changes").fetchone()[0]
            )
            row["revision"] = row["audit_sequence"] = True
            db.execute("UPDATE changes SET receipt_json=?", (canonical(row),))
        self.error("storage_unavailable", self.store.snapshot, self.alpha, SCOPES)
        self.error(
            "storage_unavailable", self.store.apply, self.alpha, SCOPES, self.update()
        )

    def test_deleted_tail_corruption_is_detected_from_atomic_metadata_tip(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        with connection(self.path) as db:
            db.execute("DELETE FROM changes")
        self.error("storage_unavailable", self.store.snapshot, self.alpha, SCOPES)
        self.error("storage_unavailable", self.store.audit, self.auditor, SCOPES)

    def test_corrupt_config_and_extra_schema_objects_fail_closed(self):
        self.store.apply(self.alpha, SCOPES, self.update())
        with connection(self.path) as db:
            db.execute("UPDATE changes SET config_json=?", (canonical(DEFAULT_CONFIG),))
        self.error("storage_unavailable", self.store.work_orders, self.alpha, SCOPES)
        # Fresh independent file: an extra view cannot become an API query path.
        extra = AdminStore(
            self.root / "extra.sqlite", self.source, clock=lambda: self.now
        )
        try:
            with connection(extra.path) as db:
                db.execute("CREATE VIEW hidden AS SELECT * FROM changes")
            self.error("storage_unavailable", extra.snapshot, self.alpha, SCOPES)
        finally:
            extra.close()

    def test_private_file_mode_inode_replacement_and_hardlink_fail_closed(self):
        os.chmod(self.path, 0o644)
        self.error("storage_unavailable", self.store.snapshot, self.alpha, SCOPES)
        os.chmod(self.path, 0o600)
        link = self.root / "other-link"
        os.link(self.path, link)
        self.error("storage_unavailable", self.store.snapshot, self.alpha, SCOPES)
        link.unlink()
        original = self.root / "original.sqlite"
        self.path.rename(original)
        self.path.write_bytes(original.read_bytes())
        self.path.chmod(0o600)
        self.error("storage_unavailable", self.store.snapshot, self.alpha, SCOPES)

    def test_main_and_sidecar_aliases_refused_before_source_changes(self):
        initial = self.source_path.read_bytes()
        self.error("storage_unavailable", AdminStore, self.source_path, self.source)
        alias = self.root / "alias.sqlite"
        os.link(self.source_path, alias)
        self.error("storage_unavailable", AdminStore, alias, self.source)
        side = Path(str(self.path) + "-journal")
        os.link(self.source_path, side)
        try:
            self.error(
                "storage_unavailable",
                self.store.apply,
                self.alpha,
                SCOPES,
                self.update(),
            )
            self.assertEqual(self.source_path.read_bytes(), initial)
        finally:
            side.unlink()

    def test_symlink_and_nonprivate_parent_refused(self):
        link = self.root / "symlink.sqlite"
        link.symlink_to(self.path)
        self.error("storage_unavailable", AdminStore, link, self.source)
        public = self.root / "public"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        self.error(
            "storage_unavailable",
            AdminStore,
            public / "configuration.sqlite",
            self.source,
        )

    def test_audit_projection_bounded_to_last_twenty_own_tenant_events(self):
        for i in range(23):
            self.store.apply(
                self.alpha, SCOPES, self.update("alpha-" + str(i), i, 1 + i % 10)
            )
        self.store.apply(self.beta, SCOPES, self.update())
        audit = self.store.audit(self.alpha, SCOPES)
        self.assertEqual(audit["total"], 23)
        self.assertEqual(len(audit["events"]), 20)
        self.assertEqual(audit["events"][0]["revision"], 4)
        self.assertTrue(all(x["tenant_id"] == "alpha" for x in audit["events"]))

    def test_invalid_commands_do_not_mutate_input_or_storage(self):
        valid = self.update()
        bad = [
            None,
            [],
            {},
            True,
            valid | {"tenant_id": "beta"},
            valid | {"action": "restart"},
            valid | {"command_id": ""},
            valid | {"command_id": "a" * 65},
            valid | {"expected_revision": True},
            valid | {"expected_revision": -1},
            valid
            | {"config": {"work_order_page_size": True, "default_status": "open"}},
            valid | {"config": {"work_order_page_size": 11, "default_status": "open"}},
            valid
            | {"config": {"work_order_page_size": 1, "default_status": "anything"}},
            valid | {"config": dict(DEFAULT_CONFIG) | {"roles": ["analyst"]}},
            {
                "action": "rollback",
                "command_id": "bad",
                "expected_revision": 0,
                "target_revision": True,
            },
        ]
        for body in bad:
            with self.subTest(body=body):
                before = copy.deepcopy(body)
                self.error(
                    "invalid_request", self.store.apply, self.alpha, SCOPES, body
                )
                self.assertEqual(body, before)
        before = copy.deepcopy(valid)
        self.store.apply(self.alpha, SCOPES, valid)
        self.assertEqual(valid, before)
        self.assertEqual(self.store.audit(self.alpha, SCOPES)["total"], 1)


if __name__ == "__main__":
    unittest.main()
