"""At most one explicit read-only failover; absolute work deadline never resets."""
from __future__ import annotations
import time
from .contract import Budget, FixtureContract, ReadFailure

RETRYABLE = frozenset(('transport_timeout', 'transport_closed'))


class AttemptFailure(Exception):
    def __init__(self, code, trace):
        self.code, self.trace = code, trace
        super().__init__(code)


class FailoverReader:
    def __init__(self, endpoints, profile, *, budget=Budget(), _driver=None, _clock=time.monotonic):
        if type(budget) is not Budget:
            raise ValueError('An explicit validated work budget is required')
        self.contract = FixtureContract(endpoints, profile)
        self.budget, self.clock = budget, _clock
        if _driver is None:
            from .transport import StdioDriver
            _driver = StdioDriver()
        self.driver = _driver

    async def read(self, operation, arguments):
        started = self.clock()
        deadline = started + self.budget.total_seconds
        arguments = self.contract.validate_command(operation, arguments)
        traces = []
        for endpoint in self.contract.endpoints[:self.budget.max_attempts]:
            if self.clock() >= deadline:
                raise ReadFailure('total_deadline', traces)
            try:
                self.contract.before_attempt(endpoint)
            except ReadFailure as exc:
                raise ReadFailure(exc.code, traces) from None
            if self.clock() >= deadline:
                raise ReadFailure('total_deadline', traces)
            attempt_deadline = min(deadline, self.clock() + self.budget.attempt_seconds)
            try:
                result, trace = await self.driver.attempt(endpoint, self.contract, operation, arguments, attempt_deadline)
            except AttemptFailure as exc:
                traces.append(exc.trace)
                if exc.code not in RETRYABLE:
                    raise ReadFailure(exc.code, traces) from None
                # Cleanup consumes the same absolute total; never allocate a
                # fresh full budget to the next replica after a slow teardown.
                if self.clock() >= deadline:
                    raise ReadFailure('total_deadline', traces) from None
                continue
            # Validation, cleanup and caller cancellation are all terminal.
            # Never catch BaseException/cancellation and turn it into failover.
            traces.append(trace)
            if self.clock() >= deadline or self.clock() >= attempt_deadline:
                raise ReadFailure('total_deadline' if self.clock() >= deadline else 'attempt_deadline', traces)
            try:
                decoded = self.contract.validate_result(operation, arguments, result)
                self.contract.before_attempt(endpoint)
            except ReadFailure as exc:
                raise ReadFailure(exc.code, traces) from None
            if self.clock() >= deadline or self.clock() >= attempt_deadline:
                raise ReadFailure('total_deadline' if self.clock() >= deadline else 'attempt_deadline', traces)
            return {'response': decoded, 'attempts': traces, 'endpoint': endpoint.name,
                    'elapsed_seconds': round(self.clock() - started, 6),
                    'fixture_sha256': self.contract.fixture_sha256,
                    'profile': self.contract.profile}
        raise ReadFailure('attempts_exhausted', traces)
