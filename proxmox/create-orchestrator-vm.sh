#!/usr/bin/env bash
#
# Erstellt auf einem Proxmox-VE-Node eine eigenstaendige Ubuntu-VM fuer
# K8s Universal. Docker Engine, das Docker-Compose-v2-Plugin sowie die
# Produktionsdateien compose.yaml und .env.example werden vor dem ersten
# Start in das Image installiert.
#
# Das Skript erzeugt absichtlich keine .env und startet die Anwendung nicht:
# Echte Secrets, Bind-Adresse und Image-Version muessen vom Administrator in
# der neuen VM festgelegt werden. Danach genuegen `docker compose up -d` und
# die von Compose verwendeten vorgebauten Container-Images.
#
# SICHERHEITSMODELL
# -----------------
# - Direkte Ausfuehrung als root auf dem Ziel-Proxmox-Node.
# - Keine feste VM-ID; die Ziel-ID wird clusterweit zweimal geprueft.
# - Vorhandene VMs, Templates oder Container werden nie ueberschrieben.
# - Eine bei einem Fehler teilweise erzeugte VM wird nicht automatisch
#   geloescht und kann vom Administrator zuerst untersucht werden.
# - Ubuntu-Downloads laufen nur ueber HTTPS und werden gegen die von Ubuntu
#   veroeffentlichte SHA-256-Liste geprueft.
# - Docker wird ueber das offizielle Docker-APT-Repository installiert;
#   fremde Convenience-Skripte werden nicht in eine Shell gepiped.
# - Der Login erfolgt als ubuntu ausschliesslich mit einem SSH Public Key.
#
# SCHNELLSTART NACH DEM DOWNLOAD
# -----------------------------
# bash create-orchestrator-vm.sh --vm-id 9300 \
#   --ssh-key-file /root/.ssh/id_ed25519.pub --install-dependencies
#
# Fuer einen nicht-interaktiven, vorher geprueften Lauf:
# bash create-orchestrator-vm.sh 9300 \
#   --ssh-key-file /root/.ssh/id_ed25519.pub --install-dependencies --yes

set -Eeuo pipefail
IFS=$'\n\t'
umask 077
export LC_ALL=C

VM_ID="${ORCHESTRATOR_VM_ID:-}"
STORAGE="local-lvm"
BRIDGE="vmbr0"
VM_NAME=""
UBUNTU_RELEASE="noble"
IMAGE_URL=""
IMAGE_SHA256=""
DISK_SIZE_GB=40
MEMORY_MB=8192
CPU_CORES=4
REPOSITORY_REF="main"
SSH_KEY_FILE="${ORCHESTRATOR_SSH_KEY_FILE:-}"
IP_CONFIG="ip=dhcp"
READINESS_TIMEOUT_SECONDS=600
INSTALL_DEPENDENCIES=false
KEEP_WORKDIR=false
ASSUME_YES=false

readonly CLOUD_USER="ubuntu"
readonly PROJECT_DIR="/home/ubuntu/k8s-universal"
readonly REPOSITORY_BASE_URL="https://raw.githubusercontent.com/MidniqhtVibes/K8S-Universal"
readonly REPOSITORY_API_URL="https://api.github.com/repos/MidniqhtVibes/K8S-Universal"
readonly UBUNTU_CLOUD_IMAGE_KEY_FINGERPRINT="D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81"
readonly AGENT_CONFIG_PATTERN='^agent: (1|enabled=1)(,|$)'

WORK_DIR=""
VM_CREATED=false
FINISHED=false

