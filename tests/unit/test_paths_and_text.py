from pathlib import Path

import pytest

from app.models import Provenance
from app.security.paths import PathSafetyError, require_case_id, safe_child
from app.security.text import markdown_text, redact


def test_case_id_and_path_escape():
    assert require_case_id("TC-AB12CD34") == "TC-AB12CD34"
    with pytest.raises(PathSafetyError):
        require_case_id("../../etc/passwd")
    root = Path("/tmp/qa-root")
    with pytest.raises(PathSafetyError):
        safe_child(root, "..", "etc")
    with pytest.raises(PathSafetyError):
        safe_child(root, "TC-AB12CD34", "..", "video.webm")
    assert safe_child(root, "TC-AB12CD34", "video.webm").name == "video.webm"


def test_redaction_and_encoding():
    leaked = redact("Authorization bearer abc.def and sk-abcdefghi123 hf_exampletoken12345678")
    assert "abc.def" not in leaked
    assert "sk-abcdefghi123" not in leaked
    assert "hf_exampletoken12345678" not in leaked
    encoded = markdown_text("hello <script>alert(1)</script>\x00\u202ehidden")
    assert "<script>" not in encoded
    assert "\x00" not in encoded
    assert "\u202e" not in encoded


def test_provenance_values():
    assert {item.value for item in Provenance} == {"llm", "heuristic", "manual"}
