terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  # Every resource carries these, which is what makes the cost report
  # attributable. docs/cost-model.md section 5 promises measured cost per
  # strategy from counted API calls; Cost Explorer can only produce that
  # split if the resources are tagged consistently.
  default_tags {
    tags = {
      Project     = "nimbus"
      Environment = var.environment
      ManagedBy   = "terraform"
      Repo        = "nimbus"
    }
  }
}

# A stable per-deployment suffix. S3 bucket names are globally unique, so this
# has to exist; using it for every resource keeps two deployments in one account
# from colliding, which matters because teardown-and-redeploy is the normal
# working pattern here (docs/cost-model.md section 4).
resource "random_id" "suffix" {
  byte_length = 4
}
