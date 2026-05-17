"""Unit tests for llm._coerce_json and _strip_code_fence."""
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from llm import LLMError, _coerce_json, _strip_code_fence


class StripCodeFenceTest(unittest.TestCase):
    def test_strips_json_fence(self) -> None:
        raw = '```json\n{"ok": true, "value": 7}\n```'
        result = _strip_code_fence(raw)
        self.assertEqual(result, '{"ok": true, "value": 7}')

    def test_strips_bare_fence(self) -> None:
        raw = '```\n{"x": 1}\n```'
        self.assertEqual(_strip_code_fence(raw), '{"x": 1}')

    def test_no_fence_passthrough(self) -> None:
        raw = '{"x": 1}'
        self.assertEqual(_strip_code_fence(raw), raw)


class CoerceJsonTest(unittest.TestCase):
    def test_valid_json_passthrough(self) -> None:
        raw = '{"ok": true, "value": 7}'
        result = _coerce_json(raw)
        self.assertEqual(result, raw)

    def test_strips_code_fence_and_returns_json(self) -> None:
        raw = '```json\n{"ok": true, "value": 7}\n```'
        result = _coerce_json(raw)
        import json
        self.assertEqual(json.loads(result), {"ok": True, "value": 7})

    def test_raises_on_invalid_json(self) -> None:
        with self.assertRaises(LLMError):
            _coerce_json('not json at all')

    def test_raises_on_truncated_json(self) -> None:
        truncated = '{"tweet": "text", "total": 61, "misses": ["a", "b"],'
        with self.assertRaises(LLMError):
            _coerce_json(truncated)

    def test_raises_on_fenced_invalid(self) -> None:
        with self.assertRaises(LLMError):
            _coerce_json('```json\nnot valid json\n```')


if __name__ == "__main__":
    unittest.main()
