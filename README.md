# Projektdokumentation: HA-Kubernetes-Cluster auf Proxmox mit Ingress

## 1. Ziel des Projekts

Dieses Projekt beschreibt den Aufbau eines wiederverwendbaren Kubernetes-Labors auf Proxmox. Das Ziel ist ein hochverfügbares Kubernetes-Cluster, das jederzeit per Terraform und Ansible neu erstellt werden kann. Das Cluster soll als saubere Lern- und Testumgebung dienen, in der Anwendungen über Kubernetes-YAMLs deployed und später über DNS/Ingress erreichbar gemacht werden können.

Das Setup ist bewusst in drei Ebenen getrennt:

1. **Infrastruktur**: Proxmox-VMs, IP-Adressen, Netzwerk, Terraform.
2. **Kubernetes-Basiscluster**: HA-Control-Plane, Worker, Calico, kubeconfig.
3. **Optionale Addons**: Traefik Ingress Controller, HAProxy-Weiterleitung für HTTP/HTTPS, Demo-Anwendungen.

Die Trennung ist wichtig, damit `make cluster` ein generisches Kubernetes-Cluster erzeugen kann, während `make ingress` den optionalen Ingress-Layer ergänzt. So bleibt das Grundsystem sauber und testbar, statt direkt in ein App-spezifisches Konfigurationsmonster zu mutieren. Kubernetes braucht schließlich keine zusätzliche Hilfe, um kompliziert zu wirken.

---

## 2. Zielarchitektur

### 2.1 Gesamtübersicht

```text
                             Proxmox Host
                            10.200.50.134
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
        ▼                         ▼                         ▼
   Load Balancer              Control Plane              Worker Nodes

   lb-01                      control-01                 worker-01
   10.200.50.145              10.200.50.151              10.200.50.161

   lb-02                      control-02                 worker-02
   10.200.50.146              10.200.50.152              10.200.50.162

                              control-03
                              10.200.50.153
```

Zusätzlich existiert eine virtuelle IP-Adresse, die durch Keepalived zwischen den beiden Load-Balancern verwaltet wird:

```text
VIP: 10.200.50.150
```

Diese VIP ist der zentrale Einstiegspunkt für:

- Kubernetes-API auf Port `6443`
- HTTP-Anwendungen auf Port `80`
- HTTPS-Anwendungen auf Port `443`

---

## 3. IP- und Rollenübersicht

| Rolle | Hostname | IP-Adresse | Aufgabe |
|---|---:|---:|---|
| Proxmox Host | proxmox-management-ag | 10.200.50.134 | Virtualisierungshost |
| Load Balancer 1 | lb-01 | 10.200.50.145 | HAProxy + Keepalived |
| Load Balancer 2 | lb-02 | 10.200.50.146 | HAProxy + Keepalived |
| Virtuelle IP | VIP | 10.200.50.150 | zentraler Zugriffspunkt |
| Control Plane 1 | control-01 | 10.200.50.151 | Kubernetes API, etcd, Scheduler, Controller |
| Control Plane 2 | control-02 | 10.200.50.152 | Kubernetes API, etcd, Scheduler, Controller |
| Control Plane 3 | control-03 | 10.200.50.153 | Kubernetes API, etcd, Scheduler, Controller |
| Worker 1 | worker-01 | 10.200.50.161 | Anwendungspods |
| Worker 2 | worker-02 | 10.200.50.162 | Anwendungspods |

---

## 4. Netzwerkbereiche

Im Projekt gibt es mehrere Netzwerkbereiche, die unterschiedliche Aufgaben haben.

### 4.1 VM-/LAN-Netz

```text
10.200.50.0/24
```

Dieses Netz enthält die echten IP-Adressen der Proxmox-VMs. Diese IPs sind von außen erreichbar, sofern das Netzwerk entsprechend verbunden ist.

Beispiele:

```text
10.200.50.150  VIP
10.200.50.151  control-01
10.200.50.161  worker-01
```

### 4.2 Kubernetes Pod-Netz

```text
192.168.0.0/16
```

Dieses Netz wird von Calico für Pod-IPs verwendet. Pods erhalten daraus interne IP-Adressen, zum Beispiel:

