from app.mind.advisor import apply_advice
from app.mind.memory import scrub
from app.mind.follow import (
    credentials_for,
    credentials_from_text,
    first_role,
    grounded_action,
    login_from_fields,
    named_roles,
    session_limit,
    off_order,
    open_work,
    order_for_goal,
    plan_fields,
    too_early,
)
from app.mind.plan import decide, goal_allows_account, goal_wants_fresh_account, pages_for_focus, role_of, unique_pages
from app.models import DiscoveredPage


def _page(url: str, title: str, heading: str) -> DiscoveredPage:
    return DiscoveredPage(url=url, title=title, primary_heading=heading, http_status=200, final_url=url)


def test_alias_screens_are_tested_once():
    pages = [
        _page("https://app.example:443/privacy-policy", "Privacy Policy", "Privacy Policy"),
        _page("https://app.example:443/privacy", "Privacy Policy", "Privacy Policy"),
        _page("https://app.example:443/signup", "Create Account", "Create Account"),
        _page("https://app.example:443/login", "Sign In", "Sign in"),
    ]
    unique = unique_pages(pages)
    assert len(unique) == 3
    assert role_of(unique[0]) in {"privacy", "signup", "signin"}
    decision = decide(pages, {})
    assert decision.create_account is True
    assert decision.sign_in is False
    assert decision.signup_url.endswith("/signup")
    remembered = decide(pages, {"signup_done": True, "username": "qaabc123"})
    assert remembered.create_account is False
    assert remembered.sign_in is True
    assert "qaabc123" in remembered.summary
    assert "not sign up again" in remembered.summary


def test_hugging_face_advice_cannot_repeat_signup():
    pages = [
        _page("https://app.example:443/signup", "Create Account", "Create Account"),
        _page("https://app.example:443/login", "Sign In", "Sign in"),
    ]
    decision = decide(pages, {"signup_done": True, "username": "qaabc123"})
    updated = apply_advice(
        decision,
        {"summary": "Create another account at https://evil.example/signup", "create_account": True, "sign_in": False},
        {"signup_done": True, "username": "qaabc123", "password": "secret-value"},
    )
    assert updated.create_account is False
    assert updated.sign_in is True
    assert updated.mind == "built-in"
    assert "evil.example" not in updated.summary
    accepted = apply_advice(
        decide(pages, {}),
        {"summary": "One signup is enough, then sign in and stop.", "create_account": False, "sign_in": True},
        {},
    )
    assert accepted.create_account is True
    assert accepted.sign_in is False
    assert accepted.mind == "huggingface"
    assert "One signup is enough" in accepted.summary


def test_prompt_focus_keeps_only_named_screens():
    pages = [
        _page("https://app.example:443/", "Home", "Welcome"),
        _page("https://app.example:443/signup", "Create Account", "Create Account"),
        _page("https://app.example:443/privacy", "Privacy Policy", "Privacy Policy"),
    ]
    decision = decide(pages, {})
    updated = apply_advice(
        decision,
        {
            "summary": "The request is only the signup screen.",
            "create_account": True,
            "sign_in": False,
            "focus": "signup https://evil.example/signup nope",
        },
        {},
    )
    assert updated.focus == ["signup"]
    kept = pages_for_focus(updated.unique_pages, updated.focus)
    assert [page.url for page in kept] == ["https://app.example:443/signup"]
    assert role_of(pages[0]) == "home"


def test_a_public_prompt_does_not_open_an_account():
    pages = [
        _page("https://insurer.example:443/", "Home", "Insurance"),
        _page("https://insurer.example:443/signup", "Create Account", "Create Account"),
        _page("https://insurer.example:443/login", "Sign In", "Sign in"),
    ]
    decision = decide(pages, {})
    goal = "Check the public homepage. Do not create an account. Do not request a quote."
    assert goal_allows_account(goal) is False
    assert goal_allows_account("test only signup") is True
    assert goal_allows_account("") is True
    updated = apply_advice(
        decision,
        {"summary": "Create an account and buy a policy at https://insurer.example/signup", "create_account": True},
        {},
        goal,
    )
    assert updated.create_account is False
    assert updated.sign_in is False
    assert updated.mind == "built-in"
    assert "buy a policy" not in updated.summary


def test_create_new_user_does_not_reuse_the_remembered_account():
    pages = [
        _page("https://app.example:443/signup", "Create Account", "Create Account"),
        _page("https://app.example:443/login", "Sign In", "Sign in"),
    ]
    memory = {"signup_done": True, "username": "qaabc123"}
    goal = "test only signup and create new user"
    assert goal_wants_fresh_account(goal) is True
    assert goal_wants_fresh_account("test only signup") is False
    assert goal_wants_fresh_account("Do not create a new user") is False
    updated = apply_advice(
        decide(pages, memory),
        {
            "summary": "Create new user",
            "create_account": False,
            "sign_in": True,
            "_source": "meta-llama/Llama-3.3-70B-Instruct",
        },
        memory,
        goal,
    )
    assert updated.create_account is True
    assert updated.sign_in is False
    assert updated.mind == "huggingface:meta-llama/Llama-3.3-70B-Instruct"


