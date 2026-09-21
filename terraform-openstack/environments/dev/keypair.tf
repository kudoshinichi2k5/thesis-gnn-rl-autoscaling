# Import the existing local public key into OpenStack for SSH access.
resource "openstack_compute_keypair_v2" "cluster" {
  name       = var.keypair_name
  public_key = file(pathexpand(var.public_key_path))
}
