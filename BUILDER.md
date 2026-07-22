# Proxmox Kubernetes Cluster Builder

Der Cluster Builder ist die Weboberfläche für dieses Repository. Er verwaltet mehrere Cluster, erzeugt aus einer zentralen Konfiguration die Terraform-/Ansible-Dateien und führt Plan, Apply, Prüfung und Destroy als nachvollziehbare Hintergrundjobs aus.

## Voraussetzungen

- Ubuntu-Orchestrator mit Docker Engine und Docker Compose
- Netzwerkzugriff vom Orchestrator auf Proxmox und alle VM-IP-Adressen
- QEMU-Cloud-Init-Template auf dem Zielnode mit Ubuntu, QEMU Guest Agent und funktionierendem Cloud-Init
- fuer Talos zusaetzlich ein getrenntes Talos-NoCloud-QEMU-Template auf dem Zielnode
- reservierte Node-IP-Adressen und eine freie API-VIP
- VRRP zwischen den Load-Balancern muss im Netzwerk erlaubt sein

## Einmaliges Proxmox-Host-Setup

`proxmox/create-template.sh` und `proxmox/create-talos-template.sh` sind
Bestandteil des Releases, aber bewusst kein CLI-Zugang zur Webanwendung. Sie
bereiten ausschliesslich die externen Proxmox-Voraussetzungen vor, aus denen
Terraform spaeter die VMs klont. Die Skripte werden niemals von Web, Worker,
Docker Compose oder Ansible ausgefuehrt.

### Ubuntu-Template

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

### Talos-NoCloud-Template

Fuer Control Planes und Worker eines Talos-Clusters gibt es ein separates,
stilgleich abgesichertes Hostskript:

```bash
bash proxmox/create-talos-template.sh \
  --vm-id 9200 \
  --storage local-lvm \
  --bridge vmbr0 \
  --talos-version v1.13.6 \
  --install-disk /dev/sda \
  --install-dependencies
```

Auch `9200` ist nur ein Beispiel. Diese ID muss sich von der Ubuntu-Template-ID
und allen Node-VM-IDs unterscheiden. Das Skript laedt das exakt unterstuetzte
Vanilla-NoCloud-Image ueber HTTPS und vergleicht es mit dem im Repository fuer
dieses Image festgehaltenen SHA-256-Wert. Eine abweichende `--image-url` ist nur
zusammen mit einem expliziten `--image-sha256` zulaessig. Image Factory bietet
fuer den oeffentlichen Download derzeit keinen publisherseitigen
Pruefsummen-Endpunkt; eine unerwartete Aenderung des gepinnten Factory-Objekts
stoppt deshalb bewusst, bis der Pin geprueft und aktualisiert wurde.

Das erzeugte Template verwendet OVMF/UEFI, q35, eine EFI-Disk, deaktiviertes
Memory Ballooning und keinen QEMU Guest Agent. Es enthaelt weder Machine Config
noch PKI, SSH-Zugang, Hostname oder IP-Adresse. Eine NoCloud-Disk ist bereits
vorhanden, damit Terraform pro Clone die statische Netzwerkkonfiguration
bereitstellen kann.

Standard und Empfehlung sind `--install-disk /dev/sda` und damit `scsi0` an
einem normalen `virtio-scsi-pci`-Controller. Optional erzeugt
`--install-disk /dev/vda` das Template physisch mit `virtio0`. Der Wert im
Wizard muss exakt zum jeweiligen Template passen; ein `scsi0`-Template darf
nicht einfach als `/dev/vda` verwendet werden. Weitere Parameter zeigt
`bash proxmox/create-talos-template.sh --help`.

Ein Talos-Cluster benoetigt weiterhin das getrennte Ubuntu-Template aus dem
vorigen Abschnitt fuer seine SSH-verwalteten Load Balancer.

## Optionale Orchestrator-VM auf Proxmox

