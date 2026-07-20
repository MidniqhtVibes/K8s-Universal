# Proxmox Kubernetes Cluster Builder

Web-based builder for HA Kubernetes clusters on Proxmox. The application generates Terraform and Ansible configurations from a wizard, creates the Proxmox VMs, and installs Kubernetes, Calico, and optionally Traefik.

The application is provided as prebuilt container images via the GitHub Container Registry (GHCR). For normal operation, the images therefore do not need to be built locally. Installation-specific values such as passwords, secrets, ports, and runtime settings are configured through a dedicated `.env` file.

## Features

- Proxmox cluster creation with load balancers, control planes, and workers
- Terraform Plan, Apply, Destroy Plan, and Destroy through the web interface
- Provisioning via Terraform, Ansible, and kubeadm
- HAProxy and Keepalived for the Kubernetes API VIP
- Calico as the CNI and optionally Traefik as the ingress controller
- Optional private container registry configuration for every Kubernetes node
- Credential management for the Proxmox API and SSH keys
- Kubernetes web console for `kubectl`
- Application templates and manifest bundles
- Job recovery after worker restarts
- Prebuilt web and worker images via GHCR
- Version-based updates via `BUILDER_VERSION`

## Requirements

For operation:

- Docker and Docker Compose
- A reachable Proxmox host or Proxmox cluster
- A cloud-init-capable QEMU VM template on the selected Proxmox node
- A Proxmox API token with permissions to create, read, and delete VMs
- Free IP addresses and VM IDs for load balancers, control planes, and workers
- Network access from the builder/worker to Proxmox
- Internet access for the created VMs
- Access to `ghcr.io` if the images are pulled directly from the GitHub Container Registry

Additionally, for local development:

- Git
- A checkout of the complete repository

## Preparing the Proxmox Template

The repository includes a one-time host setup tool at `proxmox/create-template.sh`. It is executed directly as `root` on the exact Proxmox node that will later be selected in the wizard.

The script does not run inside the builder container and is not started automatically by either the web application or Ansible.

It downloads an official Ubuntu cloud image over HTTPS, verifies its SHA-256 checksum, installs Cloud-Init, SSH, and the QEMU Guest Agent, and creates a QEMU template from it.

The VM ID has no fixed default value and must be free across the entire Proxmox cluster. Existing VMs are not overwritten.

First copy the script from the repository to the target node:

```bash
scp proxmox/create-template.sh root@pve-node:/root/create-template.sh
ssh root@pve-node
```

Then run it on the Proxmox host. `9100` is only an example ID here:

```bash
bash /root/create-template.sh \
  --vm-id 9100 \
  --storage local-lvm \
  --bridge vmbr0 \
  --ubuntu-release noble \
  --install-dependencies
```

Without `--install-dependencies`, the script does not modify host packages and exits with an explanation if required tools are missing.

To display all options:

```bash
bash /root/create-template.sh --help
```

After successful completion, the template can later be selected in the builder.

## Setup with Prebuilt Container Images

For normal operation, the images are no longer built locally.

Required files:

```text
compose.yaml
.env
```

Use `.env.example` as the configuration template.

### 1. Create `.env`

On Linux:

```bash
cp .env.example .env
chmod 600 .env
```

On PowerShell:

```powershell
Copy-Item .env.example .env
```

Then adjust the values in `.env`.

Example:

```env
COMPOSE_PROJECT_NAME=k8s-universal

BUILDER_VERSION=1.0.0

BUILDER_WEB_IMAGE=ghcr.io/midniqhtvibes/k8s-universal-web
BUILDER_WORKER_IMAGE=ghcr.io/midniqhtvibes/k8s-universal-worker

POSTGRES_PASSWORD=replace-with-a-long-random-url-safe-password
MASTER_KEY=replace-with-at-least-32-random-characters
SESSION_SECRET=replace-with-an-independent-long-random-value
INITIAL_ADMIN_PASSWORD=replace-on-first-start

BUILDER_BIND_ADDRESS=127.0.0.1
BUILDER_PORT=8000
SESSION_HTTPS_ONLY=false

TERRAFORM_PARALLELISM=2
ANSIBLE_FORKS=4

STALE_JOB_TIMEOUT_MINUTES=60
JOB_RETENTION_KEEP=100
MANIFEST_REVISION_RETENTION_KEEP=30
```

