from app.execution.verdict import judge, should_retain_media
from app.models import Classification, FailureKind, StepResult, Verdict


def _step(action, passed, kind="assertion", error=""):
    return StepResult(action=action, passed=passed, expected="2xx", actual="404", error=error, kind=kind)


def test_assertion_failure_is_product_fail():
    verdict, classification, kind, _reason = judge([_step("assert_http_status", False)])
    assert verdict is Verdict.FAIL
    assert classification is Classification.PRODUCT_BUG
    assert kind is FailureKind.REACHABILITY


def test_runtime_assertion_is_a_different_failure_kind():
    verdict, _classification, kind, _reason = judge([_step("assert_no_page_errors", False)])
    assert verdict is Verdict.FAIL
    assert kind is FailureKind.RUNTIME_ERROR


def test_runner_timeout_is_inconclusive():
    verdict, classification, kind, reason = judge(
        [_step("goto", False, kind="runner", error="navigation timeout")],
        runner_error="navigation timeout",
    )
    assert verdict is Verdict.INCONCLUSIVE
    assert classification is Classification.ENVIRONMENT
    assert kind is FailureKind.NONE
    assert "timeout" in reason


def test_policy_block_is_not_a_product_failure():
    verdict, classification, _kind, _reason = judge([], blocked_reason="destructive action is denied")
    assert verdict is Verdict.BLOCKED
    assert classification is Classification.BLOCKED


def test_pass_and_media_retention():
    verdict, classification, _kind, _reason = judge([_step("assert_title", True)])
    assert verdict is Verdict.PASS
    assert classification is Classification.NONE
    assert should_retain_media(Verdict.PASS, retain_pass_media=False) is False
    assert should_retain_media(Verdict.FAIL, retain_pass_media=False) is True
    assert should_retain_media(Verdict.INCONCLUSIVE, retain_pass_media=False) is True
