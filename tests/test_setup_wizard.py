"""The wizard's Tailscale stage: the posture is asserted, not narrated.

`scripts/setup.sh` is bash with no test harness of its own, so these tests read it
the way its own Hardening stage reads `config.toml` — as text whose *shape* is a
claim. Three shapes must hold, because each one is a promise the wizard makes to
the owner:

- **Nothing in the wizard can publish past the tailnet.** Funnel is the one
  Tailscale feature that reaches the public internet, and every publisher in this
  system refuses rather than proceed; a wizard that could type `funnel on` would
  be a second, quieter way to do the thing the refusals exist to prevent.
- **The API key is sealed, and it is sealed only.** It reaches the credential
  store through a 0600 temp file that is deleted; it never reaches `.env`,
  `config.toml`, or the wizard's own output. A provisioning surface that leaks a
  key while capturing it is worse than one that never asks.
- **The registry note is a proposal, not a claim of access.** `_record_resource`
  writes commented placeholders — the T9 shape — and must stay that way.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from daedalus.genui.publish import _funnel_enabled

WIZARD = Path(__file__).resolve().parent.parent / "scripts" / "setup.sh"


def wizard_text() -> str:
    return WIZARD.read_text(encoding="utf-8")


def api_key_block() -> str:
    """The stage-8 block that captures the Tailscale API key, nothing else."""
    text = wizard_text()
    start = text.index("Capture a Tailscale API key?")
    end = text.index('SKIPPED+=("tailscale API key not captured")', start)
    return text[start:end]


# -- the posture -------------------------------------------------------------


def test_no_line_of_the_wizard_turns_funnel_on() -> None:
    for line in wizard_text().splitlines():
        low = line.lower()
        if "funnel" not in low:
            continue
        assert "funnel on" not in low or "off" in low, line
        if re.search(r"(?:^|\||&&|;|\$\()\s*tailscale\s+funnel\b", line):
            assert "off" in low, f"a Funnel command that is not the takedown: {line}"


def test_the_api_key_is_sealed_into_the_store_and_nowhere_else() -> None:
    block = api_key_block()
    # The variable is born hidden, written once to the temp file, and unset.
    assert len(re.findall(r"_TS_KEY", block)) == 4, "the key variable leaked a use"
    assert 'printf \'%s\' "$_TS_KEY" > "$_ts_tmp"' in block
    assert block.index("chmod 600") < block.index('printf \'%s\' "$_TS_KEY"')
    assert block.index("unset _TS_KEY") > block.index('printf \'%s\' "$_TS_KEY"')
    assert "_adopt_secret tailscale-api-key" in block
    # Not to the env file, not to config.toml — the store is the only destination.
    assert "write_env" not in block
    assert "toml_set" not in block
    assert "config.toml" not in block


def test_the_registry_note_is_a_proposal_not_a_claim() -> None:
    text = wizard_text()
    start = text.index("_record_resource() {")
    end = text.index("}", text.index("ok \"noted", start))
    body = text[start:end]
    for line in body.splitlines():
        if "printf" not in line:
            continue
        fmt = line.split("'", 2)[1] if line.count("'") >= 2 else ""
        assert fmt.startswith("#"), f"_record_resource wrote a live line: {line}"
    assert 'grep -q "^# resource: ${name}\\$"' in body, "the once-only guard is gone"


# -- parity with the publisher ----------------------------------------------


def wizard_funnel_reading(payload: str) -> str:
    """Run the wizard's own Funnel reader the way the wizard runs it."""
    text = wizard_text()
    match = re.search(
        r"python3 -c '(.*?)' 2>/dev/null \|\| echo unknown",
        text[text.index("_funnel_state() {") :],
        re.S,
    )
    assert match, "the _funnel_state reader is gone or reshaped"
    proc = subprocess.run(
        [sys.executable, "-c", match.group(1)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout.strip()


def test_the_wizard_reads_funnel_the_way_the_publisher_does() -> None:
    """One rule, two readers: if they drift, the wizard blesses what the publisher refuses."""
    cases = {
        "off": json.dumps({"Web": {"443": {}}}),
        "on": json.dumps({"Funnel": {"443": {"https": True}}}),
        "unreadable": "not json at all",
    }
    for name, payload in cases.items():
        reading = wizard_funnel_reading(payload)
        publisher = _funnel_enabled(payload)
        assert reading == "on" if publisher else reading != "on", (name, reading, publisher)
        if name == "unreadable":
            # The publisher proceeds (its own command is tailnet-only); the wizard
            # must not claim a pass — it says unknown and warns instead.
            assert reading == "unknown"
        if name == "off":
            assert reading == "off"