`MASTER_KEY` must remain unchanged permanently. If the value is changed or lost, stored encrypted credentials can no longer be decrypted.

A URL-safe value is currently recommended for `POSTGRES_PASSWORD`, because the password is used inside `DATABASE_URL`.

Zum Example:

```bash
openssl rand -hex 32
```

### 2. Select Container Images

By default, the images are pulled from GHCR:

```text
ghcr.io/midniqhtvibes/k8s-universal-web
ghcr.io/midniqhtvibes/k8s-universal-worker
```

The desired version is set via `BUILDER_VERSION`.

For a fixed release version:

```env
BUILDER_VERSION=1.0.0
```

For testing the current `main` branch state:

```env
BUILDER_VERSION=edge
```

`latest` points to the most recently published release.

A fixed version number is recommended for production installations.

### 3. Pull the Images

```bash
docker compose pull
```

If the GHCR packages are private, the Docker host must log in first:

```bash
echo "$CR_PAT" | docker login ghcr.io -u MidniqhtVibes --password-stdin
```

No login is required for public images.

### 4. Start the Stack

```bash
docker compose up -d
```

Check the status:

```bash
docker compose ps
```

Check the logs:

```bash
docker compose logs -f web worker
```

By default, the web interface is available at:

```text
http://127.0.0.1:8000
```

The initial login uses the username `admin` and the value from `INITIAL_ADMIN_PASSWORD`.

## Optional Private Container Registry

The cluster wizard can configure one private registry endpoint for containerd on
all control-plane and worker nodes. Enter the endpoint as `host:port`, without a
URL scheme or path, for example:

```text
10.200.50.240:5000
```

HTTPS is used by default. Enable the separate HTTP option only for a registry in
a trusted internal lab or test network; production registries should use HTTPS.
The setting applies only to the endpoint entered in the wizard and does not
disable TLS verification globally.

After the cluster has been provisioned, Kubernetes workloads can use images from
that endpoint directly:

```yaml
containers:
  - name: azubiorga
    image: 10.200.50.240:5000/azubiorga:1.0.0
```

## Persistent Data

The stack uses two Docker volumes:

```text
postgres-data
cluster-data
```

`postgres-data` contains the PostgreSQL database.

`cluster-data` contains installation-specific builder working data, including cluster workspaces and generated files.

The volumes remain intact during a normal container update.

They are only removed when Docker volumes are explicitly deleted.

## Update

Updating to a new version does not require a local Docker build.

Change the desired version in `.env`:

```env
BUILDER_VERSION=1.1.0
```

Then run:

```bash
docker compose pull
docker compose up -d
```

Database migrations run automatically when the `web` container starts.

Existing data in `postgres-data` and `cluster-data` remains intact.

A backup of the persistent data should still be created before major version upgrades.

## Rollback

For a simple container rollback, set an older image version again:

```env
BUILDER_VERSION=1.0.0
```

Then run:

```bash
docker compose pull
docker compose up -d
```

However, an image rollback does not automatically guarantee that previously executed database migrations are backward compatible.

## Local Development

For local development, `compose.yaml` and `compose.dev.yaml` are used together.

On Linux:

```bash
docker compose \
  -f compose.yaml \
  -f compose.dev.yaml \
  up -d --build
```

On PowerShell:

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

In the development setup, the web and worker images are built locally from the Dockerfile, and the repository is additionally mounted to `/iac`.

This allows direct work with the local Terraform, Ansible, and application files.

Local tests can be run through the test profile:

```powershell
docker compose -f compose.yaml -f compose.dev.yaml --profile test run --rm test
```

## Notes

- For production installations, `BUILDER_VERSION` should be pinned to a fixed version.
- `MASTER_KEY` must be stored securely and kept unchanged.


