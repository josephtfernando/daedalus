"""CLI surface: print replies, prompt for answers and approvals."""

from __future__ import annotations

import asyncio
import sys

from .base import ApprovalRequest


class CliSurface:
    def __init__(self, *, auto_approve: bool = False, verbose: bool = True) -> None:
        self.auto_approve = auto_approve
        self.verbose = verbose
        self.transcript: list[str] = []

    async def send(self, text: str, *, thread: str | None = None) -> None:
        self.transcript.append(text)
        if self.verbose:
            print(text, flush=True)

    async def ask(self, question: str, *, options: list[str] | None = None) -> str:
        if self.verbose:
            print(question, flush=True)
            if options:
                print("  " + " / ".join(options), flush=True)
        answer = await asyncio.to_thread(sys.stdin.readline)
        return answer.strip()

    async def approve(self, request: ApprovalRequest) -> bool:
        if self.auto_approve:
            return True
        if self.verbose:
            print("\n" + request.describe(), flush=True)
        answer = await asyncio.to_thread(sys.stdin.readline)
        return answer.strip().lower() in ("y", "yes")


class ScriptedSurface:
    """Deterministic double for tests and the offline self-test."""

    def __init__(
        self,
        *,
        answers: list[str] | None = None,
        approve: bool = True,
    ) -> None:
        self.sent: list[str] = []
        self.asked: list[str] = []
        self.approvals: list[ApprovalRequest] = []
        self.progressed: list[str] = []
        self._answers = list(answers or [])
        self.approve_all = approve

    def progress(self, text: str) -> None:
        """Record one line of the work trace, the way a live surface would show it."""
        self.progressed.append(text)

    async def send(self, text: str, *, thread: str | None = None) -> None:
        self.sent.append(text)

    async def ask(self, question: str, *, options: list[str] | None = None) -> str:
        self.asked.append(question)
        return self._answers.pop(0) if self._answers else ""

    async def approve(self, request: ApprovalRequest) -> bool:
        self.approvals.append(request)
        return self.approve_all
