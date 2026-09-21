# The root module composes reusable modules and supplies environment-specific values.
module "networking" {
  source = "../../modules/networking"

  external_network_name = var.external_network_name
  external_network_id   = var.external_network_id
  private_network_name  = var.private_network_name
  private_subnet_name   = var.private_subnet_name
  private_subnet_cidr   = var.private_subnet_cidr
  router_name           = var.router_name
  dns_nameservers       = var.dns_nameservers
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

  nodes                 = var.nodes
  network_id            = module.networking.private_network_id
  external_network_name = module.networking.external_network_name
  security_group_name   = module.security_group.security_group_name
  keypair_name          = module.keypair.name
  image_id              = var.image_id

  depends_on = [module.networking]
}
