#!/usr/bin/env bash
#
# Erstellt auf einem Proxmox-VE-Node ein Talos-NoCloud-QEMU-Template fuer den
# Proxmox Kubernetes Cluster Builder.
#
# WARUM DIESES SKRIPT EXISTIERT
# ----------------------------
# Der Builder klont fuer Talos-Control-Planes und -Worker ein bereits
# vorhandenes Proxmox-Template. Das Template muss schon beim ersten Start die
# von Proxmox erzeugte NoCloud-Netzwerkkonfiguration lesen, damit der Worker die
# Talos-API im Maintenance Mode erreichen kann. Das Ubuntu-Template fuer die
# SSH-verwalteten Load Balancer bleibt davon getrennt.
#
# SICHERHEITSMODELL
# -----------------
# - Das Skript muss direkt als root auf dem Ziel-Proxmox-Node laufen.
# - Die Template-VM-ID besitzt absichtlich keinen statischen Default.
# - Die ID wird vor und unmittelbar vor `qm create` clusterweit geprueft.
# - Eine vorhandene VM oder ein vorhandenes Template wird nie ueberschrieben.
# - Bei einem Fehler wird eine bereits angelegte, unvollstaendige VM nicht
#   automatisch geloescht. Der Administrator kann ihren Zustand zuerst pruefen.
# - Das gepinnte offizielle NoCloud-Image wird nur ueber HTTPS geladen und
#   gegen den hier fuer v1.13.6 festgehaltenen SHA-256-Wert geprueft.
# - Es werden keine Machine Config, Cluster-PKI, Machine Identity, SSH-Daten,
#   Hostnamen oder festen IP-Adressen in das Template eingebaut.
# - QEMU Guest Agent und Memory Ballooning bleiben fuer das unveraenderte
#   Talos-Image deaktiviert.
#
# BEISPIEL
# --------
# bash create-talos-template.sh \
#   --vm-id 9200 \
#   --storage local-lvm \
#   --bridge vmbr0 \
#   --talos-version v1.13.6 \
#   --install-disk /dev/sda \
#   --install-dependencies
#
# Die VM-ID 9200 ist nur ein Beispiel. Sie muss im gesamten Proxmox-Cluster
# frei sein und darf spaeter nicht als ID einer Kubernetes-VM verwendet werden.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077
export LC_ALL=C

# TALOS_TEMPLATE_VM_ID kann fuer Automatisierung als Umgebungsvariable gesetzt
# werden. Auch dann gibt es keinen festen Wert im Skript.
VM_ID="${TALOS_TEMPLATE_VM_ID:-}"
STORAGE="local-lvm"
BRIDGE="vmbr0"
TEMPLATE_NAME=""
TALOS_VERSION="v1.13.6"
INSTALL_DISK="/dev/sda"
IMAGE_URL=""
IMAGE_SHA256=""
DISK_SIZE_GB=20
MEMORY_MB=4096
CPU_CORES=2
INSTALL_DEPENDENCIES=false
KEEP_WORKDIR=false
ASSUME_YES=false

# Der Builder unterstuetzt derzeit genau diese Talos-Version und dieses
# unveraenderte NoCloud-Schematic. Der SHA-256-Pin wurde fuer exakt das unten
# angegebene offizielle Image-Factory-Objekt ermittelt. Image Factory stellt
# fuer den oeffentlichen Dienst keinen publisherseitigen Checksum-Endpunkt zur
# Verfuegung; der lokale Pin sorgt dennoch dafuer, dass spaetere Downloads bei
# jeder Byteabweichung sicher stoppen.
readonly SUPPORTED_TALOS_VERSION="v1.13.6"
readonly DEFAULT_SCHEMATIC_ID="376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
readonly DEFAULT_IMAGE_SHA256="d46b9209f9aa9d96d8ee4439351687e2b4519c0d61df2fe974ee533a3ed9ef21"
readonly DEFAULT_IMAGE_SIZE_BYTES=213557488

WORK_DIR=""
VM_CREATED=false
FINISHED=false
DISK_INTERFACE=""

