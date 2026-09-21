# Import the local public key into OpenStack; no private key is stored in state.
resource "openstack_compute_keypair_v2" "this" {
  name       = var.keypair_name
  public_key = file(pathexpand(var.public_key_path))
}
