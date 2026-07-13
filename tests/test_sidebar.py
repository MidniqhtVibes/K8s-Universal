import re
from pathlib import Path


def test_sidebar_has_collapsible_cluster_section_and_bottom_admin_links():
    project = Path(__file__).parents[1]
    base = (project / "app/templates/base.html").read_text(encoding="utf-8")
    cluster = (project / "app/templates/cluster.html").read_text(encoding="utf-8")
    css = (project / "app/static/app.css").read_text(encoding="utf-8")
    script = (project / "app/static/sidebar.js").read_text(encoding="utf-8")

    assert 'data-sidebar-section="clusters"' in base
    assert '<summary class="sidebar-summary' in base
    assert 'class="new-cluster-link' in base
    assert base.index('class="new-cluster-link') > base.index('</details>')
    assert 'class="sidebar-bottom"' in base
    assert '/credentials' in base and '/settings' in base
    assert "ui.icon('key')" in base and '<span class="nav-text">Credentials</span>' in base
    assert "ui.icon('settings')" in base and '<span class="nav-text">Einstellungen</span>' in base
    assert '<span class="nav-icon">O</span>' not in base
    assert '/static/sidebar.js' in base
    assert base.index("localStorage.getItem('cluster-builder-sidebar-collapsed')") < base.index('<aside class="sidebar"')
    assert 'class="environment"' not in base
    assert 'http-warning' not in base
    assert '<span class="nav-text">Anwendungen</span>' not in base
    assert 'href="/clusters/{{ cluster.id }}/applications"' in cluster
    assert "'/applications' not in request.url.path" not in base

    assert "body.sidebar-collapsed" in css
    assert re.search(r"\.sidebar-bottom\s*\{[^}]*margin-top:\s*auto", css)
    assert ".cluster-links" in css
    assert re.search(r"body\.sidebar-collapsed\s+\.sidebar-toggle\s+\.icon\s*\{[^}]*rotate\(180deg\)", css)

    assert "cluster-builder-sidebar-collapsed" in script
    assert "cluster-builder-clusters-open" in script