usage() {
  cat <<'EOF'
Erstellt und startet eine Ubuntu-Orchestrator-VM fuer K8s Universal.

Aufruf:
  bash proxmox/create-orchestrator-vm.sh --vm-id ID [Optionen]
  bash proxmox/create-orchestrator-vm.sh ID [Optionen]

Pflichtparameter:
  --vm-id ID                 Clusterweit freie Proxmox-VM-ID (100..999999999).
                              Die ID darf alternativ positional oder ueber
                              ORCHESTRATOR_VM_ID uebergeben werden.
  --ssh-key-file PFAD        Explizite Public-Key-Datei fuer ubuntu. Alternativ
                              kann ORCHESTRATOR_SSH_KEY_FILE gesetzt werden.

Optionen:
  --storage NAME             Storage fuer System- und Cloud-Init-Disk
                              (Standard: local-lvm).
  --bridge NAME              Lokale Netzwerk-Bridge (Standard: vmbr0).
  --name NAME                VM-Name (Standard: k8s-universal-ID).
  --ubuntu-release RELEASE   noble (24.04) oder jammy (22.04), Standard noble.
  --image-url URL            Abweichendes Ubuntu-Cloud-Image, nur HTTPS.
  --image-sha256 HASH        Erwarteter SHA-256-Hash fuer --image-url. Bei einer
                              abweichenden URL verpflichtend. Das offizielle
                              Ubuntu-Image wird sonst per SHA256SUMS.gpg geprueft.
  --disk-size GB             Systemdisk in GiB (Standard: 40, Minimum: 20).
  --memory MB                Arbeitsspeicher (Standard: 8192, Minimum: 4096).
  --cores ANZAHL             vCPU-Anzahl (Standard: 4, Minimum: 2).
  --repository-ref REF       Git-Branch, Tag oder Commit fuer compose.yaml und
                              .env.example (Standard: main). Ein gepinnter Tag
                              oder Commit ist fuer Produktion reproduzierbarer.
  --ip-config CONFIG         Proxmox-Cloud-Init-Netzwerk fuer ipconfig0
                              (Standard: ip=dhcp).
  --readiness-timeout SEK    Maximale Wartezeit auf QEMU-Agent, Cloud-Init und
                              Docker (Standard: 600, Bereich: 60..3600).
  --install-dependencies     Fehlende Hostpakete explizit installieren.
  --keep-workdir             Download-/Arbeitsdateien nach dem Lauf behalten.
  --yes                      Bestaetigungsabfrage ueberspringen; alle
                              Schutzpruefungen bleiben aktiv.
  -h, --help                 Diese Hilfe anzeigen.

Nach erfolgreichem Start liegen in /home/ubuntu/k8s-universal nur
compose.yaml und .env.example. In der VM sind anschliessend diese Schritte
erforderlich:

  cd /home/ubuntu/k8s-universal
  cp .env.example .env
  chmod 600 .env
  # Secrets, Image-Version und BUILDER_BIND_ADDRESS in .env setzen
  docker compose up -d

Das Skript muss als root direkt auf einem Proxmox-VE-Node laufen.
EOF
}

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARNUNG] %s\n' "$*" >&2
}

die() {
  printf '[FEHLER] %s\n' "$*" >&2
  exit 1
}

require_command() {
  local command_name="$1"
  command -v "$command_name" >/dev/null 2>&1 \
    || die "Benoetigter Befehl fehlt: ${command_name}"
}

cleanup() {
  local exit_code=$?

  if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
    if [[ "$KEEP_WORKDIR" == true ]]; then
      warn "Arbeitsverzeichnis bleibt erhalten: ${WORK_DIR}"
    else
      case "$WORK_DIR" in
        "/var/tmp/k8s-proxmox-orchestrator.${VM_ID}."*) rm -rf -- "$WORK_DIR" ;;
        *) warn "Unerwarteter Temp-Pfad wird nicht entfernt: ${WORK_DIR}" ;;
      esac
    fi
  fi

  if (( exit_code != 0 )) && [[ "$VM_CREATED" == true && "$FINISHED" != true ]]; then
    warn "VM ${VM_ID} wurde angelegt, aber nicht fertiggestellt."
    warn "Bitte mit 'qm config ${VM_ID}' pruefen und erst danach bewusst bereinigen."
  fi
}
trap cleanup EXIT

