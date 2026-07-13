from pathlib import Path


def test_legacy_local_cli_artifacts_are_removed():
    project = Path(__file__).parents[1]
    for relative in (
        "app/cli.py",
        "cluster.yaml",
        "Makefile",
        "scripts/preflight.sh",
        "scripts/wait-for-ssh.sh",
        "scripts/check-cluster.sh",
        "scripts/install-local-tools.sh",
        "scripts/create-proxmox-template.sh",
    ):
        assert not (project / relative).exists(), relative

    searchable = [project / "README.md", project / "BUILDER.md", project / "Dockerfile"]
    contents = "\n".join(path.read_text(encoding="utf-8") for path in searchable)
    assert "app.cli" not in contents
    assert ".runtime" not in contents
    assert "FROM worker AS standalone" not in contents


def test_web_templates_do_not_reference_missing_htmx_asset():
    project = Path(__file__).parents[1]
    base = (project / "app/templates/base.html").read_text(encoding="utf-8")
    assert "htmx.min.js" not in base


def test_topbar_brand_links_to_dashboard():
    project = Path(__file__).parents[1]
    base = (project / "app/templates/base.html").read_text(encoding="utf-8")
    assert '<a class="breadcrumb-home" href="/">K8s Universal</a>' in base


def test_terminal_has_no_decorative_window_header():
    project = Path(__file__).parents[1]
    terminal = (project / "app/templates/terminal.html").read_text(encoding="utf-8")
    stylesheet = (project / "app/static/app.css").read_text(encoding="utf-8")
    assert "terminal-frame-header" not in terminal
    assert ".terminal-frame-header" not in stylesheet


def test_application_revisions_and_activity_are_visually_separated():
    project = Path(__file__).parents[1]
    stylesheet = (project / "app/static/app.css").read_text(encoding="utf-8")
    assert ".revision-panel + .jobs-card" in stylesheet
