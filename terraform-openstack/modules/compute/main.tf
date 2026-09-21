locals {
  nodes_by_name = { for node in var.nodes : node.name => node }
}

# Each instance boots from a new Cinder volume cloned from the supplied image.
resource "openstack_compute_instance_v2" "node" {
  for_each = local.nodes_by_name

  name            = each.value.name
  flavor_id       = each.value.flavor_id
  key_pair        = var.keypair_name
  security_groups = [var.security_group_name]

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
    uuid = var.network_id
  }
}

resource "openstack_networking_floatingip_v2" "node" {
  for_each = local.nodes_by_name

  pool = var.external_network_name
}

resource "openstack_compute_floatingip_associate_v2" "node" {
  for_each = local.nodes_by_name

  floating_ip = openstack_networking_floatingip_v2.node[each.key].address
  instance_id = openstack_compute_instance_v2.node[each.key].id
  fixed_ip    = openstack_compute_instance_v2.node[each.key].network[0].fixed_ip_v4
}