usage() {
  cat <<'EOF'
Erstellt ein NoCloud-faehiges Talos-QEMU-Template auf diesem Proxmox-Node.

Aufruf:
  bash proxmox/create-talos-template.sh --vm-id ID [Optionen]

Pflichtoption:
  --vm-id ID                 Clusterweit freie Proxmox-VM-ID (100..999999999).
                              Alternativ: Umgebungsvariable TALOS_TEMPLATE_VM_ID.

Optionen:
  --storage NAME             Proxmox-Storage fuer System-, EFI- und NoCloud-Disk
                              (Standard: local-lvm).
  --bridge NAME              Lokale Proxmox-Netzwerk-Bridge
                              (Standard: vmbr0).
  --name NAME                Name des Templates. Ohne Angabe wird ein Name aus
                              der Talos-Version gebildet.
  --talos-version VERSION    Vom Builder unterstuetzte Version v1.13.6
                              (Standard: v1.13.6).
  --install-disk DEVICE      /dev/sda (scsi0, empfohlen) oder /dev/vda
                              (virtio0). Der Wizard muss denselben Wert nutzen.
  --image-url URL            Abweichendes Talos-NoCloud-RAW-XZ-Image. Nur HTTPS.
                              Erfordert immer --image-sha256.
  --image-sha256 HASH        Erwarteter SHA-256-Hash fuer --image-url. Beim
                              Standardimage wird der eingebaute Pin verwendet.
  --disk-size GB             Groesse der Template-Systemdisk in GiB
                              (Standard: 20, Minimum: 10).
  --memory MB                Template-Arbeitsspeicher (Standard: 4096,
                              Minimum: 2048).
  --cores ANZAHL             Template-vCPU-Anzahl (Standard/Minimum: 2).
  --install-dependencies     Fehlende Debian-Pakete explizit installieren.
  --keep-workdir             Download und Arbeitsdateien nach dem Lauf behalten.
  --yes                      Rueckfrage ueberspringen. Alle Schutzpruefungen
                              bleiben aktiv.
  -h, --help                 Diese Hilfe anzeigen.

Das Skript wird direkt als root auf dem Proxmox-Host ausgefuehrt, nicht im
Builder-Container. Webanwendung, Worker und Ansible starten es niemals selbst.
Es erzeugt nur das Talos-Template fuer Control Planes und Worker. Die Ubuntu-
Load-Balancer benoetigen weiterhin das getrennte Template aus create-template.sh.
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
        "/var/tmp/k8s-proxmox-talos-template.${VM_ID}."*) rm -rf -- "$WORK_DIR" ;;
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
    --talos-version)
      (( $# >= 2 )) || die "--talos-version benoetigt einen Wert"
      TALOS_VERSION="$2"
      shift 2
      ;;
    --install-disk)
      (( $# >= 2 )) || die "--install-disk benoetigt einen Wert"
      INSTALL_DISK="$2"
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
[[ -n "$VM_ID" ]] || die "Eine VM-ID ist Pflicht: --vm-id ID oder TALOS_TEMPLATE_VM_ID setzen"
[[ "$VM_ID" =~ ^[0-9]{1,9}$ ]] || die "VM-ID muss eine positive Ganzzahl mit hoechstens neun Stellen sein: ${VM_ID}"
(( 10#$VM_ID >= 100 && 10#$VM_ID <= 999999999 )) || die "VM-ID muss zwischen 100 und 999999999 liegen"
VM_ID="$((10#$VM_ID))"

[[ "$STORAGE" =~ ^[A-Za-z0-9._-]+$ ]] || die "Ungueltiger Storage-Name: ${STORAGE}"
[[ "$BRIDGE" =~ ^[A-Za-z0-9._-]+$ ]] || die "Ungueltiger Bridge-Name: ${BRIDGE}"
[[ "$TALOS_VERSION" == "$SUPPORTED_TALOS_VERSION" ]] \
  || die "Der Builder unterstuetzt derzeit nur Talos ${SUPPORTED_TALOS_VERSION}"
[[ "$DISK_SIZE_GB" =~ ^[0-9]+$ ]] || die "--disk-size muss eine Ganzzahl sein"
[[ "$MEMORY_MB" =~ ^[0-9]+$ ]] || die "--memory muss eine Ganzzahl sein"
[[ "$CPU_CORES" =~ ^[0-9]+$ ]] || die "--cores muss eine Ganzzahl sein"
(( 10#$DISK_SIZE_GB >= 10 )) || die "Die Talos-Template-Disk muss mindestens 10 GiB gross sein"
(( 10#$MEMORY_MB >= 2048 )) || die "Das Talos-Template braucht mindestens 2048 MiB RAM"
(( 10#$CPU_CORES >= 2 && 10#$CPU_CORES <= 256 )) || die "--cores muss zwischen 2 und 256 liegen"
DISK_SIZE_GB="$((10#$DISK_SIZE_GB))"
MEMORY_MB="$((10#$MEMORY_MB))"
CPU_CORES="$((10#$CPU_CORES))"

case "$INSTALL_DISK" in
  /dev/sda)
    DISK_INTERFACE="scsi0"
    ;;
  /dev/vda)
    DISK_INTERFACE="virtio0"
    ;;
  *)
    die "--install-disk unterstuetzt nur /dev/sda oder /dev/vda"
    ;;
esac

DEFAULT_TEMPLATE_NAME="talos-${TALOS_VERSION#v}-nocloud-template"
TEMPLATE_NAME="${TEMPLATE_NAME:-$DEFAULT_TEMPLATE_NAME}"
[[ ${#TEMPLATE_NAME} -le 63 && "$TEMPLATE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || die "Template-Name muss 1..63 sichere Zeichen enthalten"

if [[ -z "$IMAGE_URL" ]]; then
  [[ -z "$IMAGE_SHA256" ]] \
    || die "--image-sha256 darf nur zusammen mit einer ausdruecklichen --image-url gesetzt werden"
  IMAGE_URL="https://factory.talos.dev/image/${DEFAULT_SCHEMATIC_ID}/${TALOS_VERSION}/nocloud-amd64.raw.xz"
  IMAGE_SHA256="$DEFAULT_IMAGE_SHA256"
  EXPECTED_IMAGE_SIZE_BYTES="$DEFAULT_IMAGE_SIZE_BYTES"
else
  [[ -n "$IMAGE_SHA256" ]] \
    || die "Eine abweichende --image-url erfordert immer --image-sha256"
  EXPECTED_IMAGE_SIZE_BYTES=""
fi

[[ "$IMAGE_URL" =~ ^https://[^[:space:]?#]+$ ]] \
  || die "Das Talos-NoCloud-Image muss eine HTTPS-URL ohne Leerzeichen, Query oder Fragment verwenden"
[[ "${IMAGE_URL##*/}" == *.raw.xz ]] \
  || die "--image-url muss auf ein komprimiertes RAW-Image mit Endung .raw.xz zeigen"
[[ "$IMAGE_SHA256" =~ ^[A-Fa-f0-9]{64}$ ]] \
  || die "--image-sha256 muss genau 64 Hex-Zeichen enthalten"
IMAGE_SHA256="${IMAGE_SHA256,,}"

(( EUID == 0 )) || die "Dieses Skript muss als root direkt auf dem Proxmox-Host laufen"

# Diese Befehle duerfen niemals auf einem normalen Debian-Host automatisch
# installiert werden. Ihr Vorhandensein und /etc/pve identifizieren Proxmox VE.
[[ -d /etc/pve ]] || die "/etc/pve fehlt; dies ist kein aktiver Proxmox-VE-Host"
for command_name in qm pvesh pvesm pveversion qemu-img ip hostname mktemp awk grep; do
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

printf '\nGeplante Talos-Template-Erstellung:\n'
printf '  Proxmox-Version: %s\n' "$(pveversion | head -n 1)"
printf '  Ziel-Node:        %s\n' "$NODE_NAME"
printf '  VM-ID:            %s\n' "$VM_ID"
printf '  Template-Name:    %s\n' "$TEMPLATE_NAME"
printf '  Storage:          %s\n' "$STORAGE"
printf '  Bridge:           %s\n' "$BRIDGE"
printf '  Talos:            %s (NoCloud, amd64)\n' "$TALOS_VERSION"
printf '  Installationsdisk:%s (%s)\n' " $INSTALL_DISK" "$DISK_INTERFACE"
printf '  Systemdisk:       %s GiB\n' "$DISK_SIZE_GB"
printf '  Image-SHA-256:    %s\n\n' "$IMAGE_SHA256"

if [[ "$INSTALL_DISK" == "/dev/vda" ]]; then
  warn "/dev/sda mit VirtIO SCSI ist die offizielle Proxmox-Empfehlung."
  warn "Fuer dieses virtio0-Template muss im Wizard zwingend /dev/vda gewaehlt werden."
fi

if [[ "$ASSUME_YES" != true ]]; then
  [[ -t 0 ]] || die "Keine interaktive Eingabe moeglich; nach eigener Pruefung --yes verwenden"
  read -r -p "Zur Bestaetigung die VM-ID ${VM_ID} erneut eingeben: " confirmation
  [[ "$confirmation" == "$VM_ID" ]] || die "Bestaetigung stimmt nicht; Abbruch ohne Aenderung"
fi

ensure_image_dependencies() {
  local missing=()
  local command_name

  for command_name in curl sha256sum xz; do
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
    ca-certificates coreutils curl xz-utils
  hash -r

  for command_name in curl sha256sum xz; do
    require_command "$command_name"
  done
}

ensure_image_dependencies

WORK_DIR="$(mktemp -d "/var/tmp/k8s-proxmox-talos-template.${VM_ID}.XXXXXX")"
IMAGE_FILENAME="${IMAGE_URL##*/}"
COMPRESSED_IMAGE_PATH="${WORK_DIR}/${IMAGE_FILENAME}"
RAW_IMAGE_PATH="${WORK_DIR}/talos-nocloud-amd64.raw"

download_https() {
  local url="$1"
  local output="$2"
  curl --fail --show-error --silent --location \
    --retry 3 --retry-all-errors --connect-timeout 20 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --output "$output" "$url"
}

log "Lade das gepinnte Talos-NoCloud-Image herunter"
download_https "$IMAGE_URL" "$COMPRESSED_IMAGE_PATH"
[[ -s "$COMPRESSED_IMAGE_PATH" ]] || die "Das heruntergeladene Talos-Image ist leer"

if [[ -n "$EXPECTED_IMAGE_SIZE_BYTES" ]]; then
  ACTUAL_IMAGE_SIZE_BYTES="$(wc -c <"$COMPRESSED_IMAGE_PATH")"
  ACTUAL_IMAGE_SIZE_BYTES="${ACTUAL_IMAGE_SIZE_BYTES//[[:space:]]/}"
  [[ "$ACTUAL_IMAGE_SIZE_BYTES" == "$EXPECTED_IMAGE_SIZE_BYTES" ]] \
    || die "Image-Groesse stimmt nicht: erwartet ${EXPECTED_IMAGE_SIZE_BYTES}, erhalten ${ACTUAL_IMAGE_SIZE_BYTES} Bytes"
fi

ACTUAL_SHA256="$(sha256sum "$COMPRESSED_IMAGE_PATH" | awk '{ print tolower($1) }')"
[[ "$ACTUAL_SHA256" == "$IMAGE_SHA256" ]] \
  || die "SHA-256-Pruefung fehlgeschlagen: erwartet ${IMAGE_SHA256}, erhalten ${ACTUAL_SHA256}"
log "SHA-256-Pruefung erfolgreich"

log "Pruefe und entpacke das XZ-komprimierte RAW-Image"
xz --test -- "$COMPRESSED_IMAGE_PATH"
xz --decompress --stdout -- "$COMPRESSED_IMAGE_PATH" >"$RAW_IMAGE_PATH"
[[ -s "$RAW_IMAGE_PATH" ]] || die "Das entpackte Talos-RAW-Image ist leer"
qemu-img info "$RAW_IMAGE_PATH" | grep -Eq '^file format: raw$' \
  || die "Das entpackte Talos-Image ist kein RAW-Disk-Image"

# Zwischen Download/Entpacken und der ersten Proxmox-Mutation kann Zeit
# vergangen sein. Darum wird dieselbe clusterweite Kollisionspruefung wiederholt.
assert_vm_id_is_free

log "Lege die noch ausgeschaltete Talos-QEMU-VM ${VM_ID} an"
qm create "$VM_ID" \
  --name "$TEMPLATE_NAME" \
  --description "Talos ${TALOS_VERSION} NoCloud template for K8s Cluster Builder; install disk ${INSTALL_DISK}" \
  --ostype l26 \
  --bios ovmf \
  --machine q35 \
  --cpu host \
  --cores "$CPU_CORES" \
  --memory "$MEMORY_MB" \
  --balloon 0 \
  --scsihw virtio-scsi-pci \
  --net0 "virtio,bridge=${BRIDGE}" \
  --agent 0 \
  --serial0 socket \
  --vga serial0 \
  --onboot 0
VM_CREATED=true

log "Importiere die Talos-Systemdisk nach ${STORAGE}"
qm importdisk "$VM_ID" "$RAW_IMAGE_PATH" "$STORAGE"

mapfile -t UNUSED_VOLUMES < <(
  qm config "$VM_ID" | awk -F ': ' '/^unused[0-9]+: / { sub(/,.*/, "", $2); print $2 }'
)
(( ${#UNUSED_VOLUMES[@]} == 1 )) \
  || die "Erwartete genau eine importierte unused-Disk, gefunden: ${#UNUSED_VOLUMES[@]}"
IMPORTED_VOLUME="${UNUSED_VOLUMES[0]}"

log "Binde Systemdisk, EFI-Disk und NoCloud-Konfigurationsdisk ein"
qm set "$VM_ID" "--${DISK_INTERFACE}" "${IMPORTED_VOLUME},discard=on,ssd=1"
qm resize "$VM_ID" "$DISK_INTERFACE" "${DISK_SIZE_GB}G"
qm set "$VM_ID" --efidisk0 "${STORAGE}:0,efitype=4m,pre-enrolled-keys=0"
qm set "$VM_ID" --ide2 "${STORAGE}:cloudinit"
qm set "$VM_ID" --boot "order=${DISK_INTERFACE}"

# Vor der irreversiblen Umwandlung in ein Template werden genau die
# Eigenschaften geprueft, auf die Terraform und der Talos-Worker vertrauen.
VM_CONFIG="$(qm config "$VM_ID")"
grep -Eq "^${DISK_INTERFACE}: " <<<"$VM_CONFIG" || die "Systemdisk ${DISK_INTERFACE} fehlt"
grep -Eq '^ide2: .*cloudinit' <<<"$VM_CONFIG" || die "NoCloud-/Cloud-Init-Disk fehlt"
grep -Eq '^efidisk0: ' <<<"$VM_CONFIG" || die "EFI-Disk fehlt"
grep -Eq '^efidisk0: .*efitype=4m' <<<"$VM_CONFIG" || die "EFI-Disk verwendet nicht efitype=4m"
if grep -Eq '^efidisk0: .*pre-enrolled-keys=1([,[:space:]]|$)' <<<"$VM_CONFIG"; then
  die "Secure-Boot-Standardschluessel sind unerwartet aktiviert"
fi
grep -Eq '^bios: ovmf$' <<<"$VM_CONFIG" || die "BIOS ist nicht auf OVMF/UEFI gesetzt"
grep -Eq '^machine: q35([,[:space:]]|$)' <<<"$VM_CONFIG" || die "Maschinentyp ist nicht q35"
grep -Eq '^scsihw: virtio-scsi-pci$' <<<"$VM_CONFIG" || die "VirtIO-SCSI-Controller stimmt nicht"
grep -Eq '^balloon: 0$' <<<"$VM_CONFIG" || die "Memory Ballooning ist nicht deaktiviert"
if grep -Eq '^agent: (1|enabled=1)(,|$)' <<<"$VM_CONFIG"; then
  die "QEMU Guest Agent ist fuer das unveraenderte Talos-Image aktiviert"
fi
grep -Eq "^net0: .*bridge=${BRIDGE}([,[:space:]]|$)" <<<"$VM_CONFIG" || die "Netzwerk-Bridge stimmt nicht"
grep -Eq "^boot: order=${DISK_INTERFACE}([;[:space:]]|$)" <<<"$VM_CONFIG" || die "Boot-Reihenfolge stimmt nicht"

log "Wandle VM ${VM_ID} in ein unveraenderliches Proxmox-Template um"
qm template "$VM_ID"

FINAL_CONFIG="$(qm config "$VM_ID")"
grep -Eq '^template: 1' <<<"$FINAL_CONFIG" || die "Proxmox meldet VM ${VM_ID} nicht als Template"
FINISHED=true

printf '\nTalos-Template erfolgreich erstellt.\n'
printf '  Node:               %s\n' "$NODE_NAME"
printf '  VM-ID:              %s\n' "$VM_ID"
printf '  Name:               %s\n' "$TEMPLATE_NAME"
printf '  Storage:            %s\n' "$STORAGE"
printf '  Bridge:             %s\n' "$BRIDGE"
printf '  Talos:              %s\n' "$TALOS_VERSION"
printf '  Installationsdisk:  %s (%s)\n\n' "$INSTALL_DISK" "$DISK_INTERFACE"
printf 'Naechster Schritt: In der Weboberflaeche dieses Template als Talos-Template\n'
printf 'mit VM-ID %s auswaehlen und als Talos-Installationsdisk exakt %s setzen.\n' "$VM_ID" "$INSTALL_DISK"
printf 'Fuer die Load Balancer zusaetzlich ein separates Ubuntu-Template auswaehlen.\n'
