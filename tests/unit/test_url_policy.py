from app.security.url_policy import ScopeError, check_target, origin_of


def test_allows_explicit_loopback_origin():
    url = check_target("http://127.0.0.1:8000/demo/")
    assert url == "http://127.0.0.1:8000/demo/"


def test_rejects_credentials_without_echoing_them():
    secret = "s3cret-token-value"
    try:
        check_target(f"http://user:{secret}@127.0.0.1:8000/demo/")
    except ScopeError as exc:
        assert secret not in str(exc)
    else:
        raise AssertionError("credentials were accepted")


def test_rejects_metadata_and_link_local():
    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.1.1/",
        "http://metadata.google.internal/",
    ):
        try:
            check_target(url)
        except ScopeError:
            continue
        raise AssertionError(url)


def test_rejects_non_http_and_off_origin_redirect_target():
    for url in ("javascript:alert(1)", "file:///etc/passwd", "http://example.com/"):
        try:
            check_target(url, authorized=origin_of("http://127.0.0.1:8000/demo/"))
        except ScopeError:
            continue
        raise AssertionError(url)


def test_rejects_port_change_on_authorized_origin():
    origin = origin_of("http://127.0.0.1:8000/demo/")
    try:
        check_target("http://127.0.0.1:22/", authorized=origin)
    except ScopeError:
        return
    raise AssertionError("port change was accepted")


def test_private_address_only_when_it_is_the_authorized_host():
    allowed = check_target("http://10.1.2.3:8080/app")
    assert allowed.startswith("http://10.1.2.3:8080/")
    origin = origin_of(allowed)
    try:
        check_target("http://10.9.9.9:8080/", authorized=origin)
    except ScopeError:
        return
    raise AssertionError("a different private host was accepted")