```text
192.168.37.196
192.168.171.3
```

Diese IPs sind nicht als stabile externe Zugriffspunkte gedacht. Pods sind vergänglich. Wenn ein Pod neu erstellt wird, kann er eine neue IP bekommen. Kubernetes behandelt Pods nicht wie Haustiere, sondern wie Wegwerfobjekte mit erstaunlich wenig Sentimentalität.

### 4.3 Kubernetes Service-Netz

```text
10.96.0.0/12
```

Dieses Netz wird für interne Kubernetes-Services genutzt. Services erhalten daraus ClusterIP-Adressen, die innerhalb des Clusters verwendet werden.

Beispiel:

```text
nginx-demo-service → 10.96.x.x
```

---

## 5. Grundidee der Zugriffspfade

Es gibt zwei grundlegend verschiedene Zugriffspfade:

1. **Cluster-Verwaltung** über die Kubernetes-API.
2. **Anwendungszugriff** über DNS, HAProxy, Traefik und Kubernetes Ingress.

Diese Trennung ist zentral.

---

## 6. Verwaltungszugriff auf das Cluster

Die Verwaltung erfolgt über `kubectl`, `helm` oder andere Kubernetes-Clients. Diese sprechen nicht direkt mit einzelnen Worker-Nodes, sondern mit der Kubernetes-API.

Die Kubernetes-API ist über die VIP erreichbar:

```text
https://10.200.50.150:6443
```

Diese Adresse steht in der `kubeconfig`:

```yaml
server: https://10.200.50.150:6443
```

### 6.1 Ablauf bei einem kubectl-Befehl

```text
kubectl auf WSL
        │
        ▼
liest kubeconfig
        │
        ▼
https://10.200.50.150:6443
        │
        ▼
HAProxy auf lb-01/lb-02
        │
        ▼
control-01/control-02/control-03:6443
        │
        ▼
Kubernetes API
```

### 6.2 Beispielbefehle

```bash
cd ~/IaC-Kubernetes
export KUBECONFIG=$PWD/kubeconfig

kubectl get nodes -o wide
kubectl get pods -A
kubectl cluster-info
```

Diese Befehle sprechen die Kubernetes-API über die VIP an:

```text
10.200.50.150:6443
```

Sie sprechen nicht direkt die Worker an.

---

## 7. Anwendungszugriff über Ingress

Für Anwendungen wird später nicht die Kubernetes-API verwendet. Anwendungen werden über HTTP oder HTTPS angesprochen.

Beispiel:

```text
http://nginx.lab.local
```

Dieser DNS-Name zeigt auf die VIP:

```text
nginx.lab.local → 10.200.50.150
```

### 7.1 Ablauf bei einem Webseitenaufruf

```text
Browser / curl
        │
        ▼
http://nginx.lab.local
        │
        ▼
DNS / hosts-Datei
        │
        ▼
10.200.50.150
        │
        ▼
HAProxy auf lb-01/lb-02
        │
        ▼
worker-01:30080 oder worker-02:30080
        │
        ▼
Traefik Ingress Controller
        │
        ▼
Ingress-Regel für nginx.lab.local
        │
        ▼
Kubernetes Service vom Typ ClusterIP
        │
        ▼
Pods der Anwendung
```

### 7.2 Wichtigster Unterschied

Für Verwaltung:

```text
10.200.50.150:6443
```

Für Anwendungen:

```text
10.200.50.150:80
10.200.50.150:443
```

oder per DNS:

```text
http://nginx.lab.local
https://nginx.lab.local
```

Die Worker werden im Zielbetrieb nicht direkt angesprochen. Sie sind interne Ziele hinter HAProxy und Traefik.

---

## 8. Rolle von HAProxy und Keepalived

### 8.1 Keepalived

Keepalived verwaltet die virtuelle IP:

```text
10.200.50.150
```

Diese VIP liegt immer auf genau einem der beiden Load-Balancer. Wenn der aktive Load-Balancer ausfällt, übernimmt der zweite Load-Balancer die VIP.

Dadurch bleibt der Zugriffspunkt gleich, auch wenn ein Load-Balancer nicht mehr verfügbar ist.

