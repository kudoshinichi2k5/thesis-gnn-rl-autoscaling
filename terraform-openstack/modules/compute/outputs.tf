output "nodes" {
  description = "Nodes with their roles, private fixed IPv4 addresses, and floating IPv4 addresses."
  value = {
    for name, instance in openstack_compute_instance_v2.node : name => {
      fixed_ip    = try(instance.network[0].fixed_ip_v4, null)
      floating_ip = openstack_networking_floatingip_v2.node[name].address
      role        = instance.metadata.role
    }
  }
}
