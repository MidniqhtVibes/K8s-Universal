resource "proxmox_virtual_environment_vm" "k8s" {
  for_each = var.nodes

  name      = each.value.vm_name
  node_name = var.proxmox_node
  vm_id     = each.value.vm_id

  clone {
    vm_id = each.value.role == "loadbalancer" ? coalesce(var.load_balancer_template_vm_id, var.template_vm_id) : var.template_vm_id
    full  = true
  }

  agent {
    # A standard Talos image has no QEMU guest agent. Enabling the Proxmox
    # integration without the matching system extension causes noisy errors.
    enabled = var.cluster_type == "kubeadm" || each.value.role == "loadbalancer"
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
    interface    = var.cluster_type == "talos" && each.value.role != "loadbalancer" && var.talos_install_disk == "/dev/vda" ? "virtio0" : "scsi0"
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

    dynamic "user_account" {
      for_each = var.cluster_type == "kubeadm" || each.value.role == "loadbalancer" ? [1] : []
      content {
        username = var.ssh_user
        keys     = [var.ssh_public_key]
      }
    }
  }

  operating_system {
    type = "l26"
  }

  started = true
  tags    = ["kubernetes", each.value.role]
}
