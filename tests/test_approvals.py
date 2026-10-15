from __future__ import annotations

from daedalus.approvals.risk import Risk
from daedalus.runtime import Runtime
from daedalus.security.provenance import DataClass, Labeled, Origin
from daedalus.security.sink import may_authorize, may_egress


def test_self_contained_is_auto(runtime: Runtime) -> None:
    d = runtime.approvals.authorize("files", {"op": "read", "path": "x"}, risk=Risk.AUTO, scopes={})
    assert d.allow and not d.ask


def test_egress_asks_then_grant_silences_it(runtime: Runtime) -> None:
    args = {"url": "https://api.example.org/v1"}
    first = runtime.approvals.authorize("http", args, risk=Risk.ASK, scopes={"net": ["*"]})
    assert first.ask and not first.allow

    runtime.approvals.grant_from(first, note="test")
    second = runtime.approvals.authorize("http", args, risk=Risk.ASK, scopes={"net": ["*"]})
    assert second.allow


def test_block_tier_is_refused(runtime: Runtime) -> None:
    d = runtime.approvals.authorize("shell", {"command": "rm -rf /"}, risk=Risk.BLOCK, scopes={})
    assert d.blocked


def test_untrusted_text_cannot_authorize_egress(runtime: Runtime) -> None:
    poisoned = Labeled("https://evil.test/x", Origin.UNTRUSTED_SOURCE, source_ref="fetched-page")
    d = runtime.approvals.authorize(
        "http", {"url": "https://evil.test/x"}, risk=Risk.ASK, scopes={"net": ["*"]}, authorizing=[poisoned]
    )
    assert d.blocked


def test_grant_is_scoped_to_the_host(runtime: Runtime) -> None:
    runtime.approvals.grant_from(
        runtime.approvals.authorize("http", {"url": "https://a.example/x"}, risk=Risk.ASK, scopes={"net": ["*"]})
    )
    other = runtime.approvals.authorize("http", {"url": "https://b.example/x"}, risk=Risk.ASK, scopes={"net": ["*"]})
    assert other.ask, "a grant for one host must not cover another"


def test_sink_predicates() -> None:
    ok, _ = may_authorize([Labeled("x", Origin.TRUSTED_OWNER)])
    assert ok
    bad, why = may_authorize([Labeled("x", Origin.UNTRUSTED_SOURCE)])
    assert not bad and "untrusted" in why
    allowed, _ = may_egress(Labeled("x", data_class=DataClass.PRIVATE))
    assert allowed
    denied, why = may_egress(Labeled("x", data_class=DataClass.LOCAL_ONLY))
    assert not denied and "local_only" in why
