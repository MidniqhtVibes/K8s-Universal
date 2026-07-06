locals {
  nodes = {
    "lb-01" = {
      vm_id    = 301
      ip       = "10.200.50.145"
      cores    = 1
      memory   = 1024
      disk_gb  = 20
      role     = "loadbalancer"
    }

    "lb-02" = {
      vm_id    = 302
      ip       = "10.200.50.146"
      cores    = 1
      memory   = 1024
      disk_gb  = 20
      role     = "loadbalancer"
    }

    "control-01" = {
      vm_id    = 311
      ip       = "10.200.50.151"
      cores    = 2
      memory   = 4096
      disk_gb  = 40
      role     = "control_plane"
    }

    "control-02" = {
      vm_id    = 312
      ip       = "10.200.50.152"
      cores    = 2
      memory   = 4096
      disk_gb  = 40
      role     = "control_plane"
    }

    "control-03" = {
      vm_id    = 313
      ip       = "10.200.50.153"
      cores    = 2
      memory   = 4096
      disk_gb  = 40
      role     = "control_plane"
    }

    "worker-01" = {
      vm_id    = 321
      ip       = "10.200.50.161"
      cores    = 2
      memory   = 4096
      disk_gb  = 50
      role     = "worker"
    }

    "worker-02" = {
      vm_id    = 322
      ip       = "10.200.50.162"
      cores    = 2
      memory   = 4096
      disk_gb  = 50
      role     = "worker"
    }
  }
}

resource "proxmox_virtual_environment_vm" "k8s" {
  for_each = local.nodes

  name      = each.key
  node_name = var.proxmox_node
  vm_id     = each.value.vm_id

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  agent {
    enabled = true
  }

  cpu {
    cores = each.value.cores
    type  = "host"
  }

  memory {
    dedicated = each.value.memory
  }

  disk {
    datastore_id = var.datastore_id
    interface    = "scsi0"
    size         = each.value.disk_gb
  }

  network_device {
    bridge = var.network_bridge
  }

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = "${each.value.ip}/24"
        gateway = var.gateway
      }
    }

    dns {
      servers = var.dns_servers
    }

    user_account {
      username = var.ssh_user
      keys     = [trimspace(file(pathexpand(var.ssh_public_key_path)))]
    }
  }

  operating_system {
    type = "l26"
  }

  started = true

  tags = [
    "kubernetes",
    each.value.role
  ]
}