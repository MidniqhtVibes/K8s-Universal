# Proxmox Kubernetes Cluster Builder

Der Cluster Builder ist die Weboberfläche für dieses Repository. Er verwaltet mehrere Cluster, erzeugt aus einer zentralen Konfiguration die Terraform-/Ansible-Dateien und führt Plan, Apply, Prüfung und Destroy als nachvollziehbare Hintergrundjobs aus.

## Voraussetzungen

- Ubuntu-Orchestrator mit Docker Engine und Docker Compose
- Netzwerkzugriff vom Orchestrator auf Proxmox und alle VM-IP-Adressen
- QEMU-Cloud-Init-Template auf dem Zielnode mit Ubuntu, QEMU Guest Agent und funktionierendem Cloud-Init
- reservierte Node-IP-Adressen und eine freie API-VIP
- VRRP zwischen den Load-Balancern muss im Netzwerk erlaubt sein

## Einmaliges Proxmox-Host-Setup

`proxmox/create-template.sh` ist Bestandteil des Releases, aber bewusst kein
CLI-Zugang zur Webanwendung. Es bereitet ausschliesslich die externe
Proxmox-Voraussetzung vor, aus der Terraform spaeter die VMs klont. Das Skript
wird niemals von Web, Worker, Docker Compose oder Ansible ausgefuehrt.

Es muss als `root` direkt auf dem Proxmox-Host laufen, der im Wizard als Node
ausgewaehlt wird:

```bash
bash proxmox/create-template.sh \
  --vm-id 9100 \
  --storage local-lvm \
  --bridge vmbr0 \
  --ubuntu-release noble \
  --install-dependencies
```

Die VM-ID `9100` ist nur ein Beispiel und kein Default. `--vm-id` ist ein
Pflichtwert, wird clusterweit auf Kollisionen geprueft und darf anschliessend
nicht fuer eine Node-VM verwendet werden. `--storage` muss QEMU-Images und eine
Cloud-Init-Disk aufnehmen koennen; `--bridge` muss auf dem lokalen Node
existieren. Standardmaessig wird Ubuntu 24.04 (`noble`) verwendet. Ubuntu 22.04
(`jammy`) ist ebenfalls waehlbar.

Der Ablauf ist absichtlich defensiv:

1. Proxmox-Host, lokaler Node, Storage, Bridge und freie VM-ID pruefen.
2. Geplante Werte anzeigen und die VM-ID interaktiv bestaetigen.
3. Das Ubuntu-Image nur per HTTPS laden und gegen `SHA256SUMS` pruefen.
4. Cloud-Init, OpenSSH, sudo und QEMU Guest Agent im Image vorbereiten.
5. Systemdisk als `scsi0`, Cloud-Init-Disk und VirtIO-Netz konfigurieren.
6. Erst nach erneuter Konfigurationspruefung `qm template` ausfuehren.

Es gibt kein `--force` und keinen automatischen Loeschpfad. Scheitert der Lauf
nach `qm create`, bleibt die unvollstaendige VM zur manuellen Untersuchung
erhalten. Fehlende Hostwerkzeuge werden nur mit dem ausdruecklichen Schalter
`--install-dependencies` installiert. Weitere Parameter zeigt
`bash proxmox/create-template.sh --help`.

Nach der Erstellung muss die Web-Discovery aufgerufen und dort genau das QEMU-
Template mit der ausgegebenen ID auf demselben Node ausgewaehlt werden.

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

Nach einer Änderung der Clusterkonfiguration gelten vorhandene Kubeconfig und Laufzeitstatus nicht mehr als aktuell. Der Builder verlangt dann einen neuen Terraform-Plan und wendet exakt dieses geprüfte Planartefakt an. SSH-Port `22`, Kubernetes-API-Port `6443` und Kubernetes `v1.36` sind die derzeit vollständig unterstützten Werte.

Destroy ist zweistufig. Zuerst wird nach Eingabe des Clusternamens ein Destroy-Plan erzeugt. Erst dieser unveränderte Plan kann danach angewendet werden.

Nach einem erfolgreichen Destroy erhält der Cluster den Status `destroyed`. Erst dann erscheint die zusätzliche Aktion **Cluster endgültig entfernen**. Sie löscht den Builder-Eintrag sowie dessen lokalen Terraform-State, Kubeconfig, generierte Dateien und Jobdaten. Credentials bleiben erhalten.

### Automatische IP- und VM-ID-Vergabe