`proxmox/create-orchestrator-vm.sh` erzeugt direkt auf einem Proxmox-Node eine
normale Ubuntu-VM fuer die Anwendung. Es prueft die Signatur der offiziellen
Ubuntu-Pruefsummen, installiert Docker Engine und das Compose-v2-Plugin aus
dem offiziellen Docker-APT-Repository, loest den Git-Ref einmalig auf eine
Commit-SHA auf und legt deren `compose.yaml` sowie `.env.example` unter
`/home/ubuntu/k8s-universal` ab. Eine `.env` mit unsicheren Platzhalterwerten
wird ebenso wenig erzeugt wie die Anwendung automatisch gestartet wird.

Das Skript kann auf dem Proxmox-Host mit `wget` heruntergeladen und vor der
Ausfuehrung geprueft werden:

```bash
wget --https-only -O /root/create-orchestrator-vm.sh \
  https://raw.githubusercontent.com/MidniqhtVibes/K8S-Universal/main/proxmox/create-orchestrator-vm.sh
chmod 700 /root/create-orchestrator-vm.sh
bash /root/create-orchestrator-vm.sh --help
```

Anschliessend wird eine clusterweit freie Ziel-ID uebergeben. `9300` ist nur
ein Beispiel:

```bash
bash /root/create-orchestrator-vm.sh \
  --vm-id 9300 \
  --storage local-lvm \
  --bridge vmbr0 \
  --ssh-key-file /root/.ssh/id_ed25519.pub \
  --install-dependencies
```

Ohne weitere Angaben verwendet die VM Ubuntu 24.04, DHCP, vier vCPUs, 8 GiB
RAM und 40 GiB Disk. Eine explizite Public-Key-Datei ist Pflicht;
`authorized_keys` von Proxmox-root wird bewusst niemals automatisch in die VM
kopiert. Fuer reproduzierbare Installationen kann `--repository-ref` auf einen
Release-Tag oder Commit gesetzt werden. Vor der Erfolgsmeldung wartet das
Skript auf den QEMU Guest Agent und prueft im Gast Cloud-Init, Docker-Daemon,
Compose v2, beide Projektdateien und deren Eigentuemerschaft.

Nach dem ersten Boot sind in der neuen VM nur noch diese Schritte noetig:

```bash
cd /home/ubuntu/k8s-universal
cp .env.example .env
chmod 600 .env
nano .env
docker compose up -d
```

Vor dem Start muessen alle Passwort-Platzhalter und die gewuenschte
`BUILDER_VERSION` gesetzt werden. Fuer Zugriff aus dem internen Netz ist
`BUILDER_BIND_ADDRESS` bewusst auf eine geeignete private Adresse zu setzen;
der Repository-Standard `127.0.0.1` ist nur lokal in der VM erreichbar. Die
Mitgliedschaft in der Docker-Gruppe entspricht praktisch root-Rechten, daher
darf nur ein vertrauenswuerdiger Admin-Key verwendet werden. Ohne TLS-Reverse-
Proxy darf die Anwendung nur in einem vertrauenswuerdigen LAN oder VPN liegen.

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

Die Discovery zeigt für das ausgewählte QEMU-Template auch dessen Disk-Größe.
Load-Balancer-, Control-Plane- und Worker-Disks dürfen nicht kleiner sein. Der
Builder prüft diese Regel beim Speichern sowie erneut vor Terraform-Plan und
-Apply gegen die aktuellen Proxmox-Daten. Fehlt die Größenangabe, wird der
Vorgang mit einer verständlichen Fehlermeldung gestoppt.

### Talos-Cluster

Der Wizard bietet zusätzlich zum unveränderten Ubuntu-/Ansible-/kubeadm-Pfad
den Cluster-Typ **Talos Linux** an. Dabei bleiben die Load Balancer Ubuntu-VMs
mit HAProxy und Keepalived. Nur Control Planes und Worker verwenden Talos und
werden über die Talos-API statt über SSH provisioniert. Deshalb ist die
allgemeine SSH-Konfiguration bei `talos` tatsächlich `null`; ein getrenntes
SSH-Credential bleibt ausschließlich für die beiden Ubuntu-Load-Balancer
erforderlich.

Aktuell ist diese Kombination fest unterstützt:

