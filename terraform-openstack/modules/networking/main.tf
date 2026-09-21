# Read the pre-existing external network; this module creates no network resources.
data "openstack_networking_network_v2" "this" {
  name = var.network_name

  lifecycle {
    postcondition {
      condition     = self.id == var.network_id
      error_message = "${var.network_name} did not resolve to the expected network ID."
    }
  }
}