Unter **Einstellungen** lassen sich das Standardnetz sowie getrennte IP- und
VM-ID-Pools für Load Balancer, Control Planes und Worker festlegen. Neue Cluster
erhalten daraus automatisch den ersten zusammenhängenden freien Bereich. Aktive
Cluster, manuell reservierte IPs/CIDRs und bei ausgewähltem Proxmox-Credential
bereits vorhandene VM-IDs werden übersprungen. Doppelte Vergaben zwischen vom
Builder verwalteten Clustern werden zusätzlich beim Speichern abgewiesen.

Vor dem Terraform-Plan und erneut unmittelbar vor dessen Anwendung prüft der
Worker die Zielumgebung direkt über die Proxmox-API. Fremde Ressourcen mit
einer angeforderten VM-ID, einem erzeugten VM-Namen oder einer bereits in
`ipconfigN` beziehungsweise `netN` hinterlegten statischen IPv4-Adresse
blockieren den Lauf mit einer konkreten Fehlermeldung. Ressourcen, die laut
Terraform-State demselben Cluster gehören, werden dabei bewusst ausgenommen.
Manuell innerhalb eines Gastbetriebssystems gesetzte Adressen, die nicht in der
Proxmox-Konfiguration stehen, bleiben über **Reservierte IPs/CIDRs** abzusichern.

Der Schalter **Clustername im Proxmox-VM-Namen** erzeugt Namen wie
`produktion-control-01`. Bestehende Cluster behalten ohne aktivierten Schalter
ihre bisherigen VM-Namen.

## kubectl-Terminal

Nach erfolgreichem Apply ist das clustergebundene Terminal über die Sidebar oder die Clusteransicht erreichbar. Read-only-Befehle wie `get`, `describe` und `logs` funktionieren direkt. Mutierende Befehle benötigen den aktivierten Administrationsmodus und eine Einzelbestätigung. Allgemeine Shellbefehle, Verbindungsoptionen sowie interaktive TTY-Funktionen sind gesperrt.

## Anwendungs-Bundles

Jeder Cluster besitzt eine Anwendungsverwaltung mit mehreren YAML-Dateien pro Bundle. Neue Cluster starten ohne vorinstallierte Anwendung. Beim Anlegen einer Anwendung kann entweder ein leeres Namespace-Template oder das Nginx-Demo-Template mit `namespace.yaml`, `deployment.yaml`, `service.yaml` und `ingress.yaml` ausgewählt werden. Das Beispiel ist zunächst nur ein Entwurf und wird nicht ungefragt ausgerollt.

Der vorgesehene Ablauf lautet:

1. Manifestdateien im YAML-Editor bearbeiten und als Revision speichern.
2. **Serverseitig validieren** ausführen.
3. Mit **Diff anzeigen** die Änderungen gegenüber dem Cluster prüfen.
4. **Bundle anwenden** bestätigen und den Rollout im Joblog verfolgen.
5. Optional mit **Aus Cluster entfernen** die deklarierten Ressourcen wieder löschen und danach den Builder-Eintrag entfernen.

Jedes Speichern und jeder Lauf erzeugt eine unveränderliche Revision. Frühere Revisionen können als neuer Entwurf wiederhergestellt werden. Unverschlüsselte Ressourcen vom Typ `Secret` sind gesperrt; dafür ist später eine Integration mit SOPS oder Sealed Secrets vorgesehen.

## Proxmox-Berechtigungen

Das Token soll nur die für VM-Cloning, VM-Konfiguration, Storage-Abfrage und Ressourcenerkennung benötigten Rechte besitzen. Die Kollisionsprüfung muss mit `VM.Audit` die Konfiguration der für das Token sichtbaren QEMU-VMs und LXC-Container lesen können. Kein Root-Passwort verwenden. Der genaue Rechteumfang hängt von Proxmox-Version, Pool- und Storage-Struktur ab und muss vor dem ersten echten Plan in der Zielumgebung geprüft werden.

Tokenformat für den Builder:

```text
user@realm!token-id=token-secret
```

## Persistenz und Sicherung

Docker-Volumes:

- `postgres-data`: Benutzer, verschlüsselte Credentials, Cluster, Jobs und Auditdaten
- `cluster-data`: zentrale Konfigurationen, Terraform-State, Pläne, generierte Dateien, Kubeconfigs und Logs

Für eine vollständige Wiederherstellung werden beide Volumes und der unveränderte `MASTER_KEY` benötigt. Kubeconfigs und Terraform-State sind sensible Daten und müssen verschlüsselt gesichert werden.

## Grenzen des MVP

- Ein Administrator, keine Rollenverwaltung
- genau eine Proxmox-Umgebung, mehrere getrennte Cluster
- lokaler Terraform-State pro Cluster
- keine Worker-Skalierung und keine Kubernetes-Upgrades
- Calico und Traefik als unterstützte Addons
- HTTP nur für ein vertrauenswürdiges internes Netz; HTTPS ist der nächste Härtungsschritt
