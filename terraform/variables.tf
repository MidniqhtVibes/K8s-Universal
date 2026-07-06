variable "proxmox_endpoint" {
  type        = string
  description = "Proxmox API endpoint, e.g. https://10.200.50.134:8006/"
}

variable "proxmox_api_token" {
  type        = string
  sensitive   = true
  description = "Proxmox API token"
}

variable "proxmox_insecure" {
  type    = bool
  default = true
}

variable "proxmox_node" {
  type        = string
  description = "Name des Proxmox Nodes, z. B. pve"
}

variable "template_vm_id" {
  type        = number
  description = "VM-ID des Ubuntu Cloud-Init Templates"
}

variable "datastore_id" {
  type        = string
  description = "Storage, z. B. local-lvm"
}

variable "network_bridge" {
  type    = string
  default = "vmbr0"
}

variable "gateway" {
  type    = string
  default = "10.200.50.1"
}

variable "dns_servers" {
  type    = list(string)
  default = ["10.200.50.1", "1.1.1.1"]
}

variable "ssh_user" {
  type    = string
  default = "ubuntu"
}

variable "ssh_public_key_path" {
  type    = string
  default = "~/.ssh/id_rsa.pub"
}