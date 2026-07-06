import pytest

from app.kubectl_terminal import audit_safe_command, parse_kubectl_command


def test_read_only_kubectl_command():
    command = parse_kubectl_command("get pods -A")
    assert command.verb == "get"
    assert command.mutating is False


def test_mutation_requires_confirmation():
    with pytest.raises(PermissionError):
        parse_kubectl_command("delete pod test")
    assert parse_kubectl_command("delete pod test", True).mutating is True


@pytest.mark.parametrize("command", ["exec -it pod -- sh", "get pods; id", "get pods --kubeconfig=/tmp/evil", "-n demo get pods"])
def test_unsafe_commands_are_blocked(command):
    with pytest.raises(ValueError):
        parse_kubectl_command(command, True)


def test_secret_command_is_redacted_for_audit():
    command = parse_kubectl_command("create secret generic demo --from-literal=password=hello", True)
    assert audit_safe_command(command) == "kubectl create secret [REDACTED]"
