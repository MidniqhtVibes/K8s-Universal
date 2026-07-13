#!/usr/bin/env bash
#
# Erstellt auf einem Proxmox-VE-Node ein Ubuntu-QEMU-Template fuer den
# Proxmox Kubernetes Cluster Builder.
#
# WARUM DIESES SKRIPT EXISTIERT
# ----------------------------
# Die Webanwendung erstellt Cluster-VMs, indem Terraform ein bereits
# vorhandenes Proxmox-Template vollstaendig klont. Das Template selbst kann
# nicht sinnvoll aus dem Builder-Container heraus erstellt werden: Das
# Herunterladen und Importieren des Basis-Images ist eine einmalige
# Administratoraufgabe direkt auf dem Proxmox-Host.
#
# SICHERHEITSMODELL
# -----------------
# - Das Skript muss direkt als root auf dem Ziel-Proxmox-Node laufen.
# - Die Template-VM-ID besitzt absichtlich keinen statischen Default.
# - Die ID wird vor und unmittelbar vor `qm create` clusterweit geprueft.
# - Eine vorhandene VM oder ein vorhandenes Template wird nie ueberschrieben.
# - Bei einem Fehler wird eine bereits angelegte, unvollstaendige VM nicht
#   automatisch geloescht. Der Administrator kann ihren Zustand zuerst pruefen.
# - Downloads sind ausschliesslich ueber HTTPS erlaubt und werden gegen den
#   von Ubuntu veroeffentlichten SHA-256-Wert geprueft.
# - Zugangsdaten, SSH-Schluessel und feste IP-Adressen werden nicht eingebaut.
#   Diese Werte injiziert der Builder spaeter pro Clone ueber Cloud-Init.
#
# BEISPIEL
# --------
# bash create-template.sh \
#   --vm-id 9100 \
#   --storage local-lvm \
#   --bridge vmbr0 \
#   --ubuntu-release noble \
#   --install-dependencies
#
# Die VM-ID 9100 ist nur ein Beispiel. Sie muss im gesamten Proxmox-Cluster
# frei sein und darf spaeter nicht als ID einer Kubernetes-VM verwendet werden.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

# TEMPLATE_VM_ID kann fuer Automatisierung als Umgebungsvariable gesetzt
# werden. Auch dann gibt es keinen festen Wert im Skript.
VM_ID="${TEMPLATE_VM_ID:-}"
STORAGE="local-lvm"
BRIDGE="vmbr0"
TEMPLATE_NAME=""
UBUNTU_RELEASE="noble"
IMAGE_URL=""
IMAGE_SHA256=""
DISK_SIZE_GB=20
MEMORY_MB=2048
CPU_CORES=2
INSTALL_DEPENDENCIES=false
KEEP_WORKDIR=false
ASSUME_YES=false

# Proxmox akzeptiert beim Setzen `enabled=1`, serialisiert dieselbe Property
# beim Lesen je nach Version aber entweder als `agent: enabled=1,...` oder in
# der kanonisch verkuerzten Form `agent: 1,...`. Die Pruefung muss beide
# gleichwertigen Darstellungen akzeptieren.
readonly AGENT_CONFIG_PATTERN='^agent: (1|enabled=1)(,|$)'

WORK_DIR=""
VM_CREATED=false
FINISHED=false

usage() {
  cat <<'EOF'
Erstellt ein Cloud-Init-faehiges Ubuntu-QEMU-Template auf diesem Proxmox-Node.

Aufruf:
  bash proxmox/create-template.sh --vm-id ID [Optionen]

Pflichtoption:
  --vm-id ID                 Clusterweit freie Proxmox-VM-ID (100..999999999).
                              Alternativ: Umgebungsvariable TEMPLATE_VM_ID.

Optionen:
  --storage NAME             Proxmox-Storage fuer System- und Cloud-Init-Disk
                              (Standard: local-lvm).
  --bridge NAME              Lokale Proxmox-Netzwerk-Bridge
                              (Standard: vmbr0).
  --name NAME                Name des Templates. Ohne Angabe wird ein Name aus
                              der Ubuntu-Version gebildet.
  --ubuntu-release RELEASE   noble (Ubuntu 24.04) oder jammy (Ubuntu 22.04).
                              Standard: noble.
  --image-url URL            Abweichendes Ubuntu-Cloud-Image. Nur HTTPS.
  --image-sha256 HASH        Erwarteter SHA-256-Hash fuer --image-url. Ohne
                              Angabe wird SHA256SUMS aus demselben Verzeichnis
                              verwendet.
  --disk-size GB             Groesse der Template-Systemdisk in GiB
                              (Standard/Minimum: 8).
  --memory MB                Template-Arbeitsspeicher (Standard: 2048).
  --cores ANZAHL             Template-vCPU-Anzahl (Standard: 2).
  --install-dependencies     Fehlende Debian-Pakete explizit installieren.
  --keep-workdir             Download und Arbeitsdateien nach dem Lauf behalten.
  --yes                      Rueckfrage ueberspringen. Alle Schutzpruefungen
                              bleiben aktiv.
  -h, --help                 Diese Hilfe anzeigen.

Das Skript wird direkt als root auf dem Proxmox-Host ausgefuehrt, nicht im
Builder-Container. Webanwendung, Worker und Ansible starten es niemals selbst.
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
  command -v "$command_name" >/dev/null 2>&1 || die "Benoetigter Befehl fehlt: ${command_name}"
}

