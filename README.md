# Proxmox Kubernetes Cluster Builder

Webbasierter Builder fuer HA-Kubernetes-Cluster auf Proxmox. Die Anwendung erzeugt aus einem Wizard Terraform- und Ansible-Konfigurationen, erstellt die Proxmox-VMs, installiert Kubernetes, Calico und optional Traefik, und verwaltet einfache Kubernetes-Anwendungen als Manifest-Bundles.

## Funktionen

- Proxmox-Cluster-Erzeugung mit Load Balancern, Control Planes und Workern
- Terraform Plan, Apply, Destroy Plan und Destroy ueber die Weboberflaeche
- Ansible/Helm/Verify erneut ausfuehrbar, ohne Terraform erneut zu starten
- Credentials fuer Proxmox API und SSH-Schluesselverwaltung
- Kubernetes-Web-Konsole fuer sichere `kubectl`-Befehle
- Anwendungsvorlagen wie `nginx-demo`, `whoami` und `rollout-demo`
- Manifest-Revisionen, Diff, Apply, Delete und automatische HTTP-Tests fuer Ingress
- Job-Recovery nach Worker-Neustart und manuelles Aufraeumen alter Jobs/Revisionen

## Voraussetzungen

- Docker und Docker Compose
- Ein erreichbarer Proxmox-Host oder Proxmox-Cluster
- Ein cloud-init-faehiges QEMU-VM-Template auf dem ausgewaehlten Proxmox-Node
- Proxmox API Token mit Rechten zum Erstellen und Loeschen von VMs
- Freie IP-Adressen und VM-IDs fuer Load Balancer, Control Planes und Worker
- Netzwerkzugriff von Builder/Worker zu Proxmox und von den VMs ins Internet

## Proxmox-Template vorbereiten

Das Release enthaelt mit `proxmox/create-template.sh` ein einmaliges
Host-Setupwerkzeug. Es wird direkt als `root` auf genau dem Proxmox-Node
ausgefuehrt, der spaeter im Wizard ausgewaehlt wird. Es laeuft nicht im
Builder-Container und wird weder von der Webanwendung noch von Ansible
automatisch gestartet.

Das Skript laedt ein offizielles Ubuntu-Cloud-Image ueber HTTPS, prueft dessen
SHA-256-Wert, installiert Cloud-Init, SSH und den QEMU Guest Agent und erzeugt
daraus ein QEMU-Template. Die VM-ID besitzt keinen festen Standardwert und muss
im gesamten Proxmox-Cluster frei sein. Vorhandene VMs werden nicht
ueberschrieben.

Das Skript zuerst aus dem geklonten Release auf den Zielnode kopieren:

```bash
scp proxmox/create-template.sh root@pve-node:/root/create-template.sh
ssh root@pve-node
```

Danach auf dem Proxmox-Host ausfuehren. `9100` ist hier nur eine Beispiel-ID:

```bash
bash /root/create-template.sh \
  --vm-id 9100 \
  --storage local-lvm \
  --bridge vmbr0 \
  --ubuntu-release noble \
  --install-dependencies
```

Ohne `--install-dependencies` veraendert das Skript keine Hostpakete und bricht
bei fehlenden Werkzeugen mit einer Erklaerung ab. Alle Optionen zeigt:

```bash
bash /root/create-template.sh --help
```

Nach erfolgreichem Abschluss in der Weboberflaeche **Proxmox-Ressourcen
erkennen** ausfuehren und exakt den Zielnode sowie die ausgegebene Template-VM-ID
auswaehlen. Diese ID darf nicht erneut fuer eine Cluster-VM vergeben werden.

## Setup

1. Repository klonen oder aktualisieren:

```powershell
git pull
```

2. `.env` aus der Vorlage erzeugen und Werte anpassen:

```powershell
Copy-Item .env.example .env
```

Wichtige Werte:

```env
POSTGRES_PASSWORD=...
MASTER_KEY=...
SESSION_SECRET=...
INITIAL_ADMIN_PASSWORD=...
BUILDER_BIND_ADDRESS=127.0.0.1
BUILDER_PORT=8000
```

`MASTER_KEY` muss dauerhaft gleich bleiben, sonst koennen gespeicherte Credentials nicht mehr entschluesselt werden.

3. Stack bauen und starten:

```powershell
docker compose up -d --build
```

4. Logs pruefen:

```powershell
docker compose logs -f web worker
```

5. Weboberflaeche oeffnen:

```text
http://127.0.0.1:8000
```

Login mit Benutzer `admin` und dem Wert aus `INITIAL_ADMIN_PASSWORD`.

