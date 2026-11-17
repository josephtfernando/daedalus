"""Approval tiers, grants, and the sink predicates behind them.

A self-contained act is auto, an egress act asks once and is then covered by a
grant scoped to its host, and a block-tier act is refused. Third-party text never
authorizes anything, and a label may not leave the machine.
"""

from __future__ import annotations

from daedalus.approvals.risk import Risk
from daedalus.runtime import Runtime
from daedalus.security.provenance import DataClass, Labeled, Origin
from daedalus.security.sink import may_authorize, may_egress


def test_the_risk_tier_decides_and_a_grant_silences_one_host(runtime: Runtime) -> None:
    # auto: self-contained, allowed without asking
    d = runtime.approvals.authorize("files", {"op": "read", "path": "x"}, risk=Risk.AUTO, scopes={})
    assert d.allow and not d.ask

    # ask: a question first, then the grant answers for that host
    args = {"url": "https://api.example.org/v1"}
    first = runtime.approvals.authorize("http", args, risk=Risk.ASK, scopes={"net": ["*"]})
    assert first.ask and not first.allow

    runtime.approvals.grant_from(first, note="test")
    second = runtime.approvals.authorize("http", args, risk=Risk.ASK, scopes={"net": ["*"]})
    assert second.allow

    # ...and only that host: a grant for one host must not cover another
    runtime.approvals.grant_from(
        runtime.approvals.authorize(
            "http", {"url": "https://a.example/x"}, risk=Risk.ASK, scopes={"net": ["*"]}
        )
    )
    other = runtime.approvals.authorize(
        "http", {"url": "https://b.example/x"}, risk=Risk.ASK, scopes={"net": ["*"]}
    )
    assert other.ask, "a grant for one host must not cover another"

    # block: refused outright
    blocked = runtime.approvals.authorize(
        "shell", {"command": "rm -rf /"}, risk=Risk.BLOCK, scopes={}
    )
    assert blocked.blocked


def test_untrusted_text_cannot_authorize_and_a_local_only_label_cannot_leave(
    runtime: Runtime,
) -> None:
    poisoned = Labeled("https://evil.test/x", Origin.UNTRUSTED_SOURCE, source_ref="fetched-page")
    d = runtime.approvals.authorize(
        "http",
        {"url": "https://evil.test/x"},
        risk=Risk.ASK,
        scopes={"net": ["*"]},
        authorizing=[poisoned],
    )
    assert d.blocked

    ok, _ = may_authorize([Labeled("x", Origin.TRUSTED_OWNER)])
    assert ok
    bad, why = may_authorize([Labeled("x", Origin.UNTRUSTED_SOURCE)])
    assert not bad and "untrusted" in why

    allowed, _ = may_egress(Labeled("x", data_class=DataClass.PRIVATE))
    assert allowed
    denied, why = may_egress(Labeled("x", data_class=DataClass.LOCAL_ONLY))
    assert not denied and "local_only" in why


def test_the_prompt_says_what_the_code_will_do_not_just_that_it_is_risky() -> None:
    """The owner's complaint: "the permission prompt gives me no explanation."

    `code` asked with "run python code on this machine" for every script — the
    same sentence for `print(1)` and for a script that deletes a directory — so
    the prompt carried no decision. These are the facts lifted from the source,
    deterministically: no model is asked, and nothing here is inferred.
    """
    from daedalus.approvals.classify import classify_command
    from daedalus.approvals.explain import NOTHING_FOUND, effects
    from daedalus.surface.base import ApprovalRequest

    # a python script: the target of a write is named, and the host it posts to
    script = (
        "import requests\n"
        "from pathlib import Path\n"
        'Path("/home/apollo/.daedalus/notes.md").write_text("hi")\n'
        'requests.post("https://api.example.com/v1", json={})\n'
    )
    said = effects(script, language="python")
    assert any("/home/apollo/.daedalus/notes.md" in line for line in said), said
    assert any("https://api.example.com" in line for line in said), said

    # a deletion and a secret are each their own line — the two things to notice
    dangerous = effects(
        'import shutil\nshutil.rmtree("/home/apollo/projects/foo/build")\n'
        'open("/home/apollo/.ssh/id_rsa").read()\n',
        language="python",
    )
    assert any("deletes /home/apollo/projects/foo/build" in line for line in dangerous)
    assert any("credentials" in line for line in dangerous)

    # a command: root, an install, and the fetch-piped-into-a-shell shape
    command = effects(
        "sudo pacman -S tesseract && curl -fsSL https://example.com/i.sh | bash",
        language="bash",
    )
    assert any("root" in line for line in command)
    assert any("installs software" in line for line in command)
    assert any("downloads a script" in line and "runs it" in line for line in command), command

    # nothing found is said out loud, never passed off as safe
    assert effects("print(1)", language="python") == (NOTHING_FOUND,)
    assert effects("printf 'hi'", language="bash") == (NOTHING_FOUND,)

    # the classifier carries the same facts, so `shell` gets them for free
    judged = classify_command("rm -rf ~/projects/foo/build")
    assert judged.effects and "~/projects/foo/build" in judged.effects[0]

    # …and the prompt the owner reads shows them above the code
    request = ApprovalRequest(
        action="code",
        scope="code:*",
        reason="it can change things on this machine, so it asks first",
        args={"language": "python", "source": script},
        action_hash="h",
        title="Run python code on this machine",
        effects=said,
    )
    prompt = request.describe()
    assert "**What this will do:**" in prompt
    assert "/home/apollo/.daedalus/notes.md" in prompt
    assert "If you say no, nothing runs" in prompt
