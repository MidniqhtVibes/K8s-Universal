import re
from hashlib import sha256
from math import pow
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import APP_CSS_VERSION, THEMES_JS_VERSION, app


PROJECT_ROOT = Path(__file__).parents[1]
THEME_IDS = ("standard", "github-dark", "hello-kitty", "light")
THEME_STORAGE_KEY = "k8s-universal-theme"


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "test-admin-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _rule_body(css: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(?P<body>[^{}]*)\}", css)
    assert match is not None, f"CSS-Regel fuer {selector!r} fehlt"
    return match.group("body")


def _rule_bodies_containing(css: str, selector_fragment: str) -> str:
    bodies = []
    for match in re.finditer(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", css):
        if selector_fragment in match.group("selectors"):
            bodies.append(match.group("body"))
    assert bodies, f"CSS-Regel fuer {selector_fragment!r} fehlt"
    return "\n".join(bodies)


def test_themes_page_requires_login_and_renders_all_theme_choices():
    with TestClient(app) as client:
        anonymous = client.get("/themes", follow_redirects=False)
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/login"

        _login(client)
        response = client.get("/themes")

    assert response.status_code == 200
    assert re.search(r"<h1[^>]*>\s*Themes\s*</h1>", response.text)
    assert re.search(r'class="[^"]*\btheme-current-name\b', response.text)
    for theme_id in THEME_IDS:
        assert re.search(
            rf'class="[^"]*\btheme-card\b[^"]*"[^>]*data-theme-option="{re.escape(theme_id)}"',
            response.text,
        )
    assert ">Dark<" in response.text
    assert "GitHub Dark" not in response.text


def test_themes_navigation_link_is_directly_above_credentials():
    with TestClient(app) as client:
        _login(client)
        response = client.get("/themes")

    assert response.status_code == 200
    themes_link = re.search(r'<a\b[^>]*href="/themes"[^>]*>', response.text)
    credentials_link = re.search(r'<a\b[^>]*href="/credentials"[^>]*>', response.text)
    assert themes_link is not None
    assert credentials_link is not None
    assert themes_link.start() < credentials_link.start()

    between_links = response.text[themes_link.start() : credentials_link.start()]
    assert "Themes" in between_links
    assert re.search(r'nav-text">\s*Themes\s*</span>', between_links)


def test_theme_bootstrap_runs_before_app_stylesheet_to_avoid_a_flash():
    base = (PROJECT_ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    stylesheet = re.search(r'<link\b[^>]*href="/static/app\.css(?:\?[^\"]*)?"[^>]*>', base)
    assert stylesheet is not None

    before_stylesheet = base[: stylesheet.start()]
    bootstrap_scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", before_stylesheet, re.DOTALL)
    bootstrap = next((script for script in bootstrap_scripts if THEME_STORAGE_KEY in script), None)
    assert bootstrap is not None, "Theme-Bootstrap muss vor app.css stehen"

    assert "localStorage.getItem" in bootstrap
    assert "document.documentElement" in bootstrap
    assert ".dataset.theme" in bootstrap or 'setAttribute("data-theme"' in bootstrap
    assert ".style.colorScheme" in bootstrap
    assert (
        'meta[name="theme-color"]' in bootstrap
        or "meta[name='theme-color']" in bootstrap
        or "#theme-color" in bootstrap
        or "getElementById('theme-color')" in bootstrap
        or 'getElementById("theme-color")' in bootstrap
    )
    assert "standard" in bootstrap

    assert re.search(r'<html\b[^>]*\bdata-theme="standard"', base)
    assert re.search(r'<meta\b[^>]*name="color-scheme"[^>]*content="dark light"', base)
    assert re.search(r'<meta\b[^>]*name="theme-color"[^>]*content="#[0-9a-fA-F]{6}"', base)


def test_theme_assets_use_content_hashes_to_avoid_stale_palettes():
    stylesheet = PROJECT_ROOT / "app/static/app.css"
    javascript = PROJECT_ROOT / "app/static/themes.js"
    expected_css_version = sha256(stylesheet.read_bytes()).hexdigest()[:12]
    expected_js_version = sha256(javascript.read_bytes()).hexdigest()[:12]

    assert APP_CSS_VERSION == expected_css_version
    assert THEMES_JS_VERSION == expected_js_version

    with TestClient(app) as client:
        _login(client)
        response = client.get("/themes")

    assert response.status_code == 200
    assert f'/static/app.css?v={expected_css_version}' in response.text
    assert f'/static/themes.js?v={expected_js_version}' in response.text


def test_theme_javascript_persists_and_applies_the_selection_globally():
    base = (PROJECT_ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    javascript_path = PROJECT_ROOT / "app/static/themes.js"
    assert javascript_path.is_file()
    javascript = javascript_path.read_text(encoding="utf-8")

    assert re.search(r'<script\b[^>]*src="/static/themes\.js(?:\?[^\"]*)?"', base)
    assert THEME_STORAGE_KEY in javascript
    assert "localStorage.getItem" in javascript
    assert "localStorage.setItem" in javascript
    assert "document.documentElement" in javascript
    assert ".dataset.theme" in javascript or 'setAttribute("data-theme"' in javascript
    assert ".style.colorScheme" in javascript
    assert 'meta[name="theme-color"]' in javascript or "meta[name='theme-color']" in javascript
    assert "theme-current-name" in javascript
    assert "data-theme-option" in javascript
    assert "aria-pressed" in javascript
    assert "is-active" in javascript
    assert "addEventListener" in javascript
    assert "K8S_THEME_CONFIG" in base and "K8S_THEME_CONFIG" in javascript
    for theme_id in THEME_IDS:
        assert theme_id in base


def test_every_theme_defines_a_color_scheme_and_semantic_palette():
    css = (PROJECT_ROOT / "app/static/app.css").read_text(encoding="utf-8")
    theme_rules = {
        "standard": _rule_body(css, ":root"),
        "github-dark": _rule_body(css, 'html[data-theme="github-dark"]'),
        "hello-kitty": _rule_body(css, 'html[data-theme="hello-kitty"]'),
        "light": _rule_body(css, 'html[data-theme="light"]'),
    }

    semantic_tokens = {
        "--button-primary-bg",
        "--button-primary-hover-bg",
        "--button-text",
        "--button-secondary-bg",
        "--feature-accent",
        "--input-line",
        "--input-bg",
        "--input-focus-bg",
        "--success-bg",
        "--terminal-link",
    }
    semantic_tokens.update(
        f"--state-{state}-{part}"
        for state in ("success", "info", "warning", "danger", "pending", "inactive")
        for part in ("line", "bg", "text")
    )
    semantic_tokens.update(
        f"--role-{role}-{part}"
        for role in ("load-balancer", "control-plane", "worker")
        for part in ("line", "bg", "text")
    )

    for theme_id, declarations in theme_rules.items():
        assert re.search(r"(?:--)?color-scheme\s*:\s*(?:dark|light)", declarations), theme_id
        for token in semantic_tokens:
            assert re.search(rf"{re.escape(token)}\s*:", declarations), f"{token} fehlt in {theme_id}"
        assert re.search(r"--switch-[\w-]+\s*:", declarations), f"Switch-Farbe fehlt in {theme_id}"
        assert re.search(r"--button-danger-[\w-]+\s*:", declarations), f"Danger-Button fehlt in {theme_id}"


def test_semantic_theme_colors_are_used_by_controls_statuses_and_roles():
    css = (PROJECT_ROOT / "app/static/app.css").read_text(encoding="utf-8")

    assert "var(--button-text)" in _rule_bodies_containing(css, ".button,")
    assert "var(--button-primary-bg)" in _rule_bodies_containing(css, ".button,")
    assert "var(--button-primary-hover-bg)" in _rule_bodies_containing(css, ".button:hover,")
    assert "var(--button-secondary-bg)" in _rule_bodies_containing(css, ".button.secondary")
    assert "var(--button-danger-bg)" in _rule_bodies_containing(css, ".button.danger")

    form_controls = _rule_bodies_containing(css, "input,")
    assert "var(--input-line)" in form_controls
    assert "var(--input-bg)" in form_controls
    assert "var(--input-focus-bg)" in _rule_bodies_containing(css, "input:focus")
    assert "var(--switch-bg)" in _rule_bodies_containing(css, ".switch-field input")

    status_selectors = {
        ".badge.ready": "--state-success-",
        ".badge.planned": "--state-info-",
        ".badge.running": "--state-warning-",
        ".badge.failed": "--state-danger-",
        ".badge.draft": "--state-pending-",
        ".badge.destroyed": "--state-inactive-",
    }
    for selector, token_prefix in status_selectors.items():
        assert f"var({token_prefix}" in _rule_bodies_containing(css, selector)

    role_selectors = {
        ".role-badge.role-loadbalancer": "--role-load-balancer-",
        ".role-badge.role-control_plane": "--role-control-plane-",
        ".role-badge.role-worker": "--role-worker-",
    }
    for selector, token_prefix in role_selectors.items():
        declarations = _rule_bodies_containing(css, selector)
        for part in ("line", "bg", "text"):
            assert f"var({token_prefix}{part})" in declarations

    load_balancer_cards = _rule_bodies_containing(css, ".role-config-card.role-lb")
    assert "var(--role-load-balancer-line)" in load_balancer_cards

    assert "var(--feature-accent)" in _rule_bodies_containing(css, ".application-icon")
    assert "var(--success-bg)" in _rule_bodies_containing(css, ".alert-success")
    assert "var(--success)" in _rule_bodies_containing(css, ".cluster-dot.ready")
    assert "var(--danger)" in _rule_bodies_containing(css, ".cluster-dot.failed")
    assert "var(--blue)" in _rule_bodies_containing(css, ".cluster-dot.planned")
    assert "var(--warning)" in _rule_bodies_containing(css, ".cluster-dot.applying")


def _theme_variables(css: str, selector: str) -> dict[str, str]:
    variables = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", _rule_body(css, selector)):
        variables[name] = value.strip()
    return variables


def _resolve_hex(variables: dict[str, str], name: str) -> str:
    value = variables[name]
    seen = {name}
    while match := re.fullmatch(r"var\((--[\w-]+)\)", value):
        name = match.group(1)
        assert name not in seen, f"Zyklische Theme-Variable: {name}"
        seen.add(name)
        value = variables[name]
    assert re.fullmatch(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}", value), (name, value)
    if len(value) == 4:
        value = "#" + "".join(character * 2 for character in value[1:])
    return value


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else pow((channel + 0.055) / 1.055, 2.4) for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_core_text_links_and_primary_buttons_meet_wcag_aa_contrast():
    css = (PROJECT_ROOT / "app/static/app.css").read_text(encoding="utf-8")
    base_variables = _theme_variables(css, ":root")
    selectors = {
        "standard": ":root",
        "github-dark": 'html[data-theme="github-dark"]',
        "hello-kitty": 'html[data-theme="hello-kitty"]',
        "light": 'html[data-theme="light"]',
    }
    pairs = (
        ("--text", "--bg"),
        ("--muted", "--bg"),
        ("--accent", "--bg"),
        ("--button-text", "--button-primary-bg"),
        ("--button-text", "--button-primary-hover-bg"),
    )

    for theme_name, selector in selectors.items():
        variables = {**base_variables, **(_theme_variables(css, selector) if selector != ":root" else {})}
        for foreground, background in pairs:
            ratio = _contrast_ratio(_resolve_hex(variables, foreground), _resolve_hex(variables, background))
            assert ratio >= 4.5, f"{theme_name}: {foreground}/{background} hat nur {ratio:.2f}:1"
