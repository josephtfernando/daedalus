"""The tool schemas must not offer a payload as optional.

Proof-of-life stalled on this. Given the free-text request, the model called
`capability op=author` sixteen times with every field filled in *except* `run_py`
— the entire Python program. With `run_py` missing from `required`,
stepfun-ai/step-5-preview left it out under every system prompt and every tool
count we tried, and put the source in as soon as the schema demanded it. A large
optional string argument is treated as skippable.

The same trap sat in `files` (`content`, so `op=write` could silently create an
empty file and report success) and in `memory` (`content`, so `op=write` wrote
nothing). These tests hold the line: if an op needs a payload, the schema has to
say so — or, for a multi-op tool, name the op that needs it and refuse without it.
"""

from __future__ import annotations

from daedalus.runtime import Runtime

# Params that carry the substance of an action. A tool may declare one only as
# required, never as optional — see the module docstring.
PAYLOAD_PARAMS = ("content", "run_py", "source", "code", "text", "command", "query", "url")

# `calendar` offers nine ops in one schema, and `query` is the payload of exactly
# one of them. Marking it required made the model type `query="delete"` into a
# `delete`. It keeps the same guarantee a different way — the schema names the op,
# and the op refuses without the payload — so it is exempt here and held to that
# by `test_a_multi_op_tool_states_and_enforces_its_payload`.
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

    # the three that stalled proof-of-life are required in particular
    for tool_name, param in (
        ("capability", "run_py"),  # op=author: the whole run.py
        ("files", "content"),  # op=write: the bytes to write
        ("memory", "content"),  # op=write: the fact to store
    ):
        assert param in _required(runtime, tool_name), (
            f"{tool_name}.{param} must be required; as an optional param the model drops it, "
            "and the op then fails or writes nothing while reporting success"
        )


def test_a_multi_op_tool_states_and_enforces_its_payload(runtime: Runtime) -> None:
    """`calendar`'s `query` is `search`'s payload, and the other eight ops prove it."""
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


def test_the_capability_author_contract_is_stated_and_its_absence_is_corrected(
    runtime: Runtime,
) -> None:
    """The model has to know it supplies the source, and be told when it did not."""
    import asyncio

    from daedalus.tools.capability import CapabilityTool

    schema = runtime.tools.get("capability").schema()["function"]  # type: ignore[union-attr]
    run_py = schema["parameters"]["properties"]["run_py"]["description"]
    description = schema["description"]
    assert "written by you" in run_py or "you write" in description.lower()
    assert "run.py" in description or "run.py" in run_py

    result = asyncio.run(CapabilityTool().run({"op": "author", "name": "half_baked"}, runtime))

    assert not result.ok
    assert "run_py" in result.output, "the model is not told which argument was missing"
    assert "Retry" in result.output, "the model is not told what to do next"