### 8.2 HAProxy

HAProxy nimmt Verbindungen auf der VIP entgegen und leitet sie weiter.

Für das Basiscluster gilt:

```text
10.200.50.150:6443 → control-01/02/03:6443
```

Für den Ingress-Layer gilt zusätzlich:

```text
10.200.50.150:80  → worker-01/02:30080
10.200.50.150:443 → worker-01/02:30443
```

HAProxy kennt dabei keine konkreten Anwendungen. Er kennt nur die generischen Traefik-NodePorts. Das ist gewollt.

---

## 9. Rolle von Traefik

Traefik läuft als Ingress Controller im Kubernetes-Cluster.

Er ist über einen Kubernetes-Service vom Typ `NodePort` erreichbar:

```text
HTTP  → NodePort 30080
HTTPS → NodePort 30443
```

HAProxy leitet externe Anfragen an diese NodePorts weiter.

Traefik liest Kubernetes-Ingress-Objekte und entscheidet anhand von Hostnamen und Pfaden, welcher Service angesprochen werden soll.

Beispiel:

```yaml
rules:
  - host: nginx.lab.local
    http:
      paths:
        - path: /
          pathType: Prefix
          backend:
            service:
              name: nginx-demo-service
              port:
                number: 80
```

Damit gilt:

```text
nginx.lab.local → nginx-demo-service → nginx Pods
```

---

## 10. Warum nicht direkt Worker ansprechen?

Direkt möglich wäre zum Testen:

```text
http://10.200.50.161:30080
http://10.200.50.162:30080
```

Das ist für Debugging nützlich, aber nicht das Ziel.

Der saubere Zielzugriff ist:

```text
http://nginx.lab.local
```

Vorteile:

- zentrale VIP statt einzelner Worker-IP
- DNS-basierter Zugriff
- keine sichtbaren NodePorts in der URL
- mehrere Anwendungen über Hostnamen möglich
- HAProxy muss nicht für jede Anwendung angepasst werden
- Routing-Regeln liegen in Kubernetes-Ingress-YAMLs

---

## 11. Zugriffspfade im Vergleich

### 11.1 Ohne Ingress, nur NodePort

```text
Browser
  │
  ▼
worker-01:30080 oder worker-02:30080
  │
  ▼
Kubernetes NodePort Service
  │
  ▼
Pods
```

Diese Variante eignet sich gut zum Lernen von Services und NodePorts.

### 11.2 Mit Ingress

```text
Browser
  │
  ▼
nginx.lab.local
  │
  ▼
10.200.50.150
  │
  ▼
HAProxy
  │
  ▼
Traefik
  │
  ▼
Ingress-Regel
  │
  ▼
ClusterIP Service
  │
  ▼
Pods
```

Diese Variante ist sauberer für mehrere HTTP-Anwendungen.

---

## 12. Empfohlene Projektstruktur

```text
~/IaC-Kubernetes/
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   └── outputs.tf
│
├── ansible/
│   ├── inventory.ini
│   ├── group_vars/
│   │   └── all.yml
│   └── playbooks/
│       ├── 01-base.yml
│       ├── 02-loadbalancer.yml
│       ├── 03-kubernetes-prereqs.yml
│       ├── 04-init-control-plane.yml
│       ├── 05-join-control-planes.yml
│       ├── 06-join-workers.yml
│       ├── 07-calico.yml
│       ├── 09-fetch-kubeconfig.yml
│       └── addons/
│           └── 10-traefik.yml
│
├── kube/
│   ├── addons/
│   │   └── traefik/
│   │       └── values.yaml
│   │
│   └── demo/
│       └── nginx-ingress/
│           ├── namespace.yaml
│           ├── deployment.yaml
│           ├── service.yaml
│           └── ingress.yaml
│
├── scripts/
│   ├── check-cluster.sh
│   └── install-local-tools.sh
│
├── kubeconfig
└── Makefile
```

---

## 13. Terraform-Ebene

Terraform ist für die Erstellung der VMs auf Proxmox zuständig.

Aufgaben:

- VMs aus Template klonen
- VM-Namen setzen
- CPU/RAM/Disk konfigurieren
- statische IPs über Cloud-Init setzen
- VMs starten

