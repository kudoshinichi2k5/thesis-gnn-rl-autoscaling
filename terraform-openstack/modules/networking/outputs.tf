output "external_network_id" {
  description = "ID of the provider-managed external network."
  value       = data.openstack_networking_network_v2.external.id
}

output "external_network_name" {
  description = "Name of the provider-managed external network."
  value       = data.openstack_networking_network_v2.external.name
}

output "private_network_id" {
  description = "ID of the project-private network used by instances."
  value       = openstack_networking_network_v2.private.id
}

output "private_subnet_id" {
  description = "ID of the project-private subnet."
  value       = openstack_networking_subnet_v2.private.id
}

output "router_id" {
  description = "ID of the router providing private-subnet egress."
  value       = openstack_networking_router_v2.this.id
}