cleanup() {
  local exit_code=$?

  if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
    if [[ "$KEEP_WORKDIR" == true ]]; then
      warn "Arbeitsverzeichnis bleibt erhalten: ${WORK_DIR}"
    else
      # Der Pfad wird von mktemp mit genau diesem Praefix erzeugt. Die
      # Fallunterscheidung verhindert, dass bei einem Variablenfehler ein
      # beliebiges Verzeichnis entfernt werden kann.
      case "$WORK_DIR" in
        "/var/tmp/k8s-proxmox-template.${VM_ID}."*) rm -rf -- "$WORK_DIR" ;;
        *) warn "Unerwarteter Temp-Pfad wird nicht entfernt: ${WORK_DIR}" ;;
      esac
    fi
  fi

  if (( exit_code != 0 )) && [[ "$VM_CREATED" == true && "$FINISHED" != true ]]; then
    warn "VM ${VM_ID} wurde durch dieses Skript angelegt, aber nicht fertiggestellt."
    warn "Bitte mit 'qm config ${VM_ID}' pruefen und erst danach bewusst bereinigen."
  fi
}
trap cleanup EXIT

while (( $# > 0 )); do
  case "$1" in
    --vm-id)
      (( $# >= 2 )) || die "--vm-id benoetigt einen Wert"
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
      TEMPLATE_NAME="$2"
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
    --)
      shift
      (( $# == 0 )) || die "Positionsargumente werden nicht unterstuetzt: $*"
      ;;
    *)
      die "Unbekannte Option: $1 (Hilfe: --help)"
      ;;
  esac
done

# Eingaben werden vor root- und Proxmox-Pruefungen validiert. Dadurch sind
# Tippfehler frueh sichtbar und es wurde zu diesem Zeitpunkt nichts veraendert.
[[ -n "$VM_ID" ]] || die "Eine VM-ID ist Pflicht: --vm-id ID oder TEMPLATE_VM_ID setzen"
[[ "$VM_ID" =~ ^[0-9]{1,9}$ ]] || die "VM-ID muss eine positive Ganzzahl mit hoechstens neun Stellen sein: ${VM_ID}"
(( 10#$VM_ID >= 100 && 10#$VM_ID <= 999999999 )) || die "VM-ID muss zwischen 100 und 999999999 liegen"
VM_ID="$((10#$VM_ID))"

[[ "$STORAGE" =~ ^[A-Za-z0-9._-]+$ ]] || die "Ungueltiger Storage-Name: ${STORAGE}"
[[ "$BRIDGE" =~ ^[A-Za-z0-9._-]+$ ]] || die "Ungueltiger Bridge-Name: ${BRIDGE}"
[[ "$DISK_SIZE_GB" =~ ^[0-9]+$ ]] || die "--disk-size muss eine Ganzzahl sein"
[[ "$MEMORY_MB" =~ ^[0-9]+$ ]] || die "--memory muss eine Ganzzahl sein"
[[ "$CPU_CORES" =~ ^[0-9]+$ ]] || die "--cores muss eine Ganzzahl sein"
(( 10#$DISK_SIZE_GB >= 8 )) || die "Die Template-Disk muss mindestens 8 GiB gross sein"
(( 10#$MEMORY_MB >= 512 )) || die "Das Template braucht mindestens 512 MiB RAM"
(( 10#$CPU_CORES >= 1 && 10#$CPU_CORES <= 256 )) || die "--cores muss zwischen 1 und 256 liegen"
DISK_SIZE_GB="$((10#$DISK_SIZE_GB))"
MEMORY_MB="$((10#$MEMORY_MB))"
CPU_CORES="$((10#$CPU_CORES))"

case "$UBUNTU_RELEASE" in
  noble)
    UBUNTU_VERSION="24.04"
    DEFAULT_TEMPLATE_NAME="ubuntu-2404-k8s-template"
    ;;
  jammy)
    UBUNTU_VERSION="22.04"
    DEFAULT_TEMPLATE_NAME="ubuntu-2204-k8s-template"
    ;;
  *)
    die "--ubuntu-release unterstuetzt nur noble oder jammy"
    ;;
esac

TEMPLATE_NAME="${TEMPLATE_NAME:-$DEFAULT_TEMPLATE_NAME}"
[[ ${#TEMPLATE_NAME} -le 63 && "$TEMPLATE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || die "Template-Name muss 1..63 sichere Zeichen enthalten"

if [[ -z "$IMAGE_URL" ]]; then
  IMAGE_URL="https://cloud-images.ubuntu.com/releases/${UBUNTU_RELEASE}/release/ubuntu-${UBUNTU_VERSION}-server-cloudimg-amd64.img"
fi
[[ "$IMAGE_URL" =~ ^https://[^[:space:]?#]+$ ]] || die "Das Cloud-Image muss eine HTTPS-URL ohne Leerzeichen, Query oder Fragment verwenden"
if [[ -n "$IMAGE_SHA256" ]]; then
  [[ "$IMAGE_SHA256" =~ ^[A-Fa-f0-9]{64}$ ]] || die "--image-sha256 muss genau 64 Hex-Zeichen enthalten"
  IMAGE_SHA256="${IMAGE_SHA256,,}"
fi

(( EUID == 0 )) || die "Dieses Skript muss als root direkt auf dem Proxmox-Host laufen"

# Diese Befehle duerfen niemals auf einem normalen Debian-Host automatisch
# installiert werden. Ihr Vorhandensein und /etc/pve identifizieren Proxmox VE.
[[ -d /etc/pve ]] || die "/etc/pve fehlt; dies ist kein aktiver Proxmox-VE-Host"
for command_name in qm pvesh pvesm pveversion ip hostname mktemp awk grep; do
  require_command "$command_name"
done

NODE_NAME="$(hostname -s)"
pvesh get "/nodes/${NODE_NAME}/status" --output-format json >/dev/null \
  || die "Der lokale Proxmox-Node ${NODE_NAME} ist ueber die API nicht erreichbar"

assert_vm_id_is_free() {
  local resources
  resources="$(pvesh get /cluster/resources --type vm --output-format json)" \
    || die "Proxmox-Clusterressourcen konnten nicht gelesen werden"

  # VM_ID ist vorher auf Ziffern beschraenkt. Die JSON-Suche erfasst QEMU-VMs,
  # Templates und LXC-Container auf allen Nodes des Proxmox-Clusters.
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

printf '\nGeplante Template-Erstellung:\n'
printf '  Proxmox-Version: %s\n' "$(pveversion | head -n 1)"
printf '  Ziel-Node:        %s\n' "$NODE_NAME"
printf '  VM-ID:            %s\n' "$VM_ID"
printf '  Template-Name:    %s\n' "$TEMPLATE_NAME"
printf '  Storage:          %s\n' "$STORAGE"
printf '  Bridge:           %s\n' "$BRIDGE"
printf '  Ubuntu:           %s (%s)\n' "$UBUNTU_RELEASE" "$UBUNTU_VERSION"
printf '  Systemdisk:       %s GiB\n\n' "$DISK_SIZE_GB"

if [[ "$ASSUME_YES" != true ]]; then
  [[ -t 0 ]] || die "Keine interaktive Eingabe moeglich; nach eigener Pruefung --yes verwenden"
  read -r -p "Zur Bestaetigung die VM-ID ${VM_ID} erneut eingeben: " confirmation
  [[ "$confirmation" == "$VM_ID" ]] || die "Bestaetigung stimmt nicht; Abbruch ohne Aenderung"
fi

ensure_image_dependencies() {
  local missing=()
  local command_name

  for command_name in curl sha256sum virt-customize; do
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
    ca-certificates curl coreutils libguestfs-tools
  hash -r

  for command_name in curl sha256sum virt-customize; do
    require_command "$command_name"
  done
}

ensure_image_dependencies

WORK_DIR="$(mktemp -d "/var/tmp/k8s-proxmox-template.${VM_ID}.XXXXXX")"
IMAGE_FILENAME="${IMAGE_URL##*/}"
IMAGE_PATH="${WORK_DIR}/${IMAGE_FILENAME}"
SHA256_PATH="${WORK_DIR}/SHA256SUMS"

download_https() {
  local url="$1"
  local output="$2"
  curl --fail --show-error --silent --location \
    --retry 3 --retry-all-errors --connect-timeout 20 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --output "$output" "$url"
}

log "Lade Ubuntu-Cloud-Image herunter"
download_https "$IMAGE_URL" "$IMAGE_PATH"

if [[ -z "$IMAGE_SHA256" ]]; then
  SHA256_URL="${IMAGE_URL%/*}/SHA256SUMS"
  log "Lade offizielle SHA-256-Liste herunter"
  download_https "$SHA256_URL" "$SHA256_PATH"

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
log "SHA-256-Pruefung erfolgreich"

# virt-customize bearbeitet das Image vor dem Import. Es startet dabei keine
# Ziel-VM auf Proxmox. Der direkte libguestfs-Backendmodus vermeidet eine
# Abhaengigkeit von libvirtd auf dem Proxmox-Host.
log "Installiere Cloud-Init, SSH, sudo und QEMU Guest Agent im Image"
LIBGUESTFS_BACKEND=direct virt-customize -a "$IMAGE_PATH" \
  --install cloud-init,openssh-server,qemu-guest-agent,sudo \
  --run-command 'systemctl enable ssh.service' \
  --run-command 'systemctl enable qemu-guest-agent.service' \
  --run-command 'cloud-init clean --logs --seed' \
  --run-command 'truncate -s 0 /etc/machine-id' \
  --run-command 'rm -f /var/lib/dbus/machine-id' \
  --run-command 'rm -f /etc/ssh/ssh_host_*' \
  --run-command 'command -v cloud-init >/dev/null' \
  --run-command 'test -x /usr/sbin/sshd'

# Zwischen Download/Image-Anpassung und der ersten Proxmox-Mutation kann Zeit
# vergangen sein. Darum wird dieselbe clusterweite Kollisionspruefung wiederholt.
assert_vm_id_is_free

log "Lege die noch ausgeschaltete QEMU-VM ${VM_ID} an"
qm create "$VM_ID" \
  --name "$TEMPLATE_NAME" \
  --description "Ubuntu ${UBUNTU_VERSION} Cloud-Init template for K8s Cluster Builder" \
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

# Vor der irreversiblen Umwandlung in ein Template werden genau die
# Eigenschaften geprueft, auf die Terraform und der Web-Preflight vertrauen.
VM_CONFIG="$(qm config "$VM_ID")"
grep -Eq '^scsi0: ' <<<"$VM_CONFIG" || die "Systemdisk scsi0 fehlt"
grep -Eq '^ide2: .*cloudinit' <<<"$VM_CONFIG" || die "Cloud-Init-Disk fehlt"
grep -Eq "$AGENT_CONFIG_PATTERN" <<<"$VM_CONFIG" || die "QEMU Guest Agent ist nicht aktiviert"
grep -Eq '^agent: .*fstrim_cloned_disks=1' <<<"$VM_CONFIG" || die "Guest-Agent-FSTRIM fuer Clones ist nicht aktiviert"
grep -Eq "^net0: .*bridge=${BRIDGE}([,[:space:]]|$)" <<<"$VM_CONFIG" || die "Netzwerk-Bridge stimmt nicht"
grep -Eq '^boot: order=scsi0' <<<"$VM_CONFIG" || die "Boot-Reihenfolge stimmt nicht"

log "Wandle VM ${VM_ID} in ein unveraenderliches Proxmox-Template um"
qm template "$VM_ID"

FINAL_CONFIG="$(qm config "$VM_ID")"
grep -Eq '^template: 1' <<<"$FINAL_CONFIG" || die "Proxmox meldet VM ${VM_ID} nicht als Template"
FINISHED=true

printf '\nTemplate erfolgreich erstellt.\n'
printf '  Node:     %s\n' "$NODE_NAME"
printf '  VM-ID:    %s\n' "$VM_ID"
printf '  Name:     %s\n' "$TEMPLATE_NAME"
printf '  Storage:  %s\n' "$STORAGE"
printf '  Bridge:   %s\n\n' "$BRIDGE"
printf 'Naechster Schritt: In der Weboberflaeche denselben Proxmox-Node erkennen\n'
printf 'lassen und dort das QEMU-Template mit VM-ID %s auswaehlen.\n' "$VM_ID"
