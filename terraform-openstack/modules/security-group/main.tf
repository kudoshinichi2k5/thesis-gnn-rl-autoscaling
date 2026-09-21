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

moved {
  from = openstack_networking_secgroup_rule_v2.ssh
  to   = openstack_networking_secgroup_rule_v2.this["ssh"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.kubernetes_api
  to   = openstack_networking_secgroup_rule_v2.this["kubernetes-api"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.nodeport
  to   = openstack_networking_secgroup_rule_v2.this["nodeport"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.flannel_vxlan
  to   = openstack_networking_secgroup_rule_v2.this["flannel-vxlan"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.icmp_internal
  to   = openstack_networking_secgroup_rule_v2.this["icmp-internal"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.egress_all
  to   = openstack_networking_secgroup_rule_v2.this["egress-all"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.kubelet_metrics
  to   = openstack_networking_secgroup_rule_v2.this["kubelet-metrics"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.grafana_ui["0.0.0.0/0"]
  to   = openstack_networking_secgroup_rule_v2.this["grafana-ui"]
}

moved {
  from = openstack_networking_secgroup_rule_v2.prometheus_ui["0.0.0.0/0"]
  to   = openstack_networking_secgroup_rule_v2.this["prometheus-ui"]
}
