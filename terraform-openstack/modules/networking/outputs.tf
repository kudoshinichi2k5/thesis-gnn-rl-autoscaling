output "network_id" {
  description = "ID of the existing OpenStack network."
  value       = data.openstack_networking_network_v2.public.id
}