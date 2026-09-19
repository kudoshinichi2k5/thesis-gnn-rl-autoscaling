output "nodes" {
  description = "K3s nodes with their IP addresses and roles."

  value = {
    for name, instance in openstack_compute_instance_v2.node :
    name => {
      role = instance.metadata.role

      ip = var.assign_floating_ip
        ? openstack_networking_floatingip_v2.node[name].address
        : try(
            [
              for addr in instance.access_ip_v4 : addr
              if addr != ""
            ][0],
            null
          )
    }
  }
}