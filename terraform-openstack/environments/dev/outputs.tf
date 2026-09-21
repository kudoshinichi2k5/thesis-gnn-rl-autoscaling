output "node_fixed_ips" {
  description = "Fixed IPv4 addresses on the external Public_Net; no floating IPs are created."
  value       = module.compute.nodes
}

output "network" {
  description = "Existing external network used by the nodes."
  value = {
    id   = module.networking.network_id
    name = module.networking.network_name
  }
}
