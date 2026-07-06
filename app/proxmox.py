from urllib.parse import urljoin

import httpx


class ProxmoxError(RuntimeError):
    pass


class ProxmoxClient:
    def __init__(self, endpoint: str, api_token: str, verify_tls: bool = True) -> None:
        base = endpoint.rstrip("/") + "/"
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


def split_token(value: str) -> tuple[str, str]:
    """Accept the provider format token-id=secret and preserve both pieces."""
    if "=" not in value:
        raise ValueError("Proxmox-Token muss das Format user@realm!token-id=secret haben")
    token_id, secret = value.split("=", 1)
    if "!" not in token_id or not secret:
        raise ValueError("Ungültiges Proxmox-Tokenformat")
    return token_id, secret

