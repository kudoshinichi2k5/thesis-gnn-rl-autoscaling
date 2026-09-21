output "network_id" {
  description = "ID of the existing OpenStack network."
  value       = data.openstack_networking_network_v2.this.id
}

output "network_name" {
  description = "Name of the existing OpenStack network."
  value       = data.openstack_networking_network_v2.this.name
}
