output "addresses" {
  description = "Floating IPv4 addresses keyed by node name."
  value = {
    for name, floating_ip in openstack_networking_floatingip_v2.node : name => floating_ip.address
  }
}
