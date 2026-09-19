resource "openstack_networking_secgroup_v2" "k8s" {
  name        = var.security_group_name
  description = "Security group for the k3s research cluster."
}

# SSH
resource "openstack_networking_secgroup_rule_v2" "ssh" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

# Kubernetes API
resource "openstack_networking_secgroup_rule_v2" "kubernetes_api" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 6443
  port_range_max    = 6443
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

# Kubernetes NodePort range
resource "openstack_networking_secgroup_rule_v2" "nodeport" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 30000
  port_range_max    = 32767
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

# Flannel VXLAN between cluster nodes
resource "openstack_networking_secgroup_rule_v2" "flannel_vxlan" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "udp"
  port_range_min    = 8472
  port_range_max    = 8472
  remote_ip_prefix  = var.network_cidr
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

# ICMP between cluster nodes
resource "openstack_networking_secgroup_rule_v2" "icmp_internal" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "icmp"
  remote_ip_prefix  = var.network_cidr
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}

# Allow all outbound traffic
resource "openstack_networking_secgroup_rule_v2" "egress_all" {
  direction         = "egress"
  ethertype         = "IPv4"
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.k8s.id
}