Die Kubernetes-Installation selbst übernimmt nicht Terraform, sondern Ansible.

Empfohlene Trennung:

```text
Terraform → Infrastruktur erstellen
Ansible   → Systeme konfigurieren und Kubernetes installieren
kubectl   → Anwendungen deployen
Helm      → Addons wie Traefik installieren
```

---

## 14. Ansible-Ebene

Ansible übernimmt die Konfiguration der VMs.

Typische Aufgaben:

- Basispakete installieren
- HAProxy und Keepalived auf den Load-Balancern konfigurieren
- containerd installieren
- Kubernetes-Pakete installieren
- erstes Control-Plane-Node initialisieren
- weitere Control-Planes joinen
- Worker joinen
- Calico installieren
- kubeconfig vom Cluster holen

---

## 15. Zentrale Ansible-Variablen

Beispiel für `ansible/group_vars/all.yml`:

```yaml
kubernetes_minor: "v1.36"

api_vip: "10.200.50.150"
api_port: 6443

pod_cidr: "192.168.0.0/16"
service_cidr: "10.96.0.0/12"

control_plane_endpoint: "10.200.50.150:6443"

first_control_plane: "control-01"

kube_user: "ubuntu"

keepalived_interface: "eth0"

haproxy_backends:
  - name: control-01
    ip: 10.200.50.151
  - name: control-02
    ip: 10.200.50.152
  - name: control-03
    ip: 10.200.50.153
```

Wichtig ist insbesondere:

```yaml
keepalived_interface: "eth0"
```

Bei diesem Setup nutzt die VM das Interface `eth0`. Eine falsche Interface-Angabe wie `ens18` führt dazu, dass Keepalived die VIP nicht korrekt setzen kann. Natürlich nennt Linux Netzwerkschnittstellen nicht einfach einheitlich, weil das offenbar gegen irgendeine kosmische Bürokratie verstößt.

---

## 16. Generische HAProxy-Konfiguration

Die HAProxy-Konfiguration sollte für das Basiscluster nur die Kubernetes-API routen. Für den Ingress-Layer wird sie generisch um Port 80 und 443 erweitert.

Wichtig: Sie darf keine App-spezifischen Hostnamen wie `nginx.lab.local` enthalten.

### 16.1 HAProxy für Kubernetes API und Ingress

```haproxy
global
    log /dev/log local0
    log /dev/log local1 notice
    daemon
    maxconn 2048

defaults
    log     global
    mode    tcp
    option  tcplog
    option  dontlognull
    timeout connect 5s
    timeout client  50s
    timeout server  50s

frontend kubernetes_api
    bind *:6443
    mode tcp
    default_backend kubernetes_control_plane

backend kubernetes_control_plane
    mode tcp
    balance roundrobin
    option tcp-check
    server control-01 10.200.50.151:6443 check
    server control-02 10.200.50.152:6443 check
    server control-03 10.200.50.153:6443 check

frontend http_ingress
    bind *:80
    mode tcp
    default_backend traefik_http

backend traefik_http
    mode tcp
    balance roundrobin
    option tcp-check
    server worker-01 10.200.50.161:30080 check
    server worker-02 10.200.50.162:30080 check

frontend https_ingress
    bind *:443
    mode tcp
    default_backend traefik_https

backend traefik_https
    mode tcp
    balance roundrobin
    option tcp-check
    server worker-01 10.200.50.161:30443 check
    server worker-02 10.200.50.162:30443 check
```

Diese Konfiguration bedeutet:

```text
6443 → Kubernetes API
80   → Traefik HTTP
443  → Traefik HTTPS
```

---

## 17. Traefik Values-Datei

Datei:

```text
kube/addons/traefik/values.yaml
```

Inhalt:

```yaml
deployment:
  replicas: 2

ingressClass:
  enabled: true
  isDefaultClass: true
  name: traefik

providers:
  kubernetesIngress:
    enabled: true

service:
  type: NodePort
  spec:
    externalTrafficPolicy: Cluster

ports:
  web:
    port: 80
    expose:
      default: true
    exposedPort: 80
    nodePort: 30080
    protocol: TCP

  websecure:
    port: 443
    expose:
      default: true
    exposedPort: 443
    nodePort: 30443
    protocol: TCP
```

