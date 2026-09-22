# Security-group behavior is supplied by the environment inventory.
resource "openstack_networking_secgroup_v2" "this" {
  name                 = var.security_group.name
  description          = var.security_group.description
  delete_default_rules = var.security_group.delete_default_rules
}

resource "openstack_networking_secgroup_rule_v2" "this" {
  for_each = { for rule in var.security_group.rules : rule.name => rule }

  direction         = each.value.direction
  ethertype         = each.value.ethertype
  protocol          = each.value.protocol
  port_range_min    = each.value.port_range_min
  port_range_max    = each.value.port_range_max
  remote_ip_prefix  = each.value.remote_ip_prefix
  remote_group_id   = each.value.remote_group ? openstack_networking_secgroup_v2.this.id : null
  security_group_id = openstack_networking_secgroup_v2.this.id
}
