output "name" {
  description = "Name of the imported OpenStack keypair."
  value       = openstack_compute_keypair_v2.this.name
}
