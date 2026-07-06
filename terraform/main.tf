resource "proxmox_virtual_environment_vm" "k8s" {
  for_each = var.nodes

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
    dedicated = each.value.memory_mb
  }

  disk {
    datastore_id = var.datastore_id
    interface    = "scsi0"
    size         = each.value.disk_gb
  }

  network_device {
    bridge  = var.network_bridge
    vlan_id = var.vlan_id
  }

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = "${each.value.ip}/${var.subnet_prefix}"
        gateway = var.gateway
      }
    }

    dns {
      servers = var.dns_servers
    }

    user_account {
      username = var.ssh_user
      keys     = [var.ssh_public_key]
    }
  }

  operating_system {
    type = "l26"
  }

  started = true
  tags    = ["kubernetes", each.value.role]
}