## Nutzung

1. Unter **Credentials** ein Proxmox-Credential anlegen.
2. Unter **Credentials** ein SSH-Credential erzeugen oder hinterlegen.
3. Unter **Neuer Cluster** Proxmox, Netzwerk, Kubernetes und Node-Groessen eintragen.
4. Auf der Cluster-Seite zuerst **Terraform planen** ausfuehren.
5. Danach **Geprueften Plan anwenden** starten.
6. Nach erfolgreichem Apply kann mit **Cluster pruefen** validiert werden.
7. Falls nur Ansible, Helm oder Verify wiederholt werden sollen, **Ansible erneut ausfuehren** nutzen.

Ein erfolgreicher Cluster endet mit `READY` und sollte in `kubectl get nodes` alle Nodes als `Ready` zeigen.
Nach einer Konfigurationsaenderung wird ein Cluster wieder zum Entwurf; die alte Kubeconfig wird gesperrt und ein neuer Terraform-Plan mit anschließendem Apply ist erforderlich.

Vor Plan und Apply blockiert der Builder fremde Proxmox-Ressourcen mit
kollidierenden VM-IDs, erzeugten VM-Namen oder statischen IPv4-Adressen aus
`ipconfigN`/`netN`. Das Proxmox-Token benötigt dafür zusätzlich Leserechte
(`VM.Audit`) auf die sichtbaren Gastkonfigurationen. IPs, die nur manuell im
Gastbetriebssystem gesetzt wurden, müssen unter **Einstellungen** reserviert
werden, weil sie nicht aus der Proxmox-Konfiguration erkennbar sind.

Der Builder unterstuetzt derzeit Kubernetes `v1.36`, SSH auf Port `22` und den Kubernetes-API-Port `6443`. Diese Werte sind bewusst festgelegt, weil Cloud-Init und kubeadm abweichende Ports nicht konfigurieren.

## Anwendungen

Unter **Anwendungen** koennen Manifest-Bundles erstellt, bearbeitet und geloescht werden.

Typischer Ablauf:

1. Anwendung aus Template erstellen, zum Beispiel `whoami`.
2. Manifest speichern oder direkt validieren.
3. **Serverseitig validieren** ausfuehren.
4. Optional **Diff anzeigen**.
5. **Bundle anwenden** starten.
6. Im Manifest-Joblog das Ergebnis des automatischen HTTP-Tests gegen die VIP pruefen.

Der Hostname kommt aus dem Ingress der jeweiligen Anwendung. Die Anfrage wird vom Worker mit diesem Host-Header direkt an die API-/Ingress-VIP des Clusters gesendet.

**Aus Cluster entfernen** loescht nur die Kubernetes-Ressourcen. **Eintrag loeschen** entfernt danach den Builder-Eintrag der Anwendung.

## Update

Fuer eine neue Version normalerweise:

```powershell
git pull
docker compose up -d --build
```

Die Datenbankmigrationen laufen beim Start des `web`-Containers automatisch. Bestehende Cluster-Workspaces und Datenbankdaten liegen in Docker-Volumes und bleiben erhalten.

## Loeschen

Empfohlener Cluster-Destroy:

1. Auf der Cluster-Seite den Clusternamen eintragen.
2. **Destroy planen** starten.
3. Danach **Destroy-Plan anwenden** starten.
4. Wenn die Infrastruktur geloescht ist, **Cluster endgueltig entfernen** nutzen.

Falls VMs bereits manuell in Proxmox geloescht wurden, prueft der Builder beim Entfernen des Eintrags die konfigurierten VM-IDs gegen Proxmox. Existiert noch eine passende VM, wird das Loeschen blockiert.

## Wartung

Nuetzliche Befehle:

```powershell
docker compose ps
docker compose logs -f web worker
docker compose restart web worker
docker compose up -d --build
```

Alte abgeschlossene Jobs koennen auf der Cluster-Seite ueber **Job-Historie aufraeumen** entfernt werden. Alte nicht referenzierte Manifest-Revisionen koennen in einer Anwendung ueber **Revisionen aufraeumen** entfernt werden.

## Hinweise

- `latest`-Images in eigenen Manifesten koennen sich veraendern und Deployments weniger reproduzierbar machen.
- Wenn externe Downloads rate-limitiert werden, den Job spaeter erneut starten.
- Wenn DNS- oder APT-Fehler auftreten, zuerst Gateway, DNS und Internetzugriff der Ziel-VMs pruefen.
- Mutierende `kubectl`-Befehle in der Web-Konsole brauchen eine explizite Admin-Bestaetigung.
