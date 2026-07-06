import re
import shlex
from dataclasses import dataclass


READ_ONLY_VERBS = {
    "api-resources", "api-versions", "auth", "cluster-info", "describe", "diff",
    "explain", "get", "logs", "options", "top", "version", "wait",
}
BLOCKED_VERBS = {"attach", "cp", "debug", "exec", "port-forward", "proxy"}
CONNECTION_FLAGS = {
    "--kubeconfig", "--server", "--token", "--certificate-authority",
    "--client-certificate", "--client-key", "--username", "--password",
}
SHELL_TOKENS = re.compile(r"(^|\s)(\||&&|;|>|<|`|\$\()")


@dataclass(frozen=True)
class KubectlCommand:
    args: list[str]
    verb: str
    mutating: bool


def parse_kubectl_command(raw: str, confirm_mutation: bool = False) -> KubectlCommand:
    command = raw.strip()
    if not command or len(command) > 1000:
        raise ValueError("Befehl ist leer oder zu lang")
    if SHELL_TOKENS.search(command):
        raise ValueError("Shell-Operatoren sind in der kubectl-Konsole nicht erlaubt")
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Befehl kann nicht gelesen werden: {exc}") from exc
    if args and args[0] == "kubectl":
        args = args[1:]
    if not args or len(args) > 64:
        raise ValueError("Kein kubectl-Befehl oder zu viele Argumente")
    if args[0].startswith("-"):
        raise ValueError("Bitte zuerst den kubectl-Unterbefehl angeben, zum Beispiel: get pods -A")
    lowered = [item.lower() for item in args]
    for flag in CONNECTION_FLAGS:
        if flag in lowered or any(item.startswith(flag + "=") for item in lowered):
            raise ValueError(f"Verbindungsoption {flag} wird vom Cluster Builder verwaltet")
    verb = args[0].lower()
    if verb in BLOCKED_VERBS:
        raise ValueError(f"Interaktiver Befehl '{verb}' wird in Version 1 nicht unterstützt")
    mutating = verb not in READ_ONLY_VERBS
    if mutating and not confirm_mutation:
        raise PermissionError(f"Mutierender kubectl-Befehl '{verb}' benötigt eine Bestätigung")
    return KubectlCommand(args=args, verb=verb, mutating=mutating)


def audit_safe_command(command: KubectlCommand) -> str:
    if command.verb == "create" and "secret" in [item.lower() for item in command.args]:
        return "kubectl create secret [REDACTED]"
    safe: list[str] = []
    redact_next = False
    for item in command.args:
        lowered = item.lower()
        if redact_next:
            safe.append("[REDACTED]")
            redact_next = False
        elif any(marker in lowered for marker in ("password", "token", "secret", "from-literal")):
            if "=" in item:
                safe.append(item.split("=", 1)[0] + "=[REDACTED]")
            else:
                safe.append(item)
                redact_next = True
        else:
            safe.append(item)
    return "kubectl " + " ".join(safe)
