output "security_group_id" {
  description = "ID of the Kubernetes security group."
  value       = openstack_networking_secgroup_v2.k8s.id
}

output "security_group_name" {
  description = "Name of the Kubernetes security group."
  value       = openstack_networking_secgroup_v2.k8s.name
}