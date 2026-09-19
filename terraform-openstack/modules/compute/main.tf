locals {
  nodes = {
    for node in var.nodes :
    node.name => node
  }
}

resource "openstack_compute_instance_v2" "node" {
  for_each = local.nodes

  name            = each.value.name
  flavor_name     = each.value.flavor
  key_pair        = var.key_pair
  security_groups = [var.security_group_name]

  metadata = {
    role = each.value.role
  }

  # Boot each instance from a dedicated Cinder volume.
  # The flavor has disk=0, so no ephemeral root disk is used.
  block_device {
    uuid                  = var.image_id
    source_type           = "image"
    destination_type      = "volume"
    volume_size           = each.value.disk_size_gb
    boot_index            = 0
    delete_on_termination = true
  }

  network {
    uuid = var.network_id
  }
}

resource "openstack_networking_floatingip_v2" "node" {
  for_each = var.assign_floating_ip ? local.nodes : {}

  pool = "Public_Net"
}

resource "openstack_compute_floatingip_associate_v2" "node" {
  for_each = var.assign_floating_ip ? local.nodes : {}

  floating_ip = openstack_networking_floatingip_v2.node[each.key].address
  instance_id = openstack_compute_instance_v2.node[each.key].id
}