- Talos `v1.13.6`
- Kubernetes `v1.36` mit dem exakten Patchstand `1.36.2`
- Calico im Talos-Profil mit deaktivierter Standard-CNI, NFTables und VXLAN
- unveränderte Traefik-, Kubeconfig- und Anwendungsfunktionen nach dem Bootstrap

Der Proxmox-Abschnitt benötigt zwei verschiedene vorhandene Templates:

- **Talos-Template** für Control Planes und Worker
- **Ubuntu-/Linux-Template** für die SSH-verwalteten Load Balancer

Das Talos-Template wird nicht automatisch von Webanwendung oder Worker
erstellt; dafuer steht das einmalig auf dem Proxmox-Host auszufuehrende
`proxmox/create-talos-template.sh` bereit. Alternativ muss es ein gleichwertiges
unkonfiguriertes NoCloud-Template der ausgewählten Talos-Version sein. Die von
Terraform erzeugte NoCloud-Netzwerkkonfiguration muss bereits im Maintenance
Mode die im Wizard gewählte statische IP, das Gateway und DNS bereitstellen;
erst danach kann der Worker die Talos-API auf TCP-Port `50000` erreichen. Das
Template darf keine Machine Identity oder Machine Configuration eines anderen
Clusters enthalten. Die Netzwerkschnittstelle (standardmäßig `eth0`) und die
Installationsdisk (`/dev/sda` oder `/dev/vda`) müssen zur VM-Hardware passen.
Der Builder ordnet `/dev/sda` dabei `scsi0` und `/dev/vda` ausschließlich auf
Talos-Nodes `virtio0` zu; das Ubuntu-LB-Template bleibt immer bei `scsi0`.
Firewalls müssen TCP `50000` zu allen Talos-Nodes und TCP `50001` von Workern
zu Control Planes zusätzlich zu den Kubernetes-/etcd-Verbindungen zulassen.
DNS, Zeitsynchronisation sowie ausgehender Zugriff auf das Talos-Installer-Image
und die benötigten Kubernetes-/Calico-Images müssen bereits beim ersten Start
funktionieren.

Für Proxmox gilt zusätzlich: UEFI/q35 und eine normale VirtIO-SCSI-Disk sind
die sichere Ausgangsbasis; SCSI Single und Memory Ballooning sollen vermieden
werden. Der Builder deaktiviert den QEMU Guest Agent auf Talos-Nodes, weil er
ohne die entsprechende Talos-Systemerweiterung nicht verfügbar ist. Auf den
Ubuntu-Load-Balancern bleibt der Agent aktiv.

Der Worker erzeugt die Cluster-PKI einmalig unter
`/data/clusters/<id>/talos/`, setzt Verzeichnisse auf `0700` und sensible
Dateien auf `0600` und schreibt keine Machine Config oder Schlüssel in das
Joblog. Vor einer Änderung werden alle Talos-Nodes vollständig als eigener,
authentifizierter Node oder unkonfigurierter Maintenance-Node klassifiziert.
Ein Node mit fremder PKI oder abweichender Talos-Version stoppt den Lauf, bevor
irgendein Node verändert wird. Der etcd-Bootstrap wird genau einmal angefordert
und anhand des Remote-Zustands bestätigt; ein unklarer CLI-Fehler löst niemals
automatisch einen zweiten Bootstrap aus. Nach erfolgreichem Destroy werden nur
die Bootstrap-Marker entfernt, sodass ein bewusster Neuaufbau mit derselben
Cluster-PKI wieder möglich ist.

Cluster-Typ und Talos-Version sind gesperrt, sobald ein Deployment oder auch
ein partieller Terraform-State existiert. Ein Wechsel oder In-Place-Upgrade ist
nicht Bestandteil dieser Version; dafür muss ein neuer Cluster angelegt werden.
Die bestehende harte Netzwerkregel bleibt erhalten: API-VIP, Gateway und alle
Node-IP-Adressen müssen im VM-CIDR liegen. Geroutete externe VIPs werden in
dieser ersten Talos-Erweiterung nicht freigeschaltet.

