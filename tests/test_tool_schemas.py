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
import tool_schemas

# The schemas that describe a real Python function, and the function they describe.
IMPLEMENTED = {
    "list_dir": skippy_fs.list_dir,
    "read_file": skippy_fs.read_file,
    "grep": skippy_fs.grep,
    "glob_files": skippy_fs.glob_files,
    "apply_patch": skippy_edit.apply_patch,
    "run_command": skippy_exec.run_command,
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
    # The sandbox is injected by the dispatcher, not chosen by the model.
    accepted = {p for p in signature.parameters if p != "sandbox"}
    declared = set(tool_schemas._SCHEMAS[name]["parameters"]["properties"])
    assert declared <= accepted, f"{name} declares parameters {function.__name__} cannot take: {declared - accepted}"


@pytest.mark.parametrize("name,function", sorted(IMPLEMENTED.items()))
def test_every_required_parameter_is_actually_required(name, function):
    """A parameter with a Python default must not be declared required, and one
    without a default must be, or the model learns the wrong contract."""
    signature = inspect.signature(function)
    required = set(tool_schemas._SCHEMAS[name]["parameters"].get("required", []))
    for param in required:
        assert signature.parameters[param].default is inspect.Parameter.empty, (
            f"{name} declares '{param}' required, but it has a default"
        )
    mandatory = {
        p.name for p in signature.parameters.values()
        if p.name != "sandbox" and p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    }
    assert mandatory <= required, f"{name} omits required parameters: {mandatory - required}"


def test_the_mutating_tools_are_not_in_the_read_only_set():
    """Handing out apply_patch or run_command by accident is the mistake worth a test."""
    read_only = {t["function"]["name"] for t in tool_schemas.filesystem_tools()}
    assert "apply_patch" not in read_only
    assert "run_command" not in read_only


def test_workspace_tools_include_read_write_and_verify():
    names = {t["function"]["name"] for t in tool_schemas.workspace_tools()}
    assert {"read_file", "grep", "apply_patch", "run_command"} <= names


def test_wrapped_schemas_have_the_shape_the_api_expects():
    for tool in tool_schemas.workspace_tools():
        assert tool["type"] == "function"
        assert set(tool["function"]) == {"name", "description", "parameters"}


def test_apply_patch_describes_its_all_or_nothing_behaviour():
    """The model batches edits correctly only if the description says to, and
    getting this wrong means half-applied refactors."""
    description = tool_schemas._SCHEMAS["apply_patch"]["description"].lower()
    assert "nothing is written" in description
    assert "one call" in description
    assert "byte-for-byte" in description
