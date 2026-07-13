from urllib.parse import urljoin, urlsplit

import httpx


class ProxmoxError(RuntimeError):
    pass


class ProxmoxClient:
    def __init__(self, endpoint: str, api_token: str, verify_tls: bool = True) -> None:
        parsed = urlsplit(endpoint.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Proxmox-Endpoint muss eine vollständige HTTPS-URL sein")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Proxmox-Endpoint darf keine Zugangsdaten, Query oder Fragment enthalten")
        base = endpoint.strip().rstrip("/") + "/"
        self.base_url = urljoin(base, "api2/json/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"PVEAPIToken={api_token}"},
            verify=verify_tls,
            timeout=15,
        )

    def get(self, path: str, **params: object) -> list[dict] | dict:
        try:
            response = self.client.get(path.lstrip("/"), params=params)
            response.raise_for_status()
            return response.json()["data"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProxmoxError(f"Proxmox API fehlgeschlagen: {exc}") from exc

    def discover(self) -> dict:
        nodes = self.get("nodes")
        resources = self.get("cluster/resources", type="vm")
        result = {"nodes": nodes, "vms": resources, "details": {}}
        for node in nodes if isinstance(nodes, list) else []:
            name = node["node"]
            networks = self.get(f"nodes/{name}/network", type="bridge")
            storages = self.get(f"nodes/{name}/storage", content="images")
            result["details"][name] = {"bridges": networks, "storages": storages}
        return result

    def guest_config(self, resource: dict) -> dict:
        """Read one QEMU/LXC config for static-address collision checks."""
        guest_type = str(resource.get("type", ""))
        node = str(resource.get("node", ""))
        vm_id = resource.get("vmid")
        if guest_type not in {"qemu", "lxc"} or not node or vm_id is None:
            raise ProxmoxError("Proxmox-Ressource enthaelt keine lesbare Gast-Konfiguration")
        config = self.get(f"nodes/{node}/{guest_type}/{int(vm_id)}/config")
        if not isinstance(config, dict):
            raise ProxmoxError(f"Proxmox-Konfiguration fuer VM-ID {vm_id} ist ungueltig")
        return config


def split_token(value: str) -> tuple[str, str]:
    """Accept the provider format token-id=secret and preserve both pieces."""
    if "=" not in value:
        raise ValueError("Proxmox-Token muss das Format user@realm!token-id=secret haben")
    token_id, secret = value.split("=", 1)
    if "!" not in token_id or not secret:
        raise ValueError("Ungültiges Proxmox-Tokenformat")
    return token_id, secret
