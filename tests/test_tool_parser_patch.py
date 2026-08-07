"""Pins the local patch to mlx_lm's Qwen3-Coder tool parser.

The parser is third-party code, and the patch lives in the venv rather than in git —
`pip install -U mlx-lm` or a fresh venv silently reverts it. Unpatched, it raises out
of its request handler on a tool call it cannot convert (a repr-quoted string with an
apostrophe in an array-typed parameter, a non-numeric value in an integer-typed one),
which kills the handler thread and closes the socket with no response written. The
client sees "Server disconnected", which cost a day of wrong theories before the
traceback was captured.

This test is the alarm for that reversion: it feeds the parser the exact payloads that
killed it live. If it goes red after touching the venv, re-apply the patch in
mlx_lm/tool_parsers/qwen3_coder.py (guard int(), float() and ast.literal_eval with a
fall-through to the raw string) — or check whether upstream has fixed it and the patch
can be retired along with this test.
"""

import importlib.util
import os
import sysconfig

import pytest


def _load_parser():
    """The parser module by file path, not `import mlx_lm`.

    Importing the package initializes mlx and aborts wherever Metal is unreachable
    (sandboxes, CI). The parser file itself needs only ast, json and regex, so loading
    it directly tests exactly the code the server runs without dragging in the GPU.
    """
    path = os.path.join(
        sysconfig.get_paths()["purelib"], "mlx_lm", "tool_parsers", "qwen3_coder.py"
    )
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("qwen3_coder_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qwen3_coder = _load_parser()
if qwen3_coder is None:  # pragma: no cover - environment without mlx_lm
    pytest.skip(
        "mlx_lm is not installed here; the parser patch has nothing to protect",
        allow_module_level=True,
    )


def call(param_value: str, param_type: str) -> dict:
    """One tool call through the real parser, with one parameter of the given type."""
    tools = [{
        "function": {
            "name": "f",
            "parameters": {"type": "object", "properties": {"p": {"type": param_type}}},
        }
    }]
    body = f"<function=f>\n<parameter=p>\n{param_value}\n</parameter>\n</function>"
    return qwen3_coder.parse_tool_call(body, tools)


def test_code_with_an_apostrophe_in_an_array_param_does_not_raise():
    """The live killer: apply_patch edits carrying source code. json.loads fails on the
    Python-literal syntax, ast.literal_eval raises SyntaxError on the apostrophe, and
    unpatched nothing catches it."""
    # The apostrophe sits inside a single-quoted string, the way the model actually
    # writes it — invalid as JSON and as a Python literal, so both parsers fail.
    poison = "[{'path': 'motor.py', 'search': 'the motor's limit', 'replace': 'x'}]"
    parsed = call(poison, "array")
    # The contract with the caller: unparseable content comes back as the raw string,
    # which skippy_dispatch refuses with a message the model can act on.
    assert parsed["arguments"]["p"] == poison


def test_a_non_numeric_integer_param_does_not_raise():
    parsed = call("all", "integer")
    assert parsed["arguments"]["p"] == "all"


def test_a_non_numeric_number_param_does_not_raise():
    parsed = call("none", "number")
    assert parsed["arguments"]["p"] == "none"


def test_valid_values_still_parse_to_their_types():
    assert call('[{"path": "a.py"}]', "array")["arguments"]["p"] == [{"path": "a.py"}]
    assert call("12", "integer")["arguments"]["p"] == 12
    assert call("2.5", "number")["arguments"]["p"] == 2.5
    assert call("true", "boolean")["arguments"]["p"] is True
    assert call("plain text", "string")["arguments"]["p"] == "plain text"
