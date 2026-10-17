"""The tool schemas must not offer a payload as optional.

Proof-of-life stalled on this. Given the free-text request, the model called
`capability op=author` sixteen times with every field filled in *except* `run_py`
— the entire Python program. It was not a quirk of one model or one prompt: with
`run_py` missing from `required`, stepfun-ai/step-5-preview left it out under
every system prompt and every tool count we tried, and put the source in as soon
as the schema demanded it. A large optional string argument is treated as
skippable.

The same trap sat in `files` (`content`, so `op=write` could silently create an
empty file and report success) and in `memory` (`content`, so `op=write` wrote
nothing). These tests hold the line: if an op needs a payload, the schema has to
say so.
"""

from __future__ import annotations

import pytest

from daedalus.runtime import Runtime

# Params that carry the substance of an action. A tool may declare one only as
# required, never as optional — see the module docstring.
PAYLOAD_PARAMS = ("content", "run_py", "source", "code", "text", "command", "query", "url")

# `calendar` offers nine ops in one schema, and `query` is the payload of exactly
# one of them. Marking it required to satisfy the rule above is how the model came
# to type `query="delete"` into a `delete`: a required field is a field that must
# be filled, and the only word it had was the verb. A tool like that keeps the
# same guarantee a different way — the schema names the op that needs the payload,
# and the op refuses without it — so it is exempt here and held to that instead by
# `test_a_multi_op_tool_states_and_enforces_its_payload`.
MULTI_OP = frozenset({"calendar"})


def _required(runtime: Runtime, tool_name: str) -> set[str]:
    schema = runtime.tools.get(tool_name).schema()["function"]["parameters"]  # type: ignore[union-attr]
    return set(schema.get("required", []))


def test_no_tool_offers_a_payload_as_optional(runtime: Runtime) -> None:
    offenders: list[str] = []
    for tool in runtime.tools.all():
        if tool.name in MULTI_OP:
            continue
        params = tool.schema()["function"]["parameters"]
        required = set(params.get("required", []))
        for name, spec in params.get("properties", {}).items():
            if name in PAYLOAD_PARAMS and spec.get("type") == "string" and name not in required:
                offenders.append(f"{tool.name}.{name}")
    assert not offenders, (
        "these payload params are optional, so the model may silently omit them: "
        + ", ".join(offenders)
    )


def test_a_multi_op_tool_states_and_enforces_its_payload(runtime: Runtime) -> None:
    """`calendar`'s `query` is `search`'s payload, and the other eight ops prove it.

    The rule this replaces was "a payload is required", enforced tool-wide. On a
    one-op tool that is exactly right. On a nine-op tool it made every op carry a
    field only one of them reads, and the model — asked to remove an event it had
    just made — filled it with the only word it had, `query="delete"`. The
    guarantee that matters is not weakened: the schema says which op the payload
    belongs to, and that op refuses without it.
    """
    import asyncio

    from daedalus.tools.calendar import CalendarTool

    schema = runtime.tools.get("calendar").schema()["function"]  # type: ignore[union-attr]
    assert "op=search" in schema["parameters"]["properties"]["query"]["description"], (
        "the schema no longer says which op needs `query`"
    )
    assert "`search` takes `query`" in schema["description"]
    assert "query" not in set(schema["parameters"].get("required", []))

    result = asyncio.run(CalendarTool().run({"op": "search"}, runtime))

    assert not result.ok
    assert "query" in result.output, "op=search did not name the payload it needs"


@pytest.mark.parametrize(
    ("tool_name", "param"),
    [
        ("capability", "run_py"),  # op=author: the whole run.py
        ("files", "content"),  # op=write: the bytes to write
        ("memory", "content"),  # op=write: the fact to store
    ],
)
def test_payload_params_that_matter_are_required(runtime: Runtime, tool_name: str, param: str) -> None:
    assert param in _required(runtime, tool_name), (
        f"{tool_name}.{param} must be required; as an optional param the model drops it, "
        "and the op then fails or writes nothing while reporting success"
    )


def test_the_capability_author_contract_is_stated_in_the_schema(runtime: Runtime) -> None:
    """The model has to know it supplies the source, not the tool."""
    schema = runtime.tools.get("capability").schema()["function"]  # type: ignore[union-attr]
    run_py = schema["parameters"]["properties"]["run_py"]["description"]
    description = schema["description"]
    assert "written by you" in run_py or "you write" in description.lower()
    assert "run.py" in description or "run.py" in run_py


def test_authoring_without_source_fails_with_a_corrective_message(runtime: Runtime) -> None:
    """The error has to tell the model what was missing and to retry with it."""
    import asyncio

    from daedalus.tools.capability import CapabilityTool

    tool = CapabilityTool()
    result = asyncio.run(tool.run({"op": "author", "name": "half_baked"}, runtime))
    assert not result.ok
    assert "run_py" in result.output, "the model is not told which argument was missing"
    assert "Retry" in result.output, "the model is not told what to do next"
