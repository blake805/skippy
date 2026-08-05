"""The schemas are the model's only description of what it can do.

A schema that drifts from its Python function fails at the worst moment: the model
emits a call with a parameter that no longer exists, the dispatcher raises
TypeError, and the observation the model gets back is a stack trace rather than
something it can act on. These tests tie each workspace schema to the signature it
describes so that drift is a red test instead.
"""

import inspect

import pytest

import skippy_edit
import skippy_exec
import skippy_fs
import skippy_git
import skippy_memory
import skippy_re
import tool_schemas

# The schemas that describe a real Python function, and the function they describe.
IMPLEMENTED = {
    "list_dir": skippy_fs.list_dir,
    "read_file": skippy_fs.read_file,
    "grep": skippy_fs.grep,
    "glob_files": skippy_fs.glob_files,
    "apply_patch": skippy_edit.apply_patch,
    "run_command": skippy_exec.run_command,
    "git_status": skippy_git.git_status,
    "git_diff": skippy_git.git_diff,
    "git_branch": skippy_git.git_branch,
    "git_commit": skippy_git.git_commit,
    "note_finding": skippy_re.note_finding,
    "read_notes": skippy_re.read_notes,
    "record_decision": skippy_memory.record_decision,
    "recall_project": skippy_memory.recall_project,
}

# Arguments the dispatcher supplies. The model never sees these, so a schema that
# declared one would be describing a parameter it cannot fill.
INJECTED = ("sandbox", "pack", "journal_dir", "mode", "memory", "approver")

# Tools that accept their required arguments with Python defaults and check them in
# the body, so an omission comes back as a message naming the field and its legal
# values rather than as a TypeError from the dispatcher. Worth the exception for a
# tool with a closed vocabulary — "'struct' is not a finding kind, use one of..." is
# something the model can act on; "Bad arguments" is not.
SELF_VALIDATING = {
    "note_finding": {"kind", "title", "body", "confidence"},
    "record_decision": {"title", "body"},
}


@pytest.mark.parametrize("name", sorted(tool_schemas._SCHEMAS))
def test_every_schema_is_well_formed(name):
    schema = tool_schemas._SCHEMAS[name]
    assert schema["description"].strip(), f"{name} has no description"
    params = schema["parameters"]
    assert params["type"] == "object"
    assert isinstance(params["properties"], dict)
    # A required name that is not in properties is invisible to the model.
    assert set(params.get("required", [])) <= set(params["properties"]), name


@pytest.mark.parametrize("name", sorted(tool_schemas._SCHEMAS))
def test_every_property_has_a_type_and_a_description(name):
    for prop, spec in tool_schemas._SCHEMAS[name]["parameters"]["properties"].items():
        assert "type" in spec, f"{name}.{prop} has no type"
        assert spec.get("description", "").strip() or "enum" in spec, f"{name}.{prop} undescribed"


@pytest.mark.parametrize("name,function", sorted(IMPLEMENTED.items()))
def test_schema_parameters_match_the_function_signature(name, function):
    """Catches the drift that turns a tool call into a TypeError."""
    signature = inspect.signature(function)
    accepted = {p for p in signature.parameters if p not in INJECTED}
    declared = set(tool_schemas._SCHEMAS[name]["parameters"]["properties"])
    assert declared <= accepted, f"{name} declares parameters {function.__name__} cannot take: {declared - accepted}"


@pytest.mark.parametrize("name,function", sorted(IMPLEMENTED.items()))
def test_no_schema_declares_an_injected_argument(name, function):
    """The sandbox, the note pack, the journal and the mode are chosen by the loop.
    Naming one in a schema would invite the model to set it."""
    declared = set(tool_schemas._SCHEMAS[name]["parameters"]["properties"])
    assert not (declared & set(INJECTED)), f"{name} exposes {declared & set(INJECTED)}"


@pytest.mark.parametrize("name,function", sorted(IMPLEMENTED.items()))
def test_every_required_parameter_is_actually_required(name, function):
    """A parameter with a Python default must not be declared required, and one
    without a default must be, or the model learns the wrong contract.

    Self-validating tools are exempt from the first half and checked separately
    below: they take defaults deliberately so they can answer with a usable message.
    """
    signature = inspect.signature(function)
    required = set(tool_schemas._SCHEMAS[name]["parameters"].get("required", []))
    exempt = SELF_VALIDATING.get(name, set())
    for param in required - exempt:
        assert signature.parameters[param].default is inspect.Parameter.empty, (
            f"{name} declares '{param}' required, but it has a default"
        )
    mandatory = {
        p.name for p in signature.parameters.values()
        if p.name not in INJECTED and p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    }
    assert mandatory <= required, f"{name} omits required parameters: {mandatory - required}"