Damit wird Traefik so installiert, dass HAProxy ihn über die Worker-NodePorts erreichen kann.

---

## 18. Demo-Anwendung mit Ingress

Die Demo-Anwendung besteht aus:

```text
Namespace
Deployment
Service ClusterIP
Ingress
```

### 18.1 Namespace

Datei:

```text
kube/demo/nginx-ingress/namespace.yaml
```

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: demo
```

### 18.2 Deployment

Datei:

```text
kube/demo/nginx-ingress/deployment.yaml
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-demo
  namespace: demo
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
```

### 18.3 Service

Datei:

```text
kube/demo/nginx-ingress/service.yaml
```

```yaml
apiVersion: v1
kind: Service
metadata:
  name: nginx-demo-service
  namespace: demo
spec:
  type: ClusterIP
  selector:
    app: nginx-demo
  ports:
    - name: http
      port: 80
      targetPort: 80
```

Der Service ist bewusst vom Typ `ClusterIP`, weil der externe Zugriff über Traefik erfolgt.

### 18.4 Ingress

Datei:

```text
kube/demo/nginx-ingress/ingress.yaml
```

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: nginx-demo-ingress
  namespace: demo
spec:
  ingressClassName: traefik
  rules:
    - host: nginx.lab.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: nginx-demo-service
                port:
                  number: 80
```

---

## 19. Makefile-Struktur

Das Makefile sollte die Aufgaben klar trennen.

### 19.1 Zielstruktur

```makefile
cluster: infra wait ping k8s check

lab: cluster ingress

ingress:
	helm repo add traefik https://traefik.github.io/charts || true
	helm repo update
	KUBECONFIG=$(PWD)/kubeconfig helm upgrade --install traefik traefik/traefik \
		--namespace traefik \
		--create-namespace \
		-f kube/addons/traefik/values.yaml
	cd ansible && ansible-playbook -i inventory.ini playbooks/02-loadbalancer.yml

demo-namespace:
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/namespace.yaml
	KUBECONFIG=$(PWD)/kubeconfig kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/demo --timeout=30s

demo-ingress: demo-namespace
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/service.yaml
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/deployment.yaml
	KUBECONFIG=$(PWD)/kubeconfig kubectl apply -f kube/demo/nginx-ingress/ingress.yaml

delete-demo-ingress:
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/ingress.yaml --ignore-not-found
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/deployment.yaml --ignore-not-found
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/service.yaml --ignore-not-found
	KUBECONFIG=$(PWD)/kubeconfig kubectl delete -f kube/demo/nginx-ingress/namespace.yaml --ignore-not-found
```

### 19.2 Bedeutung der Targets

| Target | Bedeutung |
|---|---|
| `make cluster` | baut das leere HA-Kubernetes-Basiscluster |
| `make ingress` | installiert Traefik und konfiguriert HAProxy für 80/443 |
| `make lab` | baut Cluster plus Ingress-Layer |
| `make demo-ingress` | deployed die Beispiel-App mit Ingress |
| `make delete-demo-ingress` | entfernt die Beispiel-App wieder |

---

## 20. Warum Namespace separat angewendet wird

Ein Fehler kann auftreten, wenn alle YAML-Dateien gleichzeitig über einen Ordner angewendet werden:

```bash
kubectl apply -f kube/demo/nginx-ingress/
```

Dann kann Kubernetes zuerst den Namespace erstellen, aber die anderen Ressourcen noch ablehnen, weil der Namespace intern noch nicht vollständig verfügbar ist.

Typischer Fehler:

```text
Error from server (NotFound): namespaces "demo" not found
```

Deshalb ist das Makefile so aufgebaut:

```text
1. Namespace anwenden
2. warten, bis Namespace Active ist
3. Service, Deployment und Ingress anwenden
```

Das ist weniger hübsch, aber zuverlässig. Und zuverlässig schlägt hübsch, besonders wenn YAML beteiligt ist.

---

## 21. Helm und Traefik im Setup

### 21.1 Helm

Helm ist ein lokales Werkzeug auf dem WSL-/Admin-System.

