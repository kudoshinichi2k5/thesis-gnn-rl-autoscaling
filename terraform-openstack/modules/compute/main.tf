locals {
  nodes_by_name = { for node in var.nodes : node.name => node }
}

# Each node receives a managed Neutron port on the private network. Managing the
# port explicitly makes it safe to associate its floating IP through Neutron.
resource "openstack_networking_port_v2" "node" {
  for_each = local.nodes_by_name

  name               = "${each.value.name}-port"
  network_id         = var.network_id
  admin_state_up     = true
  security_group_ids = [var.security_group_id]
}

# Each instance boots from a new Cinder volume cloned from the supplied image.
resource "openstack_compute_instance_v2" "node" {
  for_each = local.nodes_by_name

  name            = each.value.name
  flavor_id       = each.value.flavor_id
  key_pair        = var.keypair_name
  metadata = {
    role        = each.value.role
    flavor_name = each.value.flavor_name
  }

  block_device {
    uuid                  = var.image_id
    source_type           = "image"
    destination_type      = "volume"
    volume_size           = each.value.volume_size_gb
    boot_index            = 0
    delete_on_termination = true
  }

  network {
    port = openstack_networking_port_v2.node[each.key].id
  }
}

resource "openstack_networking_floatingip_v2" "node" {
  for_each = local.nodes_by_name

  pool = var.external_network_name
}

resource "openstack_networking_floatingip_associate_v2" "node" {
  for_each = local.nodes_by_name

  floating_ip = openstack_networking_floatingip_v2.node[each.key].address
  port_id     = openstack_networking_port_v2.node[each.key].id
}
