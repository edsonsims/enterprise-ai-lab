"""Immutable synthetic replica admission and exact read-result validation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat

from jsonschema import Draft202012Validator

from ..mcp_gateway.audit import canonical
from ..mcp_gateway.server import _INPUTS, _output_schema
from ..mcp_gateway.service import _family, _inodes, _input_digest
from ..mcp_gateway.store import ReadOnlyStore, fixture_source, principal_for_profile

OPERATIONS = ('list_work_orders', 'get_work_order', 'get_asset_diagnostics')


class ReadFailure(ValueError):
    """Finite safe category plus attempt evidence; no original exception text."""
    def __init__(self, code, attempts=()):
        self.code, self.attempts = code, list(attempts)
        super().__init__(code)


@dataclass(frozen=True)
class Budget:
    total_seconds: float = 4.0
    attempt_seconds: float = 2.0
    max_attempts: int = 2

    def __post_init__(self):
        for value in (self.total_seconds, self.attempt_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not .01 <= value <= 30:
                raise ValueError('Finite work budgets between .01 and 30 seconds required')
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 2:
            raise ValueError('At most two attempts are allowed')


@dataclass(frozen=True)
class LocalEndpoint:
    name: str
    database: Path
    audit: Path

    def __post_init__(self):
        if self.name not in ('primary', 'secondary'):
            raise ValueError('Fixed primary/secondary names required')
        object.__setattr__(self, 'database', Path(self.database).absolute())
        object.__setattr__(self, 'audit', Path(self.audit).absolute())


class FixtureContract:
    """Trusted operator admission, not an authentication or hostile-file sandbox.

    Initial full logical inventories must equal the packaged synthetic fixture.
    Thereafter exact source byte hashes are sealed and checked before each try.
    Audits stay mutable and independent; their service-side fences are unchanged.
    """
    def __init__(self, endpoints, profile):
        self.endpoints = tuple(endpoints)
        if (not 1 <= len(self.endpoints) <= 2
                or not all(type(v) is LocalEndpoint for v in self.endpoints)
                or tuple(v.name for v in self.endpoints) != ('primary', 'secondary')[:len(self.endpoints)]):
            raise ValueError('One primary and at most one fixed secondary required')
        self.principal = principal_for_profile(profile)
        self.profile = profile
        self.fixture, self.fixture_sha256 = fixture_source()
        self.source_hashes = {}
        self._separation()
        for endpoint in self.endpoints:
            store = ReadOnlyStore(endpoint.database)
            try:
                # This trusted local admission reads the public synthetic inventory,
                # not a caller's unaudited enterprise query. No business tool is run.
                for table, grants, identity in (
                    ('work_orders', 'work_order_roles', 'work_order_id'),
                    ('asset_diagnostics', 'diagnostic_roles', 'asset_id'),
                ):
                    expected = sorted(({k: v for k, v in row.items() if k != 'allowed_roles'}
                                       for row in self.fixture[table]), key=canonical)
                    actual = sorted((dict(row) for row in store._connection.execute(f'SELECT * FROM {table}')), key=canonical)
                    if canonical(actual) != canonical(expected):
                        raise ReadFailure('provenance_mismatch')
                    expected_roles = sorted((row['tenant_id'], row[identity], role)
                                            for row in self.fixture[table] for role in row['allowed_roles'])
                    actual_roles = sorted(tuple(row) for row in store._connection.execute(f'SELECT * FROM {grants}'))
                    if actual_roles != expected_roles:
                        raise ReadFailure('provenance_mismatch')
            finally:
                store.close()
            self.source_hashes[endpoint.name] = self._hash(endpoint.database)

    @staticmethod
    def _hash(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 5_000_000:
                raise ReadFailure('provenance_mismatch')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(5_000_001)
            after = os.fstat(fd)
            if len(raw) != info.st_size or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ReadFailure('provenance_mismatch')
            return hashlib.sha256(raw).hexdigest()
        finally:
            os.close(fd)

    def _separation(self):
        sources = [_family(v.database) for v in self.endpoints]
        audits = [_family(v.audit) for v in self.endpoints]
        families = sources + audits
        try:
            for index, first in enumerate(families):
                for second in families[index + 1:]:
                    if first & second or _inodes(first) & _inodes(second):
                        raise ReadFailure('endpoint_alias')
        except OSError:
            raise ReadFailure('endpoint_alias') from None

    def before_attempt(self, endpoint):
        self._separation()
        try:
            if self._hash(endpoint.database) != self.source_hashes[endpoint.name]:
                raise ReadFailure('provenance_mismatch')
        except OSError:
            raise ReadFailure('provenance_mismatch') from None

    def expected_catalog(self):
        roles = self.principal.roles
        if not set(roles).intersection(('reader', 'analyst')):
            return []
        return list(OPERATIONS[:2]) + (list(OPERATIONS[2:]) if 'analyst' in roles else [])

    def validate_command(self, operation, arguments):
        if type(operation) is not str or operation not in OPERATIONS or type(arguments) is not dict:
            raise ReadFailure('invalid_request')
        _, _, error = _input_digest(operation, arguments)
        if error is not None:
            raise ReadFailure('invalid_request')
        # Snapshot caller-owned nested containers before the first await.
        return json.loads(canonical(arguments))

    def validate_result(self, operation, arguments, result):
        # Both MCP representations must agree; error-shaped results are terminal
        # even if a server accidentally attaches success-shaped structured data.
        if result.is_error is True:
            raise ReadFailure('tool_error')
        if result.is_error is not False or len(result.content) != 1 or result.content[0].type != 'text':
            raise ReadFailure('invalid_payload')
        try:
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError()
                    obj[key] = value
                return obj
            text = result.content[0].text
            if len(text.encode('utf-8')) > 64_000:
                raise ValueError()
            decoded = json.loads(text, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if canonical(decoded) != canonical(result.structured_content):
                raise ValueError()
            Draft202012Validator(_output_schema(operation)).validate(decoded)
        except Exception:
            raise ReadFailure('invalid_payload') from None
        try:
            Draft202012Validator(_INPUTS[operation]).validate(arguments)
            expected = self._expected(operation, arguments)
        except Exception:
            raise ReadFailure('provenance_mismatch') from None
        if canonical(decoded) != canonical(expected):
            raise ReadFailure('provenance_mismatch')
        return decoded

    def _expected(self, operation, arguments):
        principal = self.principal
        table = 'asset_diagnostics' if operation == 'get_asset_diagnostics' else 'work_orders'
        rows = [{k: v for k, v in row.items() if k != 'allowed_roles'} for row in self.fixture[table]
                if row['tenant_id'] == principal.tenant_id and set(row['allowed_roles']).intersection(principal.roles)]
        if (not set(principal.roles).intersection(('reader', 'analyst'))
                or operation == 'get_asset_diagnostics' and 'analyst' not in principal.roles):
            raise ReadFailure('provenance_mismatch')
        if operation == 'list_work_orders':
            if set(arguments) - {'status', 'after_id', 'limit'}:
                raise ReadFailure('provenance_mismatch')
            limit = arguments.get('limit', 3)
            if type(limit) is not int or not 1 <= limit <= 10:
                raise ReadFailure('provenance_mismatch')
            after = arguments.get('after_id')
            if after is not None and not any(row['work_order_id'] == after for row in rows):
                raise ReadFailure('provenance_mismatch')
            rows = sorted((row for row in rows if (arguments.get('status') is None or row['status'] == arguments['status'])
                           and (after is None or row['work_order_id'] > after)), key=lambda row: row['work_order_id'])
            data = {'items': rows[:limit], 'next_after': rows[limit - 1]['work_order_id'] if len(rows) > limit else None}
        else:
            key = 'asset_id' if table == 'asset_diagnostics' else 'work_order_id'
            if set(arguments) != {key}:
                raise ReadFailure('provenance_mismatch')
            matches = [row for row in rows if row[key] == arguments.get(key)]
            if len(matches) != 1:
                raise ReadFailure('provenance_mismatch')
            data = {'item': matches[0]}
        return {'data': data, 'meta': {'operation': operation, 'tenant_id': principal.tenant_id}}
