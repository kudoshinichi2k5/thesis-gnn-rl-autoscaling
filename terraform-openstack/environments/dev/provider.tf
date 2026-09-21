terraform {
  required_version = ">= 1.6.0"

  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 3.0"
    }
  }
}

# Authentication is read from OS_* variables exported by openstack-openrc.sh.
provider "openstack" {}
