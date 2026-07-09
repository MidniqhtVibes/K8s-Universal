# Proxmox Kubernetes Cluster Builder

Der Cluster Builder ist die Weboberfläche für dieses Repository. Er verwaltet mehrere Cluster, erzeugt aus einer zentralen Konfiguration die Terraform-/Ansible-Dateien und führt Plan, Apply, Prüfung und Destroy als nachvollziehbare Hintergrundjobs aus.

## Voraussetzungen

- Ubuntu-Orchestrator mit Docker Engine und Docker Compose
- Netzwerkzugriff vom Orchestrator auf Proxmox und alle VM-IP-Adressen
- Proxmox Cloud-Init-Template mit Ubuntu, QEMU Guest Agent und funktionierendem Cloud-Init
- reservierte Node-IP-Adressen und eine freie API-VIP
- VRRP zwischen den Load-Balancern muss im Netzwerk erlaubt sein

## Installation

```bash
cp .env.example .env
chmod 600 .env
```

Ersetze in `.env` alle Beispielwerte durch unabhängige lange Zufallswerte. Besonders `MASTER_KEY` darf nach dem Speichern von Credentials nicht verloren gehen oder geändert werden, da vorhandene Secrets sonst nicht mehr entschlüsselt werden können.

Die Anwendung bindet standardmäßig nur an `127.0.0.1:8000`. Für Zugriff aus dem internen Netz muss `BUILDER_BIND_ADDRESS` bewusst auf die private IP der Orchestrator-VM gesetzt werden. Da Version 1 HTTP verwendet, darf der Dienst nicht in ein öffentliches oder nicht vertrauenswürdiges Netz exponiert werden.

```bash
docker compose up --build -d
docker compose logs -f web worker
```

Danach mit Benutzer `admin` und `INITIAL_ADMIN_PASSWORD` anmelden. Das initiale Passwort wird nur beim ersten Datenbankstart verwendet.

## Bedienung

1. Unter **Credentials** ein Proxmox-Token testen und speichern.
2. Ein vorhandenes SSH-Schlüsselpaar hochladen oder ein Ed25519-Paar erzeugen.
3. Einen neuen Cluster öffnen und das Proxmox-Credential auswählen.
4. Mit **Proxmox-Ressourcen erkennen** Nodes, Bridges, Storages, Templates und vorhandene VMs prüfen.
5. Netzwerk und Topologie ausfüllen und speichern.
6. Einen Terraform-Plan erzeugen und im Joblog kontrollieren.
7. Nur den unveränderten Plan anwenden.
8. Nach erfolgreichem Aufbau werden Ansible, Calico, optional Traefik und die Clusterprüfung ausgeführt.

Destroy ist zweistufig. Zuerst wird nach Eingabe des Clusternamens ein Destroy-Plan erzeugt. Erst dieser unveränderte Plan kann danach angewendet werden.

Nach einem erfolgreichen Destroy erhält der Cluster den Status `destroyed`. Erst dann erscheint die zusätzliche Aktion **Cluster endgültig entfernen**. Sie löscht den Builder-Eintrag sowie dessen lokalen Terraform-State, Kubeconfig, generierte Dateien und Jobdaten. Credentials bleiben erhalten.

### Automatische IP- und VM-ID-Vergabe

Unter **Einstellungen** lassen sich das Standardnetz sowie getrennte IP- und
VM-ID-Pools fÃ¼r Load Balancer, Control Planes und Worker festlegen. Neue Cluster
erhalten daraus automatisch den ersten zusammenhÃ¤ngenden freien Bereich. Aktive
Cluster, manuell reservierte IPs/CIDRs und bei ausgewÃ¤hltem Proxmox-Credential
bereits vorhandene VM-IDs werden Ã¼bersprungen. Doppelte Vergaben zwischen vom
Builder verwalteten Clustern werden zusÃ¤tzlich beim Speichern abgewiesen.

Der Schalter **Clustername im Proxmox-VM-Namen** erzeugt Namen wie
`produktion-control-01`. Bestehende Cluster behalten ohne aktivierten Schalter
ihre bisherigen VM-Namen.

## kubectl-Terminal

Nach erfolgreichem Apply ist das clustergebundene Terminal über die Sidebar oder die Clusteransicht erreichbar. Read-only-Befehle wie `get`, `describe` und `logs` funktionieren direkt. Mutierende Befehle benötigen den aktivierten Administrationsmodus und eine Einzelbestätigung. Allgemeine Shellbefehle, Verbindungsoptionen sowie interaktive TTY-Funktionen sind gesperrt.

## Anwendungs-Bundles

Jeder Cluster besitzt eine Anwendungsverwaltung mit mehreren YAML-Dateien pro Bundle. Beim ersten Öffnen wird automatisch `nginx-demo` mit `namespace.yaml`, `deployment.yaml`, `service.yaml` und `ingress.yaml` angelegt. Das Beispiel ist zunächst nur ein Entwurf und wird nicht ungefragt ausgerollt.

Der vorgesehene Ablauf lautet:

1. Manifestdateien im YAML-Editor bearbeiten und als Revision speichern.
2. **Serverseitig validieren** ausführen.
3. Mit **Diff anzeigen** die Änderungen gegenüber dem Cluster prüfen.
4. **Bundle anwenden** bestätigen und den Rollout im Joblog verfolgen.

Jedes Speichern und jeder Lauf erzeugt eine unveränderliche Revision. Frühere Revisionen können als neuer Entwurf wiederhergestellt werden. Unverschlüsselte Ressourcen vom Typ `Secret` sind gesperrt; dafür ist später eine Integration mit SOPS oder Sealed Secrets vorgesehen.

## Proxmox-Berechtigungen

Das Token soll nur die für VM-Cloning, VM-Konfiguration, Storage-Abfrage und Ressourcenerkennung benötigten Rechte besitzen. Kein Root-Passwort verwenden. Der genaue Rechteumfang hängt von Proxmox-Version, Pool- und Storage-Struktur ab und muss vor dem ersten echten Plan in der Zielumgebung geprüft werden.

Tokenformat für den Builder:

```text
user@realm!token-id=token-secret
```

## Persistenz und Sicherung

Docker-Volumes:

- `postgres-data`: Benutzer, verschlüsselte Credentials, Cluster, Jobs und Auditdaten
- `cluster-data`: zentrale Konfigurationen, Terraform-State, Pläne, generierte Dateien, Kubeconfigs und Logs

Für eine vollständige Wiederherstellung werden beide Volumes und der unveränderte `MASTER_KEY` benötigt. Kubeconfigs und Terraform-State sind sensible Daten und müssen verschlüsselt gesichert werden.

## Entwicklerworkflow

Eine öffentliche, secret-freie `cluster.yaml` kann auch ohne Weboberfläche gerendert werden:

```bash
python3 -m app.cli render --config cluster.yaml --output .runtime --source .
```

Die Make-Targets `render`, `plan`, `infra`, `ping`, `k8s` und `check` arbeiten anschließend im isolierten `.runtime`-Verzeichnis. Credentials werden dabei weiterhin extern über Umgebungsvariablen und temporäre Dateien erwartet.

## Grenzen des MVP

- Ein Administrator, keine Rollenverwaltung
- genau eine Proxmox-Umgebung, mehrere getrennte Cluster
- lokaler Terraform-State pro Cluster
- keine Worker-Skalierung und keine Kubernetes-Upgrades
- Calico und Traefik als unterstützte Addons
- HTTP nur für ein vertrauenswürdiges internes Netz; HTTPS ist der nächste Härtungsschritt
