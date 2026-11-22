from app.discovery.routes import routes_from_script


def test_script_routes_keep_public_screens_and_skip_writes():
    source = """
    window.history.pushState({auth:`login`}, ``, `/login`);
    if ((n === `/privacy-policy`) || n === `/signup/thanks`) {}
    if (n === `/delete` || n === `/api/auth/login`) {}
    var pdf = "/Annot";
    var off = "https://evil.example/phish";
    """
    keep, skip = routes_from_script(source)
    assert keep == ["/login", "/privacy-policy", "/signup/thanks"]
    assert skip == ["/delete"]
    assert "/api/auth/login" not in keep
    assert "/api/auth/login" not in skip
    assert all("evil.example" not in path for path in keep + skip)
    assert "/Annot" not in keep
