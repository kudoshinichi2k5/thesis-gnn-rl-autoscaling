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

  # Flavor disk=0, so boot from a dedicated Cinder volume.
  block_device {
    uuid                  = var.image_id
    source_type           = "image"
    destination_type      = "volume"
    volume_size           = each.value.disk_size_gb
    boot_index            = 0
    delete_on_termination = true
  }

  # Attach the instance directly to the existing Public_Net.
  network {
    uuid = var.network_id
  }
}