Es wird nicht im Cluster installiert.

```text
Helm = lokales Installationswerkzeug
Traefik = Anwendung im Kubernetes-Cluster
```

Wenn die VMs mit `make destroy` gelöscht werden, bleibt Helm auf WSL erhalten.

### 21.2 Traefik

Traefik läuft im Kubernetes-Cluster. Wenn das Cluster gelöscht wird, ist Traefik ebenfalls weg.

Deshalb muss nach einem frischen Cluster erneut ausgeführt werden:

```bash
make ingress
```

Oder direkt:

```bash
make lab
```

---

## 22. Optionales Tool-Script für Helm

Datei:

```text
scripts/install-local-tools.sh
```

```bash
#!/usr/bin/env bash
set -euo pipefail

if command -v helm >/dev/null 2>&1; then
  echo "Helm ist bereits installiert:"
  helm version
  exit 0
fi

echo "Installiere Helm über offizielles Helm-Script..."

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

curl -fsSL -o "$tmpdir/get_helm.sh" https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
chmod 700 "$tmpdir/get_helm.sh"
"$tmpdir/get_helm.sh"

helm version
```


Makefile-Target:

```makefile
tools:
	./scripts/install-local-tools.sh
```

---

## 23. Ablauf für einen frischen Neuaufbau

### 23.1 Nur Basiscluster

```bash
cd ~/IaC-Kubernetes
make destroy
make cluster
```

Danach prüfen:

```bash
export KUBECONFIG=$PWD/kubeconfig
kubectl get nodes -o wide
kubectl get pods -A
curl -k https://10.200.50.150:6443/readyz
```

### 23.2 Standard-Lab mit Ingress

```bash
cd ~/IaC-Kubernetes
make destroy
make lab
make demo-ingress
```

Danach testen:

```bash
curl -H "Host: nginx.lab.local" http://10.200.50.150
```

Mit DNS-/hosts-Eintrag:

```text
10.200.50.150 nginx.lab.local
```

kann getestet werden:

```bash
curl http://nginx.lab.local
```

---

## 24. DNS und hosts-Dateien

Für DNS-basierte Tests muss ein Hostname auf die VIP zeigen.


Statt hosts-Dateien kann später ein interner DNS-Server genutzt werden, zum Beispiel:

```text
nginx.lab.local   → 10.200.50.150
app1.lab.local    → 10.200.50.150
grafana.lab.local → 10.200.50.150
```

Alle Namen zeigen auf dieselbe VIP. Traefik entscheidet anhand des Host-Headers, zu welchem Service weitergeleitet wird.

---

## 25. Debugging

### 25.1 Kubernetes API prüfen

```bash
curl -k https://10.200.50.150:6443/readyz
```

Erwartung:

```text
ok
```

### 25.2 kubeconfig prüfen

```bash
grep server ~/IaC-Kubernetes/kubeconfig
```

Erwartung:

```yaml
server: https://10.200.50.150:6443
```

### 25.3 Nodes prüfen

```bash
kubectl get nodes -o wide
```

### 25.4 System-Pods prüfen

```bash
kubectl get pods -A
```

### 25.5 Traefik prüfen

```bash
kubectl get pods -n traefik -o wide
kubectl get svc -n traefik
kubectl get ingressclass
```

Erwartung:

```text
Traefik Pods Running
Traefik Service NodePort mit 80:30080 und 443:30443
IngressClass traefik vorhanden
```

### 25.6 HAProxy prüfen

```bash
ssh ubuntu@10.200.50.145 "sudo haproxy -c -f /etc/haproxy/haproxy.cfg && sudo ss -lntp | grep -E ':80|:443|:6443'"
ssh ubuntu@10.200.50.146 "sudo haproxy -c -f /etc/haproxy/haproxy.cfg && sudo ss -lntp | grep -E ':80|:443|:6443'"
```

### 25.7 VIP prüfen

```bash
ssh ubuntu@10.200.50.145 "ip a | grep 10.200.50.150 || true"
ssh ubuntu@10.200.50.146 "ip a | grep 10.200.50.150 || true"
```

Die VIP sollte auf genau einem der beiden Load-Balancer liegen.

