"""The Melinoë verification gate (SPEC §3.5, ARCHITECTURE §7.2).

No unverified automation runs unattended. A newly authored capability is run
once and checked before it is registered for scheduling; re-authoring
re-triggers the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .manifest import Manifest
from .runner import CapabilityRunner, RunResult


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class VerifyReport:
    ok: bool = False
    checks: list[Check] = field(default_factory=list)
    run: RunResult | None = None

    def summary(self) -> str:
        lines = [f"{'PASS' if c.ok else 'FAIL'}  {c.name}" + (f" — {c.detail}" if c.detail else "") for c in self.checks]
        return "\n".join(lines) or "(no checks ran)"


async def verify_capability(
    manifest: Manifest,
    runner: CapabilityRunner,
    *,
    sample_args: dict[str, Any] | None = None,
    sample_context: dict[str, Any] | None = None,
    allow_egress: bool = False,
) -> VerifyReport:
    """Run the capability once and check output shape, exit, and scope posture."""
    report = VerifyReport()

    # 1. Scope posture: egress must be explicitly allowed before we exercise it.
    if manifest.scopes.needs_egress() and not allow_egress:
        report.checks.append(
            Check("scope_posture", False, f"declares net={manifest.scopes.net}; needs owner approval first")
        )
        report.ok = False
        return report
    report.checks.append(Check("scope_posture", True, "declared scopes accepted"))

    # 2. Manifest sanity.
    sane = bool(manifest.name) and bool(manifest.entry)
    report.checks.append(Check("manifest", sane, "" if sane else "missing name or entry"))
    if not sane:
        return report

    # 3. Run once.
    result = await runner.run(manifest, args=sample_args or {}, context=sample_context or {})
    report.run = result
    report.checks.append(
        Check("run_once", result.ok, result.error or (f"exit {result.exit_code} in {result.duration_s:.2f}s"))
    )

    # 4. Output shape: stdout must be the JSON protocol.
    shaped = result.result is not None or result.state_next is not None or bool(result.notifications)
    report.checks.append(
        Check("output_shape", result.ok and shaped, "no {result} payload on stdout" if not shaped else "ok")
    )

    report.ok = all(c.ok for c in report.checks)
    return report
