# Proxmox Kubernetes Cluster Builder

Webbasierter Builder fuer HA-Kubernetes-Cluster auf Proxmox. Die Anwendung erzeugt aus einem Wizard Terraform- und Ansible-Konfigurationen, erstellt die Proxmox-VMs, installiert Kubernetes, Calico und optional Traefik, und verwaltet einfache Kubernetes-Anwendungen als Manifest-Bundles.

## Funktionen

- Proxmox-Cluster-Erzeugung mit Load Balancern, Control Planes und Workern
- Terraform Plan, Apply, Destroy Plan und Destroy ueber die Weboberflaeche
- Ansible/Helm/Verify erneut ausfuehrbar, ohne Terraform erneut zu starten
- Credentials fuer Proxmox API und SSH-Schluesselverwaltung
- Kubernetes-Web-Konsole fuer sichere `kubectl`-Befehle
- Anwendungsvorlagen wie `nginx-demo`, `whoami` und `rollout-demo`
- Manifest-Revisionen, Diff, Apply, Delete und Curl-Testhinweise fuer Ingress
- Job-Recovery nach Worker-Neustart und manuelles Aufraeumen alter Jobs/Revisionen

## Voraussetzungen

- Docker und Docker Compose
- Ein erreichbarer Proxmox-Host oder Proxmox-Cluster
- Ein cloud-init-faehiges VM-Template in Proxmox
- Proxmox API Token mit Rechten zum Erstellen und Loeschen von VMs
- Freie IP-Adressen und VM-IDs fuer Load Balancer, Control Planes und Worker
- Netzwerkzugriff von Builder/Worker zu Proxmox und von den VMs ins Internet

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

## Anwendungen

Unter **Anwendungen** koennen Manifest-Bundles erstellt, bearbeitet und geloescht werden.

Typischer Ablauf:

1. Anwendung aus Template erstellen, zum Beispiel `whoami`.
2. Manifest speichern oder direkt validieren.
3. **Serverseitig validieren** ausfuehren.
4. Optional **Diff anzeigen**.
5. **Bundle anwenden** starten.
6. Im Manifest-Joblog den erzeugten Curl-Befehl gegen die VIP ausfuehren.

Beispiel:

```powershell
curl -v -H "Host: whoami.example.local" http://10.200.50.150/
```

Der Hostname kommt aus dem Ingress der jeweiligen Anwendung. Die IP ist die API-/Ingress-VIP des Clusters.

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
