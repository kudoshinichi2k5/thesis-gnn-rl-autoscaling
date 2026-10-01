# The root module composes reusable modules and supplies environment-specific values.
locals {
  floating_ip_node_names = [
    for node in var.nodes : node.name
    if node.role != "app-worker"
  ]

  floating_ip_node_ports = {
    for node_name, node_port in module.compute.node_ports : node_name => node_port
    if contains(local.floating_ip_node_names, node_name)
  }
}

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

  security_group = var.security_group
}

module "keypair" {
  source = "../../modules/keypair"

  keypair_name    = var.keypair_name
  public_key_path = var.public_key_path
}

module "compute" {
  source = "../../modules/compute"

  nodes             = var.nodes
  network_id        = module.networking.private_network_id
  security_group_id = module.security_group.security_group_id
  keypair_name      = module.keypair.name
  image_id          = var.image_id

  depends_on = [module.networking]
}

module "floating_ip" {
  source = "../../modules/floating-ip"

  external_network_name = module.networking.external_network_name
  node_ports            = local.floating_ip_node_ports

  depends_on = [module.compute]
}
