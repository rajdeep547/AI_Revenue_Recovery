"""Slice 10 - client selection and safety gates. Config only; callers never
branch on mode -- they call :func:`build_client` and get an :class:`ActionClient`.

  EXECUTION_MODE           "fake" (default) | "razorpay_test"
  LIVE_EXECUTION_ENABLED   must be truthy for "razorpay_test" to construct
  RAZORPAY_KEY_ID          test-mode key id (prefix 'rzp_test_'), from .env
  RAZORPAY_KEY_SECRET      test-mode key secret, from .env -- never logged
"""

from __future__ import annotations

import os

from app.execution.client import ActionClient
from app.execution.fake_client import FakeActionClient

_TRUTHY = {"1", "true", "yes", "on"}


def _load_dotenv_once() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001 - .env is optional; env vars may already be set
        pass


def build_client(*, env_override: dict | None = None) -> ActionClient:
    """The only place a concrete client is constructed. Selection is by config;
    no other caller changes when the mode flips."""
    if env_override is None:
        _load_dotenv_once()
        env = os.environ
    else:
        env = env_override

    mode = (env.get("EXECUTION_MODE") or "fake").strip().lower()

    if mode == "fake":
        return FakeActionClient()

    if mode == "razorpay_test":
        if (env.get("LIVE_EXECUTION_ENABLED") or "").strip().lower() not in _TRUTHY:
            raise RuntimeError(
                "EXECUTION_MODE=razorpay_test requires LIVE_EXECUTION_ENABLED to be "
                "truthy -- a deliberate opt-in before any real provider call"
            )
        from app.execution.razorpay_client import RazorpayClient

        return RazorpayClient(
            env.get("RAZORPAY_KEY_ID", ""),
            env.get("RAZORPAY_KEY_SECRET", ""),
        )

    raise ValueError(
        f"unknown EXECUTION_MODE {mode!r} (expected 'fake' or 'razorpay_test')"
    )