### 25.8 Anwendung über VIP testen

```bash
curl -H "Host: nginx.lab.local" http://10.200.50.150
```

### 25.9 Traefik direkt über Worker testen

```bash
curl -H "Host: nginx.lab.local" http://10.200.50.161:30080
curl -H "Host: nginx.lab.local" http://10.200.50.162:30080
```

Wenn dieser Test funktioniert, aber die VIP nicht, liegt das Problem wahrscheinlich bei HAProxy oder Keepalived.

### 25.10 Ingress prüfen

```bash
kubectl describe ingress nginx-demo-ingress -n demo
```

### 25.11 Service-Endpunkte prüfen

```bash
kubectl get endpoints -n demo
kubectl get endpointslice -n demo
```

Wenn keine Endpoints existieren, passt meistens der Service-Selector nicht zu den Pod-Labels.

---

## 26. Häufige Fehler und Lösungen

### 26.1 SSH Host Key hat sich geändert

Nach `make destroy` und `make cluster` behalten die VMs oft dieselben IPs, haben aber neue SSH Host Keys.

Fehler:

```text
WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!
Host key verification failed.
```

Lösung:

```bash
for ip in 10.200.50.145 10.200.50.146 10.200.50.151 10.200.50.152 10.200.50.153 10.200.50.161 10.200.50.162; do
  ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$ip"
done
```

Optional neue Keys einsammeln:

```bash
for ip in 10.200.50.145 10.200.50.146 10.200.50.151 10.200.50.152 10.200.50.153 10.200.50.161 10.200.50.162; do
  ssh-keyscan -H "$ip" >> "$HOME/.ssh/known_hosts"
done
```

### 26.2 SSH-Agent / Publickey-Fehler

Fehler:

```text
Permission denied (publickey)
```

Lösung:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
ssh-add -l
```

### 26.3 kubectl versucht localhost:8080

Fehler:

```text
The connection to the server localhost:8080 was refused
```

Ursache: `kubectl` findet keine gültige kubeconfig.

Lösung:

```bash
echo 'export KUBECONFIG="$HOME/IaC-Kubernetes/kubeconfig"' >> ~/.bashrc
source ~/.bashrc
```


---



## 27. Empfohlener finaler Workflow

### Einmalig lokale Tools vorbereiten

```bash
cd ~/IaC-Kubernetes
make tools
```

### Komplettes Lab neu bauen

```bash
make destroy
make lab
make demo-ingress
```

### Testen

```bash
curl -H "Host: nginx.lab.local" http://10.200.50.150
```

Oder nach hosts-/DNS-Eintrag:

```bash
curl http://nginx.lab.local
```

### Demo-App entfernen

```bash
make delete-demo-ingress
```

### Nur Basiscluster bauen

```bash
make destroy
make cluster
```

---

## 29. Zusammenfassung

Das Projekt stellt ein wiederverwendbares HA-Kubernetes-Lab auf Proxmox bereit. Das Basiscluster wird durch Terraform und Ansible erzeugt. Der Zugriff auf die Kubernetes-API erfolgt hochverfügbar über die VIP `10.200.50.150` auf Port `6443`.

Für Anwendungen wird ein optionaler Ingress-Layer genutzt. Dabei leitet HAProxy generisch Port `80` und `443` an den Traefik Ingress Controller weiter. Traefik übernimmt das app-spezifische Routing anhand von Kubernetes-Ingress-Regeln.

Dadurch ist das Setup sauber getrennt:

```text
make cluster → leeres HA-Kubernetes-Cluster
make ingress → generischer Ingress-Layer
make lab     → Cluster + Ingress
kubectl apply / make demo-ingress → Anwendungen
```

Das Ergebnis ist ein nachvollziehbares, wiederholbares und erweiterbares Kubernetes-Lab, in dem Anwendungen über DNS-Namen wie `nginx.lab.local` angesprochen werden können, ohne direkt Worker-IPs oder NodePorts im Browser zu verwenden.

Ein erstaunlich vernünftiges Ende für etwas, das mit Proxmox, HAProxy, Kubernetes und YAML angefangen hat. Statistisch hätte es schlimmer kommen müssen.
