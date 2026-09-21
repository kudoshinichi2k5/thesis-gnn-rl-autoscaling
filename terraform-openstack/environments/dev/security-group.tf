# Look up the existing external network; no network, subnet, or router is created.
data "openstack_networking_network_v2" "public" {
  name = var.network_name

  lifecycle {
    postcondition {
      condition     = self.id == var.network_id
      error_message = "${var.network_name} did not resolve to the expected network ID."
    }
  }
}

# Security group shared by all nodes in the research cluster.
resource "openstack_networking_secgroup_v2" "k8s" {
  name                 = var.security_group_name
  description          = "Security group for the non-HA K8s research cluster"
  delete_default_rules = true
}

# Public management and Kubernetes service entry points.
resource "openstack_networking_secgroup_rule_v2" "ssh" {
  direction = "ingress"
  ethertype = "IPv4"
  protocol  = "tcp"

  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

resource "openstack_networking_secgroup_rule_v2" "kubernetes_api" {
  direction = "ingress"
  ethertype = "IPv4"
  protocol  = "tcp"

  port_range_min    = 6443
  port_range_max    = 6443
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

resource "openstack_networking_secgroup_rule_v2" "nodeport" {
  direction = "ingress"
  ethertype = "IPv4"
  protocol  = "tcp"

  port_range_min    = 30000
  port_range_max    = 32767
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

# Cluster-only traffic is restricted by the same security group.
resource "openstack_networking_secgroup_rule_v2" "flannel_vxlan" {
  direction = "ingress"
  ethertype = "IPv4"
  protocol  = "udp"

  port_range_min    = 8472
  port_range_max    = 8472
  remote_group_id   = openstack_networking_secgroup_v2.k8s.id
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

resource "openstack_networking_secgroup_rule_v2" "icmp_internal" {
  direction = "ingress"
  ethertype = "IPv4"
  protocol  = "icmp"

  remote_group_id   = openstack_networking_secgroup_v2.k8s.id
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

# Permit all IPv4 outbound traffic from cluster nodes.
resource "openstack_networking_secgroup_rule_v2" "egress_all" {
  direction         = "egress"
  ethertype         = "IPv4"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}
