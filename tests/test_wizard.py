from pathlib import Path


def test_load_balancer_defaults_are_sized_for_keepalived_and_haproxy():
    wizard = (Path(__file__).parents[1] / "app/templates/wizard.html").read_text(encoding="utf-8")
    assert 'name="lb_memory" value="{{ values.get(\'lb_memory\',\'2048\') }}"' in wizard
    assert 'name="lb_disk" value="{{ values.get(\'lb_disk\',\'30\') }}"' in wizard
