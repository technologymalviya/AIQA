from pathlib import Path

import pytest

from app.mind.hands import hands_off
from app.models import Provenance
from app.security.paths import PathSafetyError, require_case_id, safe_child
from app.security.text import markdown_text, noise_console, redact


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


def test_hands_skip_quotes_and_ignore_missing_images():
    assert hands_off("Get a quote")
    assert hands_off("Sign in")
    assert hands_off("Skip to main content")
    assert not hands_off("Renters")
    assert hands_off("Create account")
    assert not hands_off("Create account", allow_account=True)
    assert hands_off("Send", allow_account=True)
    assert noise_console("Failed to load resource: the server responded with a status of 404 ()")
    assert noise_console("Permissions policy violation: unload is not allowed in this document.")
    assert not noise_console("Uncaught TypeError: cannot read properties of null")


def test_provenance_values():
    assert {item.value for item in Provenance} == {"llm", "heuristic", "manual"}
