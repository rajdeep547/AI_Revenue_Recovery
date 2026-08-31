"""Slice 10 - the default execution client. No network, fully deterministic.

Without a ``script`` it reports a clean ``SENT`` with a synthetic provider_ref
(``fake_<idem12>``) and remembers the idem_key, so a genuine replay of the same
key returns ``DUPLICATE`` -- the same "recorded, nothing really left the
process" stance the spend_ledger has taken since Slice 9b. Tests pass a
``script`` (a list of :class:`ExecutionResult`, consumed one per ``send``) to
drive the retry / mismatch paths.
"""

from __future__ import annotations

import threading

from app.execution.client import ExecutionRequest, ExecutionResult, ExecutionStatus


class FakeActionClient:
    def __init__(self, script=None, *, on_send=None):
        self._script = list(script) if script is not None else None
        self._on_send = on_send
        self._lock = threading.Lock()
        self.calls: list[ExecutionRequest] = []
        self.sent: dict[str, str] = {}  # idem_key -> provider_ref (the provider's memory)

    @property
    def send_count(self) -> int:
        return len(self.calls)

    def send(self, req: ExecutionRequest) -> ExecutionResult:
        with self._lock:
            self.calls.append(req)
        if self._on_send is not None:
            self._on_send(req)  # test seam: block / observe mid-send

        if self._script:
            res = self._script.pop(0)
        elif req.idem_key in self.sent:
            return ExecutionResult(
                ExecutionStatus.DUPLICATE, provider_ref=self.sent[req.idem_key],
                http_status=409,
            )
        else:
            res = ExecutionResult(
                ExecutionStatus.SENT, provider_ref=f"fake_{req.idem_key[:12]}",
                http_status=200,
            )

        if res.status in (ExecutionStatus.SENT, ExecutionStatus.DUPLICATE) and res.provider_ref:
            self.sent.setdefault(req.idem_key, res.provider_ref)
        return res

    def lookup(self, idem_key: str) -> ExecutionResult | None:
        ref = self.sent.get(idem_key)
        if ref is None:
            return None
        return ExecutionResult(ExecutionStatus.SENT, provider_ref=ref, http_status=200)