@pytest.mark.parametrize("name,fields", sorted(SELF_VALIDATING.items()))
def test_a_self_validating_tool_actually_rejects_what_it_calls_required(name, fields, tmp_path):
    """The exemption above is only safe if the check really happens in the body.
    Without this, giving a required field a default would quietly make it optional.
    """
    declared = set(tool_schemas._SCHEMAS[name]["parameters"].get("required", []))
    assert fields <= declared, f"{name}: {fields - declared} not declared required"

    if name == "note_finding":
        valid = {
            "kind": "structure",
            "title": "Header is 32 bytes",
            "body": "Load commands start at 0x20.",
            "evidence": "otool -h reports sizeofcmds 0x20",
            "confidence": "confirmed",
        }
        first = skippy_re.open_pack(str(tmp_path / "notes"), target="t")
    else:
        valid = {
            "title": "Retries belong in the transport",
            "body": "Per-call retries duplicated the backoff logic.",
        }
        first = skippy_memory.open_project(
            root=str(tmp_path / "projects"), workspace_roots=[str(tmp_path)]
        )

    assert IMPLEMENTED[name](first, **valid).ok, "the baseline call should succeed"

    for field in fields:
        without = dict(valid, **{field: ""})
        result = IMPLEMENTED[name](first, **without)
        assert not result.ok, f"{name} accepted a call with no '{field}'"


def test_the_mutating_tools_are_not_in_the_read_only_set():
    """Handing out apply_patch or run_command by accident is the mistake worth a test."""
    read_only = {t["function"]["name"] for t in tool_schemas.filesystem_tools()}
    assert "apply_patch" not in read_only
    assert "run_command" not in read_only


def test_workspace_tools_include_read_write_and_verify():
    names = {t["function"]["name"] for t in tool_schemas.workspace_tools()}
    assert {"read_file", "grep", "apply_patch", "run_command"} <= names
    # Version control is part of finishing coding work.
    assert {"git_status", "git_diff", "git_branch", "git_commit"} <= names


def test_wrapped_schemas_have_the_shape_the_api_expects():
    for tool in tool_schemas.workspace_tools():
        assert tool["type"] == "function"
        assert set(tool["function"]) == {"name", "description", "parameters"}


def test_the_re_toolset_cannot_edit_and_can_record():
    names = {t["function"]["name"] for t in tool_schemas.re_tools()}
    # The artifact is not ours to change, and editing it would destroy the evidence
    # the findings cite.
    assert "apply_patch" not in names
    # No history to write either: an RE session produces findings, not commits.
    assert "git_commit" not in names
    assert "git_branch" not in names
    assert {"note_finding", "read_notes", "read_file", "grep", "run_command"} <= names
    # Live hardware is RE-only: probing a part on the bench is not a coding concern,
    # and coding mode must not suddenly grow a way to talk to /dev.
    assert {"list_devices", "serial_open", "serial_io", "net_scan"} <= names


def test_note_finding_explains_why_evidence_is_required():
    """The model supplies real evidence only if the description says what it is for;
    asked for a field called 'evidence' with no reason, it writes 'observed'."""
    description = tool_schemas._SCHEMAS["note_finding"]["description"].lower()
    assert "evidence" in description
    assert "recheck" in description
    properties = tool_schemas._SCHEMAS["note_finding"]["parameters"]["properties"]
    assert "offset" in properties["evidence"]["description"].lower()


def test_the_finding_vocabularies_come_from_the_module_not_a_copy():
    """A hand-copied enum in the schema is a second source of truth: the module would
    start accepting a kind the model is never told about, or the reverse."""
    properties = tool_schemas._SCHEMAS["note_finding"]["parameters"]["properties"]
    assert set(properties["kind"]["enum"]) == set(skippy_re.KINDS)
    assert set(properties["confidence"]["enum"]) == set(skippy_re.CONFIDENCE)


def test_apply_patch_describes_its_all_or_nothing_behaviour():
    """The model batches edits correctly only if the description says to, and
    getting this wrong means half-applied refactors."""
    description = tool_schemas._SCHEMAS["apply_patch"]["description"].lower()
    assert "nothing is written" in description
    assert "one call" in description
    assert "byte-for-byte" in description


def test_the_memory_tools_are_offered_in_both_modes():
    """Continuing prior work is not specific to coding or to reverse engineering."""
    coding = {t["function"]["name"] for t in tool_schemas.workspace_tools()}
    re_mode = {t["function"]["name"] for t in tool_schemas.re_tools()}
    assert {"record_decision", "recall_project"} <= coding
    assert {"record_decision", "recall_project"} <= re_mode


def test_record_decision_asks_for_reasoning_and_warns_off_restating_the_diff():
    """Without the warning it writes "changed ops.py to add retry", which the diff
    already says and which is worth nothing to a later session."""
    description = tool_schemas._SCHEMAS["record_decision"]["description"].lower()
    assert "reasoning" in description
    assert "dead end" in description or "ruled out" in description
    assert "diff already says" in description
