output "loadbalancer_ips" {
  value = [
    for name, node in local.nodes : node.ip
    if node.role == "loadbalancer"
  ]
}

output "control_plane_ips" {
  value = [
    for name, node in local.nodes : node.ip
    if node.role == "control_plane"
  ]
}

output "worker_ips" {
  value = [
    for name, node in local.nodes : node.ip
    if node.role == "worker"
  ]
}