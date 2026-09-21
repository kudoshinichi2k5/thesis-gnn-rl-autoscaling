# Read the provider-managed external network used for router egress and floating IPs.
data "openstack_networking_network_v2" "external" {
  name = var.external_network_name

  lifecycle {
    postcondition {
      condition     = self.id == var.external_network_id
      error_message = "${var.external_network_name} did not resolve to the expected network ID."
    }
  }
}

resource "openstack_networking_network_v2" "private" {
  name           = var.private_network_name
  admin_state_up = true
}

resource "openstack_networking_subnet_v2" "private" {
  name            = var.private_subnet_name
  network_id      = openstack_networking_network_v2.private.id
  cidr            = var.private_subnet_cidr
  ip_version      = 4
  enable_dhcp     = true
  dns_nameservers = var.dns_nameservers
}

resource "openstack_networking_router_v2" "this" {
  name                = var.router_name
  admin_state_up      = true
  external_network_id = data.openstack_networking_network_v2.external.id
}

resource "openstack_networking_router_interface_v2" "private" {
  router_id = openstack_networking_router_v2.this.id
  subnet_id = openstack_networking_subnet_v2.private.id
}
