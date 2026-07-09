import re
from pathlib import PurePosixPath

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ApplicationBundle, ManifestRevision


PATH_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]*\.ya?ml$")
KIND_PRIORITY = {
    "Namespace": 0,
    "CustomResourceDefinition": 10,
    "ServiceAccount": 20,
    "ClusterRole": 21,
    "ClusterRoleBinding": 22,
    "Role": 23,
    "RoleBinding": 24,
    "ConfigMap": 30,
    "Service": 40,
    "Deployment": 50,
    "StatefulSet": 50,
    "DaemonSet": 50,
    "Ingress": 60,
}


APPLICATION_TEMPLATES = {
    "blank": {
        "name": "Leere Anwendung",
        "description": "Nur Namespace anlegen und eigene Manifestdateien ergaenzen",
    },
    "nginx-demo": {
        "name": "Nginx Demo",
        "description": "Namespace, Deployment, Service und Traefik Ingress",
    },
}


def namespace_template(name: str) -> dict[str, str]:
    return {
        "namespace.yaml": f"""apiVersion: v1
kind: Namespace
metadata:
  name: {name}
""",
    }


def nginx_demo_template(name: str) -> dict[str, str]:
    return {
        "namespace.yaml": """apiVersion: v1
kind: Namespace
metadata:
  name: {name}
""",
        "deployment.yaml": """apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-demo
  namespace: {name}
spec:
  replicas: 3
  selector:
    matchLabels:
      app: nginx-demo
  template:
    metadata:
      labels:
        app: nginx-demo
    spec:
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              app: nginx-demo
      containers:
        - name: nginx
          image: nginx:1.27
          ports:
            - containerPort: 80
""",
        "service.yaml": """apiVersion: v1
kind: Service
metadata:
  name: nginx-demo-service
  namespace: {name}
spec:
  type: ClusterIP
  selector:
    app: nginx-demo
  ports:
    - name: http
      port: 80
      targetPort: 80
""",
        "ingress.yaml": """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: nginx-demo-ingress
  namespace: {name}
spec:
  ingressClassName: traefik
  rules:
    - host: {name}.lab.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: nginx-demo-service
                port:
                  number: 80
""",
    }


def render_application_template(template_id: str, name: str) -> dict[str, str]:
    if template_id == "blank":
        return namespace_template(name)
    if template_id == "nginx-demo":
        return {path: content.format(name=name) for path, content in nginx_demo_template(name).items()}
    raise ValueError("Unbekanntes Anwendungstemplate")


DEFAULT_NGINX_FILES = render_application_template("nginx-demo", "demo")


def validate_manifest_path(path: str) -> str:
    value = path.strip().lower()
    pure = PurePosixPath(value)
    if not PATH_PATTERN.fullmatch(value) or pure.is_absolute() or ".." in pure.parts:
        raise ValueError("Dateiname muss ein relativer, sicherer .yaml- oder .yml-Pfad sein")
    return value


def validate_manifest_content(content: str) -> list[dict]:
    if not content.strip() or len(content.encode()) > 1_000_000:
        raise ValueError("Manifest ist leer oder größer als 1 MB")
    try:
        documents = [item for item in yaml.safe_load_all(content) if item is not None]
    except yaml.YAMLError as exc:
        raise ValueError(f"Ungültiges YAML: {exc}") from exc
    if not documents:
        raise ValueError("Manifest enthält kein Kubernetes-Objekt")
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("Jedes YAML-Dokument muss ein Kubernetes-Objekt sein")
        for key in ("apiVersion", "kind", "metadata"):
            if key not in document:
                raise ValueError(f"Pflichtfeld '{key}' fehlt")
        if not isinstance(document["metadata"], dict) or not document["metadata"].get("name"):
            raise ValueError("metadata.name fehlt")
        if document["kind"] == "Secret":
            raise ValueError("Unverschlüsselte Kubernetes-Secrets sind im Manifest-Editor gesperrt")
    return documents


def snapshot_bundle(bundle: ApplicationBundle) -> dict:
    return {manifest.path: manifest.content for manifest in sorted(bundle.files, key=lambda item: item.path)}


def create_revision(db: Session, bundle: ApplicationBundle, message: str) -> ManifestRevision:
    current = db.scalar(select(func.max(ManifestRevision.version)).where(ManifestRevision.bundle_id == bundle.id)) or 0
    revision = ManifestRevision(bundle_id=bundle.id, version=current + 1, snapshot=snapshot_bundle(bundle), message=message[:255])
    db.add(revision)
    db.flush()
    return revision


def render_snapshot(snapshot: dict) -> tuple[str, list[dict]]:
    documents: list[dict] = []
    for path in sorted(snapshot):
        validate_manifest_path(path)
        documents.extend(validate_manifest_content(str(snapshot[path])))
    documents.sort(key=lambda item: (KIND_PRIORITY.get(str(item.get("kind")), 45), str(item.get("kind")), str(item.get("metadata", {}).get("name"))))
    return yaml.safe_dump_all(documents, sort_keys=False), documents
