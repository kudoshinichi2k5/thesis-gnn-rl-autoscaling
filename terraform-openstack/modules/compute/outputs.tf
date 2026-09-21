output "nodes" {
  description = "Nodes with their roles and private fixed IPv4 addresses."
  value = {
    for name, instance in openstack_compute_instance_v2.node : name => {
      fixed_ip = try(instance.network[0].fixed_ip_v4, null)
      role     = instance.metadata.role
    }
  }
}

output "node_ports" {
  description = "Managed Neutron ports keyed by node name, for use by dependent networking modules."
  value = {
    for name, port in openstack_networking_port_v2.node : name => {
      port_id = port.id
    }
  }
}
