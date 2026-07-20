variable "proxmox_endpoint" {
  type        = string
  description = "Proxmox API endpoint"
  validation {
    condition     = can(regex("^https://", var.proxmox_endpoint))
    error_message = "Der Proxmox-Endpoint muss HTTPS verwenden."
  }
}
variable "proxmox_insecure" {
  type    = bool
  default = false
}

variable "cluster_type" {
  type    = string
  default = "kubeadm"
  validation {
    condition     = contains(["kubeadm", "talos"], var.cluster_type)
    error_message = "cluster_type muss kubeadm oder talos sein."
  }
}

variable "proxmox_node" {
  type = string
}

variable "template_vm_id" {
  type = number
}

variable "load_balancer_template_vm_id" {
  type        = number
  default     = null
  nullable    = true
  description = "Ubuntu/Linux template used by load balancers"
}

variable "talos_install_disk" {
  type    = string
  default = "/dev/sda"
  validation {
    condition     = contains(["/dev/sda", "/dev/vda"], var.talos_install_disk)
    error_message = "talos_install_disk muss /dev/sda oder /dev/vda sein."
  }
}

variable "datastore_id" {
  type = string
}

variable "network_bridge" {
  type = string
}

variable "vlan_id" {
  type     = number
  default  = null
  nullable = true
}

variable "gateway" {
  type = string
}

variable "subnet_prefix" {
  type = number
  validation {
    condition     = var.subnet_prefix >= 8 && var.subnet_prefix <= 30
    error_message = "Das IPv4-Präfix muss zwischen /8 und /30 liegen."
  }
}

variable "dns_servers" {
  type = list(string)
}

variable "ssh_user" {
  type = string
}

variable "ssh_public_key" {
  type = string
}

variable "nodes" {
  type = map(object({
    name      = string
    vm_name   = string
    role      = string
    vm_id     = number
    ip        = string
    cores     = number
    memory_mb = number
    disk_gb   = number
  }))
  validation {
    condition     = length(var.nodes) == length(distinct([for node in values(var.nodes) : node.vm_id]))
    error_message = "VM-IDs müssen eindeutig sein."
  }
  validation {
    condition     = length(var.nodes) == length(distinct([for node in values(var.nodes) : node.ip]))
    error_message = "Node-IP-Adressen müssen eindeutig sein."
  }
}
