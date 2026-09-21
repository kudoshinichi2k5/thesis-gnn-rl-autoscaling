resource "openstack_networking_floatingip_v2" "node" {
  for_each = var.node_ports

  pool = var.external_network_name
}

resource "openstack_networking_floatingip_associate_v2" "node" {
  for_each = var.node_ports

  floating_ip = openstack_networking_floatingip_v2.node[each.key].address
  port_id     = each.value.port_id
}
