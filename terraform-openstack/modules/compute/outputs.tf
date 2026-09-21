output "nodes" {
  description = "Nodes with their roles and fixed IPv4 addresses."
  value = {
    for name, instance in openstack_compute_instance_v2.node : name => {
      fixed_ip = try(instance.network[0].fixed_ip_v4, null)
      role     = instance.metadata.role
    }
  }
}
