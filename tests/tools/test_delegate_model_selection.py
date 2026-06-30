#!/usr/bin/env python3
"""
Tests for per-task model selection in delegate_task.

Ported from Kilo-Org/kilocode#11786. The agent can name a model per task
("opus", "gpt-5", "glm") when fanning work out; resolution reuses the shared
``model_switch`` pipeline so names are matched leniently and the provider is
resolved (not dictated). Gated behind ``delegation.allow_model_selection``
(default off) to preserve the "subagents inherit the parent model" contract.

Run with:  python -m pytest tests/tools/test_delegate_model_selection.py -v
"""

import unittest
from unittest.mock import patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _build_dynamic_schema_overrides,
    _get_allow_model_selection,
    _resolve_task_model_creds,
)


class _FakeParent:
    """Minimal parent agent for credential anchoring."""

    provider = "openrouter"
    model = "anthropic/claude-opus-4.8"
    base_url = "https://openrouter.ai/api/v1"
    api_key = "sk-test"


_BASE_CREDS = {
    "model": None,
    "provider": None,
    "base_url": None,
    "api_key": None,
    "api_mode": None,
    "command": None,
    "args": None,
}


class TestSchemaGating(unittest.TestCase):
    """The per-task `model` field only appears when the flag is enabled."""

    def test_flag_off_no_model_field(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            ov = _build_dynamic_schema_overrides()
        props = ov["parameters"]["properties"]
        self.assertNotIn("model", props)
        self.assertNotIn("model", props["tasks"]["items"]["properties"])

    def test_flag_on_adds_model_field(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"allow_model_selection": True},
        ):
            ov = _build_dynamic_schema_overrides()
        props = ov["parameters"]["properties"]
        self.assertIn("model", props)
        self.assertEqual(props["model"]["type"], "string")
        self.assertIn("model", props["tasks"]["items"]["properties"])

    def test_static_schema_never_mutated(self):
        """Dynamic overrides must not leak the model field into the static schema."""
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"allow_model_selection": True},
        ):
            _build_dynamic_schema_overrides()
        static_props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertNotIn("model", static_props)
        self.assertNotIn(
            "model", static_props["tasks"]["items"]["properties"]
        )


class TestFlagGetter(unittest.TestCase):
    def test_default_off(self):
        with patch("tools.delegate_tool._load_config", return_value={}):
            self.assertFalse(_get_allow_model_selection())

    def test_truthy_on(self):
        with patch(
            "tools.delegate_tool._load_config",
            return_value={"allow_model_selection": True},
        ):
            self.assertTrue(_get_allow_model_selection())


class TestModelResolution(unittest.TestCase):
    """`_resolve_task_model_creds` reuses the model_switch pipeline."""

    def test_empty_name_returns_base_unchanged(self):
        out = _resolve_task_model_creds("", _FakeParent(), _BASE_CREDS)
        self.assertIs(out, _BASE_CREDS)

    def test_bare_name_stays_on_parent_aggregator(self):
        """A bare name resolved on the parent's provider keeps inherited creds."""
        out = _resolve_task_model_creds("sonnet", _FakeParent(), _BASE_CREDS)
        # Resolved to a concrete OpenRouter slug...
        self.assertTrue(out["model"].startswith("anthropic/claude-sonnet"))
        # ...but provider stays None because it matched the parent provider,
        # so _build_child_agent inherits the parent's credentials.
        self.assertIsNone(out["provider"])

    def test_full_slug_passthrough(self):
        out = _resolve_task_model_creds(
            "openai/gpt-5.4", _FakeParent(), _BASE_CREDS
        )
        self.assertEqual(out["model"], "openai/gpt-5.4")

    def test_unresolvable_name_raises(self):
        with self.assertRaises(ValueError):
            _resolve_task_model_creds(
                "zzznotarealmodel-xyz-123", _FakeParent(), _BASE_CREDS
            )

    def test_base_creds_not_mutated(self):
        before = dict(_BASE_CREDS)
        _resolve_task_model_creds("sonnet", _FakeParent(), _BASE_CREDS)
        self.assertEqual(_BASE_CREDS, before)


if __name__ == "__main__":
    unittest.main()
