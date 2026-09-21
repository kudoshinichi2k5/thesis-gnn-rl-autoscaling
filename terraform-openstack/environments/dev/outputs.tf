output "node_fixed_ips" {
  description = "Fixed IPv4 addresses on the external Public_Net; no floating IPs are created."
  value = {
    for name, instance in openstack_compute_instance_v2.node : name => {
      fixed_ip = try(instance.network[0].fixed_ip_v4, null)
      role     = instance.metadata.role
    }
  }
}

output "network" {
  description = "Existing external network used by the nodes."
  value = {
    id   = data.openstack_networking_network_v2.public.id
    name = data.openstack_networking_network_v2.public.name
  }
}
