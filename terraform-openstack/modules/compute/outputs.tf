output "nodes" {
  description = "Created nodes with their roles and IPv4 addresses."

  value = {
    for name, instance in openstack_compute_instance_v2.node :
    name => {
      role = instance.metadata.role
      ip   = instance.access_ip_v4
    }
  }
}