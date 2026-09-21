output "node_fixed_ips" {
  description = "Private fixed IPv4 and externally reachable floating IPv4 addresses for each node."
  value       = module.compute.nodes
}

output "network" {
  description = "External and project-private network resources used by the cluster."
  value = {
    external = {
      id   = module.networking.external_network_id
      name = module.networking.external_network_name
    }
    private = {
      id        = module.networking.private_network_id
      subnet_id = module.networking.private_subnet_id
    }
    router_id = module.networking.router_id
  }
}