# Neben --vm-id wird eine positionale ID unterstuetzt, damit der wget-
# Schnellstart kurz bleibt. Alle weiteren Argumente bleiben benannte Optionen.
if (( $# > 0 )) && [[ "$1" != -* ]]; then
  [[ -z "$VM_ID" ]] || die "VM-ID wurde mehrfach angegeben"
  VM_ID="$1"
  shift
fi

while (( $# > 0 )); do
  case "$1" in
    --vm-id)
      (( $# >= 2 )) || die "--vm-id benoetigt einen Wert"
      [[ -z "$VM_ID" ]] || die "VM-ID wurde mehrfach angegeben"
      VM_ID="$2"
      shift 2
      ;;
    --storage)
      (( $# >= 2 )) || die "--storage benoetigt einen Wert"
      STORAGE="$2"
      shift 2
      ;;
    --bridge)
      (( $# >= 2 )) || die "--bridge benoetigt einen Wert"
      BRIDGE="$2"
      shift 2
      ;;
    --name)
      (( $# >= 2 )) || die "--name benoetigt einen Wert"
      VM_NAME="$2"
      shift 2
      ;;
    --ubuntu-release)
      (( $# >= 2 )) || die "--ubuntu-release benoetigt einen Wert"
      UBUNTU_RELEASE="$2"
      shift 2
      ;;
    --image-url)
      (( $# >= 2 )) || die "--image-url benoetigt einen Wert"
      IMAGE_URL="$2"
      shift 2
      ;;
    --image-sha256)
      (( $# >= 2 )) || die "--image-sha256 benoetigt einen Wert"
      IMAGE_SHA256="$2"
      shift 2
      ;;
    --disk-size)
      (( $# >= 2 )) || die "--disk-size benoetigt einen Wert"
      DISK_SIZE_GB="$2"
      shift 2
      ;;
    --memory)
      (( $# >= 2 )) || die "--memory benoetigt einen Wert"
      MEMORY_MB="$2"
      shift 2
      ;;
    --cores)
      (( $# >= 2 )) || die "--cores benoetigt einen Wert"
      CPU_CORES="$2"
      shift 2
      ;;
    --repository-ref)
      (( $# >= 2 )) || die "--repository-ref benoetigt einen Wert"
      REPOSITORY_REF="$2"
      shift 2
      ;;
    --ssh-key-file)
      (( $# >= 2 )) || die "--ssh-key-file benoetigt einen Wert"
      SSH_KEY_FILE="$2"
      shift 2
      ;;
    --ip-config)
      (( $# >= 2 )) || die "--ip-config benoetigt einen Wert"
      IP_CONFIG="$2"
      shift 2
      ;;
    --readiness-timeout)
      (( $# >= 2 )) || die "--readiness-timeout benoetigt einen Wert"
      READINESS_TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --install-dependencies)
      INSTALL_DEPENDENCIES=true
      shift
      ;;
    --keep-workdir)
      KEEP_WORKDIR=true
      shift
      ;;
    --yes)
      ASSUME_YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unbekannte Option: $1 (Hilfe: --help)"
      ;;
  esac
done

# Erst alle Benutzereingaben validieren. Auf einem Nicht-Proxmox-System werden
# Tippfehler dadurch gemeldet, bevor irgendeine Hostaktion moeglich waere.
[[ -n "$VM_ID" ]] \
  || die "Eine VM-ID ist Pflicht: --vm-id ID, positionale ID oder ORCHESTRATOR_VM_ID"
[[ "$VM_ID" =~ ^[0-9]{1,9}$ ]] \
  || die "VM-ID muss eine positive Ganzzahl mit hoechstens neun Stellen sein: ${VM_ID}"
(( 10#$VM_ID >= 100 && 10#$VM_ID <= 999999999 )) \
  || die "VM-ID muss zwischen 100 und 999999999 liegen"
VM_ID="$((10#$VM_ID))"

[[ "$STORAGE" =~ ^[A-Za-z0-9._-]+$ ]] || die "Ungueltiger Storage-Name: ${STORAGE}"
[[ "$BRIDGE" =~ ^[A-Za-z0-9._-]+$ ]] || die "Ungueltiger Bridge-Name: ${BRIDGE}"
[[ "$DISK_SIZE_GB" =~ ^[0-9]+$ ]] || die "--disk-size muss eine Ganzzahl sein"
[[ "$MEMORY_MB" =~ ^[0-9]+$ ]] || die "--memory muss eine Ganzzahl sein"
[[ "$CPU_CORES" =~ ^[0-9]+$ ]] || die "--cores muss eine Ganzzahl sein"
[[ "$READINESS_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] \
  || die "--readiness-timeout muss eine Ganzzahl sein"
(( 10#$DISK_SIZE_GB >= 20 )) || die "Die Systemdisk muss mindestens 20 GiB gross sein"
(( 10#$MEMORY_MB >= 4096 )) || die "Die Orchestrator-VM braucht mindestens 4096 MiB RAM"
(( 10#$CPU_CORES >= 2 && 10#$CPU_CORES <= 256 )) \
  || die "--cores muss zwischen 2 und 256 liegen"
(( 10#$READINESS_TIMEOUT_SECONDS >= 60 && 10#$READINESS_TIMEOUT_SECONDS <= 3600 )) \
  || die "--readiness-timeout muss zwischen 60 und 3600 Sekunden liegen"
DISK_SIZE_GB="$((10#$DISK_SIZE_GB))"
MEMORY_MB="$((10#$MEMORY_MB))"
CPU_CORES="$((10#$CPU_CORES))"
READINESS_TIMEOUT_SECONDS="$((10#$READINESS_TIMEOUT_SECONDS))"

[[ "$REPOSITORY_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
  || die "--repository-ref darf nur einen sicheren Branch, Tag oder Commit ohne Slash enthalten"
[[ "$IP_CONFIG" =~ ^[A-Za-z0-9:=,./_-]+$ ]] \
  || die "--ip-config enthaelt ungueltige Zeichen"

case "$UBUNTU_RELEASE" in
  noble) UBUNTU_VERSION="24.04" ;;
  jammy) UBUNTU_VERSION="22.04" ;;
  *) die "--ubuntu-release unterstuetzt nur noble oder jammy" ;;
esac

VM_NAME="${VM_NAME:-k8s-universal-${VM_ID}}"
[[ ${#VM_NAME} -le 63 && "$VM_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || die "VM-Name muss 1..63 sichere Zeichen enthalten"

CANONICAL_IMAGE_URL="https://cloud-images.ubuntu.com/releases/${UBUNTU_RELEASE}/release/ubuntu-${UBUNTU_VERSION}-server-cloudimg-amd64.img"
IMAGE_URL="${IMAGE_URL:-$CANONICAL_IMAGE_URL}"
[[ "$IMAGE_URL" =~ ^https://[^[:space:]?#]+$ ]] \
  || die "Das Cloud-Image muss eine HTTPS-URL ohne Leerzeichen, Query oder Fragment verwenden"
if [[ "$IMAGE_URL" != "$CANONICAL_IMAGE_URL" && -z "$IMAGE_SHA256" ]]; then
  die "Eine abweichende --image-url erfordert immer einen unabhaengigen --image-sha256"
fi
if [[ -n "$IMAGE_SHA256" ]]; then
  [[ "$IMAGE_SHA256" =~ ^[A-Fa-f0-9]{64}$ ]] \
    || die "--image-sha256 muss genau 64 Hex-Zeichen enthalten"
  IMAGE_SHA256="${IMAGE_SHA256,,}"
fi

(( EUID == 0 )) || die "Dieses Skript muss als root direkt auf dem Proxmox-Host laufen"
[[ -d /etc/pve ]] || die "/etc/pve fehlt; dies ist kein aktiver Proxmox-VE-Host"
for command_name in qm pvesh pvesm pveversion ip hostname mktemp mkdir awk grep ssh-keygen sleep; do
  require_command "$command_name"
done

NODE_NAME="$(hostname -s)"
pvesh get "/nodes/${NODE_NAME}/status" --output-format json >/dev/null \
  || die "Der lokale Proxmox-Node ${NODE_NAME} ist ueber die API nicht erreichbar"

assert_vm_id_is_free() {
  local resources
  resources="$(pvesh get /cluster/resources --type vm --output-format json)" \
    || die "Proxmox-Clusterressourcen konnten nicht gelesen werden"

  if grep -Eq "\"vmid\"[[:space:]]*:[[:space:]]*${VM_ID}([^0-9]|$)" <<<"$resources"; then
    die "VM-ID ${VM_ID} ist im Proxmox-Cluster bereits belegt; es wird nichts ueberschrieben"
  fi
}

assert_vm_id_is_free

STORAGE_CONFIG="$(pvesh get "/storage/${STORAGE}" --output-format json 2>/dev/null)" \
  || die "Storage ${STORAGE} ist in Proxmox nicht konfiguriert"
if ! grep -Eq '"content"[[:space:]]*:[[:space:]]*"([^" ]*,)*images(,[^"]*)?"' <<<"$STORAGE_CONFIG"; then
  die "Storage ${STORAGE} erlaubt keine QEMU-Disk-Images (content=images fehlt)"
fi

STORAGE_STATUS="$(pvesm status --storage "$STORAGE")" \
  || die "Status von Storage ${STORAGE} konnte nicht gelesen werden"
if ! awk -v storage="$STORAGE" '$1 == storage && $3 == "active" { found=1 } END { exit !found }' <<<"$STORAGE_STATUS"; then
  die "Storage ${STORAGE} ist auf Node ${NODE_NAME} nicht aktiv"
fi

ip link show dev "$BRIDGE" >/dev/null 2>&1 \
  || die "Bridge ${BRIDGE} existiert auf Node ${NODE_NAME} nicht"

[[ -n "$SSH_KEY_FILE" ]] \
  || die "--ssh-key-file PFAD oder ORCHESTRATOR_SSH_KEY_FILE ist fuer den VM-Zugang erforderlich"
[[ -f "$SSH_KEY_FILE" && -r "$SSH_KEY_FILE" && -s "$SSH_KEY_FILE" ]] \
  || die "SSH-Key-Datei ist nicht lesbar oder leer: ${SSH_KEY_FILE}"
if grep -q 'PRIVATE KEY' "$SSH_KEY_FILE"; then
  die "--ssh-key-file darf niemals einen privaten SSH-Schluessel enthalten"
fi
grep -Eq '(^|[[:space:]])(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com)[[:space:]]+[A-Za-z0-9+/]+={0,3}([[:space:]]|$)' "$SSH_KEY_FILE" \
  || die "SSH-Key-Datei enthaelt keine unterstuetzte Public-Key-Zeile: ${SSH_KEY_FILE}"
ssh-keygen -l -f "$SSH_KEY_FILE" >/dev/null \
  || die "SSH-Key-Datei enthaelt keinen gueltigen Public Key: ${SSH_KEY_FILE}"

printf '\nGeplante Orchestrator-VM:\n'
printf '  Proxmox-Version: %s\n' "$(pveversion | head -n 1)"
printf '  Ziel-Node:        %s\n' "$NODE_NAME"
printf '  VM-ID:            %s\n' "$VM_ID"
printf '  VM-Name:          %s\n' "$VM_NAME"
printf '  Storage/Bridge:   %s / %s\n' "$STORAGE" "$BRIDGE"
printf '  Ressourcen:       %s vCPU, %s MiB RAM, %s GiB Disk\n' "$CPU_CORES" "$MEMORY_MB" "$DISK_SIZE_GB"
printf '  Netzwerk:         %s\n' "$IP_CONFIG"
printf '  Ubuntu:           %s (%s)\n' "$UBUNTU_RELEASE" "$UBUNTU_VERSION"
printf '  Repository-Ref:   %s\n' "$REPOSITORY_REF"
printf '  Zielverzeichnis:  %s\n' "$PROJECT_DIR"
printf '  SSH-Key-Datei:    %s\n' "$SSH_KEY_FILE"
printf '  Readiness-Timeout: %s Sekunden\n\n' "$READINESS_TIMEOUT_SECONDS"

if [[ "$ASSUME_YES" != true ]]; then
  confirmation=""
  read -r -p "Zur Bestaetigung die VM-ID ${VM_ID} erneut eingeben: " confirmation </dev/tty \
    || die "Keine interaktive Eingabe moeglich; nach eigener Pruefung --yes verwenden"
  [[ "$confirmation" == "$VM_ID" ]] \
    || die "Bestaetigung stimmt nicht; Abbruch ohne Aenderung"
fi

ensure_image_dependencies() {
  local missing=()
  local command_name

  for command_name in curl gpg sha256sum virt-customize; do
    command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
  done
  (( ${#missing[@]} == 0 )) && return

  if [[ "$INSTALL_DEPENDENCIES" != true ]]; then
    die "Fehlende Werkzeuge: ${missing[*]}. Erneut mit --install-dependencies starten oder Pakete manuell installieren"
  fi

  require_command apt-get
  log "Installiere explizit angeforderte Host-Abhaengigkeiten"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl coreutils gnupg libguestfs-tools
  hash -r

  for command_name in curl gpg sha256sum virt-customize; do
    require_command "$command_name"
  done
}

ensure_image_dependencies

WORK_DIR="$(mktemp -d "/var/tmp/k8s-proxmox-orchestrator.${VM_ID}.XXXXXX")"
IMAGE_FILENAME="${IMAGE_URL##*/}"
IMAGE_PATH="${WORK_DIR}/${IMAGE_FILENAME}"
SHA256_PATH="${WORK_DIR}/SHA256SUMS"
SHA256_SIGNATURE_PATH="${WORK_DIR}/SHA256SUMS.gpg"
UBUNTU_SIGNING_KEY_PATH="${WORK_DIR}/ubuntu-cloud-image-signing-key.asc"
GPG_HOME="${WORK_DIR}/gnupg"
REPOSITORY_METADATA_PATH="${WORK_DIR}/repository-commit.json"
COMPOSE_PATH="${WORK_DIR}/compose.yaml"
ENV_EXAMPLE_PATH="${WORK_DIR}/.env.example"

download_https() {
  local url="$1"
  local output="$2"
  curl --fail --show-error --silent --location \
    --retry 3 --retry-all-errors --connect-timeout 20 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --user-agent 'K8S-Universal-Proxmox-Bootstrap/1.0' \
    --output "$output" "$url"
}

if [[ "$REPOSITORY_REF" =~ ^[a-f0-9]{40}$ ]]; then
  RESOLVED_REPOSITORY_COMMIT="$REPOSITORY_REF"
else
  log "Loese Repository-Ref ${REPOSITORY_REF} auf eine feste Commit-SHA auf"
  download_https \
    "${REPOSITORY_API_URL}/commits/${REPOSITORY_REF}" \
    "$REPOSITORY_METADATA_PATH"
  RESOLVED_REPOSITORY_COMMIT="$(awk -F '"' '
    /^[[:space:]]*"sha":[[:space:]]*"[a-f0-9]{40}"/ {
      print $4
      exit
    }
  ' "$REPOSITORY_METADATA_PATH")"
fi
[[ "$RESOLVED_REPOSITORY_COMMIT" =~ ^[a-f0-9]{40}$ ]] \
  || die "Repository-Ref ${REPOSITORY_REF} konnte nicht auf eine Commit-SHA aufgeloest werden"
log "Repository-Snapshot: ${RESOLVED_REPOSITORY_COMMIT}"

log "Lade Ubuntu-Cloud-Image herunter"
download_https "$IMAGE_URL" "$IMAGE_PATH"

if [[ -z "$IMAGE_SHA256" ]]; then
  SHA256_URL="${IMAGE_URL%/*}/SHA256SUMS"
  SHA256_SIGNATURE_URL="${IMAGE_URL%/*}/SHA256SUMS.gpg"
  log "Lade signierte Ubuntu-SHA-256-Liste herunter"
  download_https "$SHA256_URL" "$SHA256_PATH"
  download_https "$SHA256_SIGNATURE_URL" "$SHA256_SIGNATURE_PATH"
  download_https \
    "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${UBUNTU_CLOUD_IMAGE_KEY_FINGERPRINT}" \
    "$UBUNTU_SIGNING_KEY_PATH"

  mkdir -m 0700 "$GPG_HOME"
  gpg --batch --quiet --homedir "$GPG_HOME" --import "$UBUNTU_SIGNING_KEY_PATH" \
    || die "Der offizielle Ubuntu-Cloud-Image-Signierschluessel konnte nicht importiert werden"
  IMPORTED_KEY_FINGERPRINT="$(
    gpg --batch --homedir "$GPG_HOME" --with-colons \
      --fingerprint "$UBUNTU_CLOUD_IMAGE_KEY_FINGERPRINT" 2>/dev/null \
      | awk -F: '$1 == "fpr" { print toupper($10); exit }'
  )"
  [[ "$IMPORTED_KEY_FINGERPRINT" == "$UBUNTU_CLOUD_IMAGE_KEY_FINGERPRINT" ]] \
    || die "Der geladene Ubuntu-Signierschluessel besitzt einen unerwarteten Fingerprint"
  gpg --batch --homedir "$GPG_HOME" \
    --verify "$SHA256_SIGNATURE_PATH" "$SHA256_PATH" \
    || die "Die GPG-Signatur der Ubuntu-SHA-256-Liste ist ungueltig"
  log "Ubuntu-SHA-256-Liste ist gueltig signiert"

  IMAGE_SHA256="$(awk -v file="$IMAGE_FILENAME" '
    {
      name=$2
      sub(/^\*/, "", name)
      if (name == file) {
        print tolower($1)
        exit
      }
    }
  ' "$SHA256_PATH")"
  [[ "$IMAGE_SHA256" =~ ^[a-f0-9]{64}$ ]] \
    || die "Kein eindeutiger SHA-256-Eintrag fuer ${IMAGE_FILENAME} gefunden"
fi

ACTUAL_SHA256="$(sha256sum "$IMAGE_PATH" | awk '{ print tolower($1) }')"
[[ "$ACTUAL_SHA256" == "$IMAGE_SHA256" ]] \
  || die "SHA-256-Pruefung fehlgeschlagen: erwartet ${IMAGE_SHA256}, erhalten ${ACTUAL_SHA256}"
log "Ubuntu-SHA-256-Pruefung erfolgreich"

log "Lade compose.yaml und .env.example aus demselben Repository-Commit"
download_https \
  "${REPOSITORY_BASE_URL}/${RESOLVED_REPOSITORY_COMMIT}/compose.yaml" \
  "$COMPOSE_PATH"
download_https \
  "${REPOSITORY_BASE_URL}/${RESOLVED_REPOSITORY_COMMIT}/.env.example" \
  "$ENV_EXAMPLE_PATH"
[[ -s "$COMPOSE_PATH" ]] || die "Heruntergeladene compose.yaml ist leer"
[[ -s "$ENV_EXAMPLE_PATH" ]] || die "Heruntergeladene .env.example ist leer"
grep -Eq '^name:[[:space:]]*' "$COMPOSE_PATH" \
  || die "compose.yaml besitzt nicht die erwartete Compose-Struktur"
grep -Eq '^COMPOSE_PROJECT_NAME=' "$ENV_EXAMPLE_PATH" \
  || die ".env.example besitzt nicht die erwarteten K8s-Universal-Variablen"

# Die Image-Anpassung geschieht offline vor dem Import. Docker folgt der
# offiziellen Ubuntu-APT-Anleitung; die Compose-v2-CLI wird als Plugin
# installiert. Docker startet erst mit dem normalen Boot der Ziel-VM.
log "Installiere Docker Engine, Compose v2 und Projektdateien im Ubuntu-Image"
LIBGUESTFS_BACKEND=direct virt-customize -a "$IMAGE_PATH" --network \
  --install cloud-init,openssh-server,qemu-guest-agent,sudo,ca-certificates,curl,nano \
  --run-command 'install -m 0755 -d /etc/apt/keyrings' \
  --run-command "curl --fail --show-error --silent --location --retry 3 --proto '=https' --proto-redir '=https' --tlsv1.2 --output /etc/apt/keyrings/docker.asc https://download.docker.com/linux/ubuntu/gpg" \
  --run-command 'chmod a+r /etc/apt/keyrings/docker.asc' \
  --run-command '. /etc/os-release; codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"; architecture="$(dpkg --print-architecture)"; printf "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n" "$codename" "$architecture" > /etc/apt/sources.list.d/docker.sources' \
  --run-command 'apt-get update' \
  --install docker-ce,docker-ce-cli,containerd.io,docker-buildx-plugin,docker-compose-plugin \
  --mkdir "$PROJECT_DIR" \
  --copy-in "$COMPOSE_PATH:$PROJECT_DIR" \
  --copy-in "$ENV_EXAMPLE_PATH:$PROJECT_DIR" \
  --run-command "id ${CLOUD_USER} >/dev/null" \
  --run-command "usermod -aG docker ${CLOUD_USER}" \
  --run-command "chown -R ubuntu:ubuntu ${PROJECT_DIR}" \
  --run-command "chmod 0750 ${PROJECT_DIR}" \
  --run-command "chmod 0640 ${PROJECT_DIR}/compose.yaml ${PROJECT_DIR}/.env.example" \
  --run-command 'systemctl enable docker.service containerd.service ssh.service qemu-guest-agent.service' \
  --run-command 'docker --version' \
  --run-command 'docker compose version' \
  --run-command "test -s ${PROJECT_DIR}/compose.yaml" \
  --run-command "test -s ${PROJECT_DIR}/.env.example" \
  --run-command 'cloud-init clean --logs --seed' \
  --run-command 'truncate -s 0 /etc/machine-id' \
  --run-command 'rm -f /var/lib/dbus/machine-id' \
  --run-command 'rm -f /etc/ssh/ssh_host_*' \
  --run-command 'apt-get clean' \
  --run-command 'rm -rf /var/lib/apt/lists/*'

# Download und Anpassung koennen dauern. Direkt vor der ersten Mutation wird
# deshalb erneut clusterweit geprueft, ob die ID weiterhin frei ist.
assert_vm_id_is_free

log "Lege die noch ausgeschaltete Orchestrator-VM ${VM_ID} an"
qm create "$VM_ID" \
  --name "$VM_NAME" \
  --description "K8s Universal orchestrator; Docker Compose; repository commit ${RESOLVED_REPOSITORY_COMMIT}" \
  --ostype l26 \
  --cpu host \
  --cores "$CPU_CORES" \
  --memory "$MEMORY_MB" \
  --scsihw virtio-scsi-single \
  --net0 "virtio,bridge=${BRIDGE}" \
  --agent "enabled=1,fstrim_cloned_disks=1" \
  --serial0 socket \
  --vga serial0 \
  --onboot 0
VM_CREATED=true

log "Importiere die Systemdisk nach ${STORAGE}"
qm importdisk "$VM_ID" "$IMAGE_PATH" "$STORAGE"

mapfile -t UNUSED_VOLUMES < <(
  qm config "$VM_ID" | awk -F ': ' '/^unused[0-9]+: / { sub(/,.*/, "", $2); print $2 }'
)
(( ${#UNUSED_VOLUMES[@]} == 1 )) \
  || die "Erwartete genau eine importierte unused-Disk, gefunden: ${#UNUSED_VOLUMES[@]}"
IMPORTED_VOLUME="${UNUSED_VOLUMES[0]}"

log "Binde Systemdisk und Cloud-Init-Disk ein"
qm set "$VM_ID" --scsi0 "${IMPORTED_VOLUME},discard=on,ssd=1"
qm resize "$VM_ID" scsi0 "${DISK_SIZE_GB}G"
qm set "$VM_ID" --ide2 "${STORAGE}:cloudinit"
qm set "$VM_ID" --boot "order=scsi0"
qm set "$VM_ID" --ciuser "$CLOUD_USER"
qm set "$VM_ID" --sshkeys "$SSH_KEY_FILE"
qm set "$VM_ID" --ipconfig0 "$IP_CONFIG"

VM_CONFIG="$(qm config "$VM_ID")"
grep -Eq '^scsi0: ' <<<"$VM_CONFIG" || die "Systemdisk scsi0 fehlt"
grep -Eq '^ide2: .*cloudinit' <<<"$VM_CONFIG" || die "Cloud-Init-Disk fehlt"
grep -Eq "$AGENT_CONFIG_PATTERN" <<<"$VM_CONFIG" || die "QEMU Guest Agent ist nicht aktiviert"
grep -Eq '^agent: .*fstrim_cloned_disks=1' <<<"$VM_CONFIG" \
  || die "Guest-Agent-FSTRIM ist nicht aktiviert"
grep -Eq "^net0: .*bridge=${BRIDGE}([,[:space:]]|$)" <<<"$VM_CONFIG" \
  || die "Netzwerk-Bridge stimmt nicht"
grep -Eq '^boot: order=scsi0' <<<"$VM_CONFIG" || die "Boot-Reihenfolge stimmt nicht"
grep -Eq "^ciuser: ${CLOUD_USER}$" <<<"$VM_CONFIG" || die "Cloud-Init-Benutzer stimmt nicht"
grep -Eq '^ipconfig0: ' <<<"$VM_CONFIG" || die "Cloud-Init-Netzwerk fehlt"

log "Aktiviere Proxmox-Autostart erst nach vollstaendiger VM-Konfiguration"
qm set "$VM_ID" --onboot 1
VM_CONFIG="$(qm config "$VM_ID")"
grep -Eq '^onboot: 1$' <<<"$VM_CONFIG" || die "Proxmox-Autostart ist nicht aktiviert"

log "Starte Orchestrator-VM ${VM_ID}"
qm start "$VM_ID"

log "Warte auf QEMU Guest Agent (maximal ${READINESS_TIMEOUT_SECONDS} Sekunden)"
AGENT_READY=false
READINESS_INTERVAL_SECONDS=5
READINESS_ATTEMPTS="$((READINESS_TIMEOUT_SECONDS / READINESS_INTERVAL_SECONDS))"
for (( attempt=1; attempt<=READINESS_ATTEMPTS; attempt++ )); do
  if qm guest cmd "$VM_ID" ping >/dev/null 2>&1; then
    AGENT_READY=true
    break
  fi
  sleep "$READINESS_INTERVAL_SECONDS"
done
[[ "$AGENT_READY" == true ]] \
  || die "QEMU Guest Agent wurde innerhalb von ${READINESS_TIMEOUT_SECONDS} Sekunden nicht erreichbar"

log "Pruefe Cloud-Init, Docker-Daemon, Compose v2 und Projektdateien im Gast"
GUEST_VERIFY_COMMAND="cloud-init status --wait >/dev/null && systemctl is-active --quiet docker.service && docker compose version >/dev/null && test -s ${PROJECT_DIR}/compose.yaml && test -s ${PROJECT_DIR}/.env.example && test \"\$(stat -c %U:%G ${PROJECT_DIR})\" = ubuntu:ubuntu && printf 'K8S_UNIVERSAL_GUEST_READY\\n'"
GUEST_VERIFY_OUTPUT="$(
  qm guest exec "$VM_ID" --timeout "$READINESS_TIMEOUT_SECONDS" \
    -- /bin/sh -c "$GUEST_VERIFY_COMMAND"
)" || die "Gastpruefung fuer Cloud-Init, Docker oder Projektdateien ist fehlgeschlagen"
grep -Fq 'K8S_UNIVERSAL_GUEST_READY' <<<"$GUEST_VERIFY_OUTPUT" \
  || die "Gastpruefung lieferte keine eindeutige Bereitschaftsbestaetigung"
FINISHED=true

printf '\nOrchestrator-VM erfolgreich erstellt und im Gast verifiziert.\n'
printf '  Node:             %s\n' "$NODE_NAME"
printf '  VM-ID:            %s\n' "$VM_ID"
printf '  Name:             %s\n' "$VM_NAME"
printf '  Login:            %s (SSH-Key: %s)\n' "$CLOUD_USER" "$SSH_KEY_FILE"
printf '  Netzwerk:         %s\n' "$IP_CONFIG"
printf '  Repository-Commit: %s\n' "$RESOLVED_REPOSITORY_COMMIT"
printf '  Projekt:          %s\n\n' "$PROJECT_DIR"
printf 'Per SSH anmelden und ausfuehren:\n\n'
printf '  cd %s\n' "$PROJECT_DIR"
printf '  cp .env.example .env\n'
printf '  chmod 600 .env\n'
printf '  nano .env\n'
printf '  docker compose up -d\n\n'
printf 'In .env mindestens alle password-Platzhalter ersetzen. Fuer LAN-Zugriff\n'
printf 'BUILDER_BIND_ADDRESS auf die private VM-IP oder bewusst auf 0.0.0.0 setzen.\n'
printf 'Ohne vorgeschaltetes TLS nur in einem vertrauenswuerdigen LAN/VPN betreiben.\n'
