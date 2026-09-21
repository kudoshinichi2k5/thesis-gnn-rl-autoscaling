locals {
  nodes_by_name = { for node in var.nodes : node.name => node }
}

# Each node boots from a newly-created Cinder volume cloned from the Ubuntu image.
resource "openstack_compute_instance_v2" "node" {
  for_each = local.nodes_by_name

  name            = each.value.name
  flavor_id       = each.value.flavor_id
  key_pair        = openstack_compute_keypair_v2.cluster.name
  security_groups = [openstack_networking_secgroup_v2.k8s.name]

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

  network { uuid = data.openstack_networking_network_v2.public.id }
}
