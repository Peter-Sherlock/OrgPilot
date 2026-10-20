"""Explicit CLI gate and preview for the isolated L3-A approval probe."""

import json
import re
import secrets
import sys
from pathlib import Path

from orgpilot.config import OrgPilotSettings
from orgpilot.feishu.approval_acceptance import L3ApprovalAcceptance, build_test_approval_card
from orgpilot.feishu.client import AsyncFeishuClient
from orgpilot.feishu.ws import FeishuWebSocketListener


def run_approval_probe(
    run_id: str | None, timeout_seconds: int, confirmed: bool, preview: bool
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if preview:
        print("L3-A preview: one private test card; approve/reject records only a local choice.")
        print("No task execution. One send attempt, no retry. Target bound by a random challenge.")
        print(
            json.dumps(
                build_test_approval_card("feishu-l3-preview", "preview", "preview"),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not confirmed:
        raise SystemExit("--confirm-single-card is required for the real L3 approval test")
    if not run_id or not re.fullmatch(r"feishu-l3-[a-zA-Z0-9_-]{1,64}", run_id):
        raise SystemExit("--run-id must start with 'feishu-l3-' and use a safe unique suffix")
    if not 30 <= timeout_seconds <= 300:
        raise SystemExit("--timeout-seconds must be between 30 and 300")
    settings = OrgPilotSettings.from_env()
    if settings.collaboration_adapter != "feishu" or not settings.feishu_use_ws:
        raise SystemExit("L3 requires the Feishu adapter and WebSocket transport")
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise SystemExit("Feishu credentials are required (values hidden)")
    if settings.demo_bootstrap or settings.feishu_allow_writes:
        raise SystemExit("Demo bootstrap must be disabled and ambient Feishu write gate closed")
    challenge = f"ORGPILOT-L3:{secrets.token_urlsafe(18)}"
    run_dir = Path("var/feishu-acceptance") / run_id
    try:
        acceptance = L3ApprovalAcceptance(
            challenge,
            AsyncFeishuClient(
                settings.feishu_app_id, settings.feishu_app_secret, allow_writes=True
            ),
            run_dir,
        )
    except FileExistsError:
        raise SystemExit("L3 run already exists; never resume or overwrite it") from None
    except Exception as exc:
        print(f"[FAIL] L3 initialization: {type(exc).__name__} (details hidden).")
        return 1
    listener = FeishuWebSocketListener(
        settings.feishu_app_id, settings.feishu_app_secret, project_id=run_id, acceptance=acceptance
    )
    stopped = False
    try:
        print("[*] L3-A: one test approval card, isolated outbox, local decision only.", flush=True)
        listener.start()
        if not listener.wait_until_connected(30.0):
            error_type = listener.connection_error_type or "none"
            print(f"[FAIL] WebSocket not ready (error={error_type}; details hidden).", flush=True)
        else:
            acceptance.arm(timeout_seconds)
            print("[READY] Send this exact text once in the test bot private chat:", flush=True)
            print(challenge, flush=True)
            print(
                "[*] Then click ONE of the test card buttons before the window closes.", flush=True
            )
            acceptance.wait(timeout_seconds)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"[FAIL] L3 interrupted: {type(exc).__name__} (details hidden).", flush=True)
    finally:
        acceptance.close()
        try:
            stopped = listener.stop()
        except Exception as exc:
            print(f"[FAIL] L3 shutdown: {type(exc).__name__} (details hidden).", flush=True)
        acceptance.record_shutdown(stopped)
    result = acceptance.snapshot()
    print("[AUDIT] " + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    print(f"[*] Local redacted report and isolated outbox: {run_dir}", flush=True)
    passed = (
        stopped
        and result["send_attempts"] == 1
        and result["reply_status"] == "api_accepted"
        and result["outbox_status"] == "delivered"
        and result["accepted_callbacks"] == 1
        and result["approval_status"] in {"approved", "rejected"}
        and not any(result.get(key) for key in ("audit_error", "error_type", "storage_error"))
    )
    if passed:
        print("[PASS] One card API-accepted and one bound decision recorded; no task executed.")
        print("[*] User-visible card/ACK confirmation is separate from this machine report.")
        return 0
    print(
        "[FAIL] L3 evidence incomplete. Inspect report; never retry uncertain sends automatically."
    )
    return 1
