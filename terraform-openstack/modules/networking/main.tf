# Reference the existing Public_Net; Terraform does not create a new network.
data "openstack_networking_network_v2" "public" {
  name = var.network_name
}