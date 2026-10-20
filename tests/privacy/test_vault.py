"""Actual SQLite, file modes, contention and transactions; synthetic values only."""
import concurrent.futures
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from enterprise_ai_lab.privacy_proxy import PrivacyError, Vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "vault.sqlite"
        self.now = [1000]
        self.vault = Vault(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.vault.close()
        self.directory.cleanup()

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(PrivacyError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        return caught.exception

    def count(self, table):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_round_trip_only_substrings_stored_and_repeated_occurrences_unique(self):
        text = "🙂 Mail a@example.invalid twice: a@example.invalid; number +1 416 555 0123. End."
        result = self.vault.redact("alpha", "worker", text)
        self.assertEqual(result["expires_at"], 1120)
        self.assertEqual(len(result["detections"]), 3)
        tokens = [x["token"] for x in result["detections"]]
        self.assertEqual(len(set(tokens)), 3)
        self.assertNotIn("a@example.invalid", result["redacted_text"])
        self.assertNotIn("+1 416 555 0123", result["redacted_text"])
        self.assertNotIn(result["context_id"], "".join(tokens))
        for _ in range(2):
            self.assertEqual(self.vault.restore("alpha", "worker", result["context_id"], result["redacted_text"]),
                             {"restored_text": text})
        with closing(sqlite3.connect(self.path)) as connection:
            values = [x[0] for x in connection.execute("SELECT original FROM mappings")]
        self.assertEqual(sorted(values), sorted(["a@example.invalid", "a@example.invalid", "+1 416 555 0123"]))
        self.assertNotIn(text.encode(), self.path.read_bytes())

    def test_maps_survive_reopen_but_expiry_is_exact_and_purges(self):
        result = self.vault.redact("alpha", "worker", "a@example.invalid")
        self.vault.close()
        self.vault = Vault(self.path, clock=lambda: self.now[0])
        self.now[0] = 1119
        self.assertEqual(self.vault.restore("alpha", "worker", result["context_id"], result["redacted_text"]),
                         {"restored_text": "a@example.invalid"})
        self.now[0] = 1120
        self.error("context_unavailable", self.vault.restore, "alpha", "worker", result["context_id"], result["redacted_text"])
        self.assertEqual(self.count("contexts"), 0)
        self.assertEqual(self.count("mappings"), 0)
        self.assertEqual(self.count("ids"), 2)

    def test_wrong_scope_unknown_context_and_cross_context_tokens_fail(self):
        a = self.vault.redact("alpha", "worker", "a@example.invalid")
        b = self.vault.redact("alpha", "worker", "b@example.invalid")
        for tenant, client in [("beta", "worker"), ("alpha", "other")]:
            self.error("context_unavailable", self.vault.restore, tenant, client, a["context_id"], a["redacted_text"])
        self.error("context_unavailable", self.vault.restore, "alpha", "worker", "0"*32, a["redacted_text"])
        self.error("invalid_token", self.vault.restore, "alpha", "worker", a["context_id"], b["redacted_text"])
        self.assertEqual(self.count("mappings"), 2)

    def test_unknown_or_malformed_token_is_atomic_and_does_not_destroy_maps(self):
        a = self.vault.redact("alpha", "worker", "a@example.invalid")
        for token in ["[[PII:bad]]", "[[PII:" + "0"*32 + "]]", "[[PII:" + "A"*32 + "]]", "[[PII:"]:
            error = self.error("invalid_token", self.vault.restore, "alpha", "worker", a["context_id"], a["redacted_text"]+token)
            self.assertNotIn("a@example.invalid", repr(error))
        self.assertEqual(self.vault.restore("alpha", "worker", a["context_id"], a["redacted_text"])["restored_text"], "a@example.invalid")
        self.error("invalid_input", self.vault.redact, "alpha", "worker", a["redacted_text"])

    def test_rng_collision_retry_and_global_context_token_uniqueness(self):
        values = iter([1, 2, 1, 2, 3, 2, 1, 4])
        self.vault.random_hex = lambda: f"{next(values):032x}"
        a = self.vault.redact("alpha", "worker", "a@example.invalid")
        b = self.vault.redact("alpha", "worker", "b@example.invalid")
        self.assertEqual((a["context_id"], b["context_id"]), (f"{1:032x}", f"{3:032x}"))
        self.assertEqual(self.count("ids"), 4)

    def test_collision_exhaustion_rolls_back_entire_new_context(self):
        values = iter([f"{1:032x}", f"{2:032x}"] + [f"{2:032x}"]*8)
        self.vault.random_hex = lambda: next(values)
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "a@example.invalid b@example.invalid")
        for table in ("contexts", "mappings", "ids"):
            self.assertEqual(self.count(table), 0)
        self.assertEqual(self.vault.audit_events(), [{"sequence": 1, "action": "redact", "outcome": "capacity", "count": 0}])

    def test_expired_ids_cannot_be_reused(self):
        self.vault.random_hex = lambda: f"{1:032x}"
        a = self.vault.redact("alpha", "worker", "no matches")
        self.now[0] = a["expires_at"]
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "again")
        self.assertEqual(self.count("contexts"), 0)
        self.assertEqual(self.count("ids"), 1)

    def test_invalid_rng_and_clock_are_finite(self):
        self.vault.random_hex = lambda: "secret@example.invalid"
        self.error("storage_unavailable", self.vault.redact, "alpha", "worker", "plain")
        self.vault.random_hex = lambda: f"{2:032x}"
        for value in [True, math.inf, math.nan, -1, "secret"]:
            self.now[0] = value
            self.error("storage_unavailable", self.vault.redact, "alpha", "worker", "plain")

    def test_unknown_sqlite_subclass_is_not_introspected(self):
        class Unknown(sqlite3.OperationalError):
            @property
            def sqlite_errorcode(self):
                raise RuntimeError("PRIVATE unexpected attribute")
        def fail():
            raise Unknown("PRIVATE unexpected database message")
        self.vault.random_hex = fail
        error = self.error("storage_unavailable", self.vault.redact, "alpha", "worker", "plain")
        self.assertNotIn("PRIVATE", repr(error))
        self.assertEqual(self.count("contexts"), 0)

    def test_context_mapping_output_and_audit_capacity_are_finite(self):
        self.vault.close()
        self.vault = Vault(self.path, max_contexts=1, max_mappings=1, clock=lambda: self.now[0])
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "a@example.invalid b@example.invalid")
        self.assertEqual(self.count("contexts"), 0)
        a = self.vault.redact("alpha", "worker", "a@example.invalid")
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "plain")
        self.assertEqual(self.vault.restore("alpha", "worker", a["context_id"], a["redacted_text"])["restored_text"], "a@example.invalid")
        self.error("capacity_exceeded", self.vault.audit_events, limit=1)
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "a@b.co " * 1000)

    def test_audit_full_rolls_back_new_context_and_restoration(self):
        self.vault.close()
        self.vault = Vault(self.path, max_audit_rows=1, clock=lambda: self.now[0])
        a = self.vault.redact("alpha", "worker", "a@example.invalid")
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "b@example.invalid")
        self.error("capacity_exceeded", self.vault.restore, "alpha", "worker", a["context_id"], a["redacted_text"])
        self.assertEqual((self.count("contexts"), self.count("mappings"), self.count("audit")), (1, 1, 1))

    def test_output_expansion_and_persistent_id_capacity(self):
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "a@b.co " * 1000)
        self.assertEqual(self.count("contexts"), 0)
        address = "a"*64 + "@" + "b"*60 + "." + "c"*60 + ".invalid"
        a = self.vault.redact("alpha", "worker", address)
        self.error("capacity_exceeded", self.vault.restore, "alpha", "worker", a["context_id"], a["redacted_text"]*200)
        self.vault.close()
        self.vault = Vault(self.path, max_ids=2, clock=lambda: self.now[0])
        self.error("capacity_exceeded", self.vault.redact, "alpha", "worker", "no matches")
        self.assertEqual(self.count("ids"), 2)

    def test_expiry_purge_is_bounded_and_commits_even_for_denied_action(self):
        self.vault.close()
        self.vault = Vault(self.path, purge_limit=2, clock=lambda: self.now[0])
        for _ in range(5):
            self.vault.redact("alpha", "worker", "a@example.invalid")
        self.now[0] = 1120
        self.error("invalid_scope", self.vault.redact, "bad\n", "worker", "no matches")
        self.assertEqual(self.count("contexts"), 3)
        self.assertEqual(self.count("mappings"), 3)
        self.error("invalid_scope", self.vault.redact, "bad\n", "worker", "no matches")
        self.assertEqual(self.count("contexts"), 1)
        self.assertEqual(self.count("ids"), 10)

    def test_actual_sqlite_abort_rolls_back_and_never_echoes_database_message(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TRIGGER fault BEFORE INSERT ON mappings WHEN NEW.kind='PHONE' "
                               "BEGIN SELECT RAISE(ABORT,'PRIVATE synthetic fault'); END")
        error = self.error("storage_unavailable", self.vault.redact, "alpha", "worker", "a@example.invalid +14165550123")
        self.assertNotIn("PRIVATE", repr(error))
        for table in ("contexts", "mappings", "ids"):
            self.assertEqual(self.count(table), 0)

    def test_actual_lock_busy_is_bounded_and_same_vault_recovers(self):
        blocker = sqlite3.connect(self.path, isolation_level=None)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            self.error("busy", self.vault.redact, "alpha", "worker", "a@example.invalid")
            self.assertLess(time.monotonic()-started, 2)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(len(self.vault.redact("alpha", "worker", "a@example.invalid")["detections"]), 1)

    def test_real_concurrent_connections_keep_scopes_and_unique_ids(self):
        second = Vault(self.path, clock=lambda: self.now[0])
        barrier = threading.Barrier(2)
        def work(vault, tenant):
            barrier.wait(timeout=3)
            return vault.redact(tenant, "worker", f"{tenant}@example.invalid")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                fa = executor.submit(work, self.vault, "alpha")
                fb = executor.submit(work, second, "beta")
                a, b = fa.result(timeout=5), fb.result(timeout=5)
            self.assertNotEqual(a["context_id"], b["context_id"])
            self.error("context_unavailable", self.vault.restore, "alpha", "worker", b["context_id"], b["redacted_text"])
            self.assertEqual(self.count("contexts"), 2)
        finally:
            second.close()

    def test_metadata_audit_has_no_scope_text_or_tokens(self):
        a = self.vault.redact("unique-tenant", "unique-client", "a@example.invalid")
        self.error("context_unavailable", self.vault.restore, "other", "unique-client", a["context_id"], a["redacted_text"])
        encoded = json.dumps(self.vault.audit_events())
        for value in ["a@example.invalid", "unique-tenant", "unique-client", a["context_id"], a["redacted_text"]]:
            self.assertNotIn(value, encoded)
        self.assertEqual([x["outcome"] for x in self.vault.audit_events()], ["success", "denied"])

    def test_foreign_database_and_corrupt_file_are_preserved(self):
        for name, raw in [("foreign", None), ("corrupt", b"not sqlite private bytes")]:
            path = self.root / name
            if raw is None:
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE important(value TEXT)")
            else:
                path.write_bytes(raw)
            path.chmod(0o600)
            before = path.read_bytes()
            self.error("storage_unavailable", Vault, path)
            self.assertEqual(path.read_bytes(), before)

    def test_symlink_hardlink_modes_and_sidecar_alias_refused(self):
        self.vault.close()
        original = self.path.read_bytes()
        symlink = self.root / "link.sqlite"
        symlink.symlink_to(self.path)
        self.error("storage_unavailable", Vault, symlink)
        hardlink = self.root / "hard.sqlite"
        os.link(self.path, hardlink)
        self.error("storage_unavailable", Vault, self.path)
        hardlink.unlink()
        self.path.chmod(0o644)
        self.error("storage_unavailable", Vault, self.path)
        self.path.chmod(0o600)
        journal = self.root / "vault.sqlite-journal"
        os.link(self.path, journal)
        self.error("storage_unavailable", Vault, self.path)
        journal.unlink()
        self.root.chmod(0o755)
        self.error("storage_unavailable", Vault, self.path)
        self.root.chmod(0o700)
        self.assertEqual(self.path.read_bytes(), original)

    def test_scope_config_and_closed_vault_errors(self):
        self.error("invalid_scope", self.vault.redact, "alpha\n", "worker", "plain")
        for option in [{"ttl_seconds": True}, {"ttl_seconds": 601}, {"max_contexts": 0}]:
            self.error("invalid_input", Vault, self.root/"unused", **option)
        self.vault.close()
        self.error("storage_unavailable", self.vault.redact, "alpha", "worker", "plain")


if __name__ == "__main__":
    unittest.main()