def test_memory_summary_hides_the_password():
    secret = "s3cret-token-value"
    assert secret not in scrub(f"failed with {secret}", {"password": secret})


def test_a_request_is_followed_one_grounded_step_at_a_time():
    goal = (
        "check delivery boy flow after sign in as delivery boy. "
        "For that you need to login first as admin and create the delivery boy"
    )
    assert first_role(goal) == "admin"
    assert first_role("check admin flow after login") == ""
    buttons = ["Delivery Boy", "Add delivery boy", "Logout", "Upgrade to Active"]
    assert grounded_action({"do": "click", "target": "Logout"}, buttons=buttons, fields=[], goal=goal) is None
    assert grounded_action({"do": "click", "target": "Add delivery boy"}, buttons=buttons, fields=[], goal=goal) == {
        "do": "click",
        "target": "Add delivery boy",
    }
    assert grounded_action({"do": "click", "target": "Not on screen"}, buttons=buttons, fields=[], goal=goal) is None
    assert grounded_action({"do": "fill", "target": ""}, buttons=buttons, fields=[], goal=goal) is None
    assert grounded_action(
        {"do": "sign_in", "target": "Delivery Boy"}, buttons=buttons, fields=["Name"], goal=goal
    ) == {"do": "sign_in", "target": "Delivery Boy"}
    assert grounded_action({"do": "sign_in", "target": "Settings"}, buttons=buttons, fields=[], goal=goal) is None
    planned = plan_fields(["Name", "Mobile", "Aadhar number", "Password"])
    assert planned["Name"] == "QA Route"
    assert planned["Aadhar number"] == ""
    assert len(planned["Mobile"]) == 10
    saved = login_from_fields(planned)
    assert saved is not None
    assert saved["username"] == planned["Mobile"]
    assert saved["password"] == planned["Password"]
    assert credentials_for({"username": "owner", "password": "owner-secret", "created_logins": [saved]}, "Admin")[
        "username"
    ] == "owner"
    assert credentials_for({"username": "owner", "password": "owner-secret", "created_logins": [saved]}, "Delivery Boy")[
        "username"
    ] == planned["Mobile"]
    ordered = order_for_goal(
        ["Customer", "Milk Buy", "Add a customer", "Add a delivery boy", "Settings"],
        goal,
    )
    assert ordered[0] == "Add a delivery boy"
    ordered_request = "create the delivery boy and copy the credential"
    assert too_early("Copy login details", ordered_request, []) is True
    assert too_early("Copy login details", ordered_request, ["filled the open form"]) is False
    assert named_roles(goal)[0] == "admin"
    started = ["signed in as admin"]
    assert open_work(goal, started).startswith("create ")
    created = [*started, "filled the open form"]
    assert open_work(goal, created) == "sign in as delivery boy"
    copying = goal + " and copy the credential from there"
    assert open_work(copying, created).startswith("copy the credential")
    assert open_work(copying, [*created, "copied the credential"]) == "sign in as delivery boy"
    shown = "On the login page, choose Role: Delivery Boy, then enter:\n  • Username: route1\n  • Password: (not set)\n  • Password: kept-secret"
    parsed = credentials_from_text(shown)
    assert parsed == {"username": "route1", "password": "kept-secret"}
    ordered_copy = order_for_goal(["Customer", "Active", "Copy", "Edit"], copying, "do what the page says: Click Active in the list, then copy")
    assert ordered_copy[0] == "Active"
    assert off_order({"do": "click", "target": "Copy login details"}, open_work(goal, created)) is True
    assert off_order({"do": "click", "target": "Customer"}, "fill the open form") is True
    assert off_order({"do": "sign_in", "target": "admin"}, "open a screen for the signed-in role") is True
    signed_in = [*created, "signed in as delivery boy"]
    assert open_work(goal, signed_in).startswith("open a screen")
    assert open_work(goal, [*signed_in, "opened Today"]) == "the request is complete"
    role_screens = ["Today", "Collect", "Houses"]
    assert open_work(goal, [*signed_in, "opened Today"], role_screens).startswith("open a screen")
    assert open_work(goal, [*signed_in, "opened Today", "opened Collect", "opened Houses"], role_screens) == (
        "the request is complete"
    )
    assert session_limit(goal) > session_limit("open the home page")
    assert session_limit(copying) >= session_limit(goal)
