module "networking" {
  source = "../../modules/networking"

  network_name = var.network_name
}

module "security_group" {
  source = "../../modules/security-group"

  security_group_name = var.security_group_name

  # Replace with the actual CIDR of Public_Net.
  network_cidr = var.network_cidr
}

module "compute" {
  source = "../../modules/compute"

  nodes               = var.nodes
  network_id          = module.networking.network_id
  security_group_name = module.security_group.security_group_name
  key_pair            = var.key_pair
  image_id            = var.image_id
  assign_floating_ip  = var.assign_floating_ip
}