Bei einer privaten Registry erzeugt Talos eine native
`RegistryMirrorConfig` für genau den angegebenen direkten Endpoint. HTTP bleibt
auf vertrauenswürdige Labornetze beschränkt. Registry-Authentifizierung,
benutzerdefinierte CA-Zertifikate und das Abschalten der TLS-Prüfung sind nicht
Teil dieser ersten Integration; ein solches Setup muss vorerst außerhalb des
Builders vorbereitet oder als HTTPS-Endpoint mit öffentlich vertrauenswürdiger
CA bereitgestellt werden.

### Optionale Container-Registry

Im Wizard kann unter **Container Registry** genau ein privater Registry-Endpunkt
für den Cluster aktiviert werden. Der Endpoint wird ohne Protokoll und Pfad im
Format `host:port` eingegeben, zum Beispiel:

```text
10.200.50.240:5000
```

Standardmäßig verwendet containerd HTTPS. Die separate HTTP-Option darf nur für
eine vertrauenswürdige interne Lab- oder Testumgebung aktiviert werden; für
produktive Registries ist HTTPS vorgesehen. Der Builder konfiguriert
ausschließlich den angegebenen Endpoint auf Control Planes und Workern und
schaltet die TLS-Prüfung nicht global ab. Während der Ansible-Provisionierung
wird außerdem `http(s)://<endpoint>/v2/` von jedem Kubernetes-Node geprüft.

Nach erfolgreicher Provisionierung kann ein Workload das Image direkt
referenzieren:

```yaml
containers:
  - name: azubiorga
    image: 10.200.50.240:5000/azubiorga:1.0.0
```

Eine manuelle `hosts.toml`-Konfiguration per SSH auf einzelnen Nodes ist für neu
erstellte Cluster damit nicht erforderlich. Ist die Option deaktiviert, bleibt
der bisherige Provisionierungsablauf unverändert.

Destroy ist zweistufig. Zuerst wird nach Eingabe des Clusternamens ein Destroy-Plan erzeugt. Erst dieser unveränderte Plan kann danach angewendet werden.

Nach einem erfolgreichen Destroy erhält der Cluster den Status `destroyed`. Erst dann erscheint die zusätzliche Aktion **Cluster endgültig entfernen**. Sie löscht den Builder-Eintrag sowie dessen lokalen Terraform-State, Kubeconfig, generierte Dateien und Jobdaten. Credentials bleiben erhalten.

### Automatische IP- und VM-ID-Vergabe

Unter **Einstellungen** lassen sich das Standardnetz sowie getrennte IP- und
VM-ID-Pools für Load Balancer, Control Planes und Worker festlegen. Neue Cluster
erhalten daraus automatisch den ersten zusammenhängenden freien Bereich. Aktive
Cluster, manuell reservierte IPs/CIDRs und bei ausgewähltem Proxmox-Credential
bereits vorhandene VM-IDs werden übersprungen. Doppelte Vergaben zwischen vom
Builder verwalteten Clustern werden zusätzlich beim Speichern abgewiesen.

Beim ersten Start sind diese Werte nur sichtbare technische **System Defaults**;
ein Lesezugriff legt keinen Datenbankeintrag an. Erst **Standard-Konfiguration
erstellen** speichert benutzerdefinierte Startwerte für neue Wizard-Aufrufe.
Die Konfiguration bleibt optional, kann zurückgesetzt werden und enthält keine
Tokens oder privaten Schlüssel. Bestehende Cluster werden durch Änderungen an
den Standards nicht verändert.

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

**Alte Revisionen aufräumen** behält die konfigurierte Anzahl neuester
Revisionen sowie jede ältere Revision, die noch in einer Job-Historie verwendet
wird. Die Rückmeldung unterscheidet gelöschte, per Retention behaltene und per
Jobreferenz geschützte Revisionen. Entsprechend entfernt **Alte Historie
aufräumen** auf der Clusterseite nur abgeschlossene Jobs außerhalb des
konfigurierten Limits; laufende und wartende Jobs bleiben immer erhalten.

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
