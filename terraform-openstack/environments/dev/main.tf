# The root module composes reusable modules and supplies environment-specific values.
module "networking" {
  source = "../../modules/networking"

  network_name = var.network_name
  network_id   = var.network_id
}

module "security_group" {
  source = "../../modules/security-group"

  security_group_name = var.security_group_name
}

module "keypair" {
  source = "../../modules/keypair"

  keypair_name    = var.keypair_name
  public_key_path = var.public_key_path
}

module "compute" {
  source = "../../modules/compute"

  nodes               = var.nodes
  network_id          = module.networking.network_id
  security_group_name = module.security_group.security_group_name
  keypair_name        = module.keypair.name
  image_id            = var.image_id
}
