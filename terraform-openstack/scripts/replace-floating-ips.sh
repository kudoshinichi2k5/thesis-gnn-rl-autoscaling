#!/usr/bin/env bash

set -euo pipefail

# ============================================================
# Configuration
# ============================================================

DESIRED_PREFIX="192.168.120."
MAX_ATTEMPTS_PER_NODE=10

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIRECTORY="${SCRIPT_DIR}/../environments/dev"

SKIP_INITIAL_APPLY=false


# ============================================================
# Argument parsing
# ============================================================

usage() {
    cat <<EOF
Usage:
  $(basename "$0") [options]

Options:
  --prefix PREFIX          Desired Floating IP prefix
                           Default: ${DESIRED_PREFIX}

  --max-attempts NUMBER    Maximum replacement attempts per node
                           Default: ${MAX_ATTEMPTS_PER_NODE}

  --terraform-dir PATH     Terraform directory
                           Default: ${TERRAFORM_DIRECTORY}

  --skip-initial-apply     Skip the initial terraform apply

  -h, --help               Show this help message

Example:
  ./replace-floating-ips.sh

  ./replace-floating-ips.sh \\
      --prefix 192.168.120. \\
      --max-attempts 10

  ./replace-floating-ips.sh \\
      --skip-initial-apply
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)
            DESIRED_PREFIX="$2"
            shift 2
            ;;

        --max-attempts)
            MAX_ATTEMPTS_PER_NODE="$2"
            shift 2
            ;;

        --terraform-dir)
            TERRAFORM_DIRECTORY="$2"
            shift 2
            ;;

        --skip-initial-apply)
            SKIP_INITIAL_APPLY=true
            shift
            ;;

        -h|--help)
            usage
            exit 0
            ;;

        *)
            echo "Error: Unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done


# ============================================================
# Validation
# ============================================================

if [[ ! -d "$TERRAFORM_DIRECTORY" ]]; then
    echo "Error: Terraform directory does not exist:"
    echo "  $TERRAFORM_DIRECTORY"
    exit 1
fi

if ! [[ "$MAX_ATTEMPTS_PER_NODE" =~ ^[0-9]+$ ]]; then
    echo "Error: --max-attempts must be a number."
    exit 1
fi

if (( MAX_ATTEMPTS_PER_NODE < 1 || MAX_ATTEMPTS_PER_NODE > 50 )); then
    echo "Error: --max-attempts must be between 1 and 50."
    exit 1
fi

if ! command -v terraform >/dev/null 2>&1; then
    echo "Error: terraform is not installed or not in PATH."
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "Error: jq is required but was not found."
    echo "Install it with:"
    echo "  sudo apt install jq"
    exit 1
fi


# ============================================================
# Terraform helper
# ============================================================

invoke_terraform() {
    terraform "-chdir=${TERRAFORM_DIRECTORY}" "$@"
}


# ============================================================
# Get node addresses
# ============================================================

get_node_addresses() {
    invoke_terraform output -json node_fixed_ips
}


# ============================================================
# Initial Terraform apply
# ============================================================

if [[ "$SKIP_INITIAL_APPLY" == false ]]; then
    echo "Running initial Terraform apply..."

    invoke_terraform apply -auto-approve

    echo
fi


# ============================================================
# Track replacement attempts
# ============================================================

declare -A ATTEMPTS=()


# ============================================================
# Main loop
# ============================================================

while true; do

    NODE_ADDRESSES="$(get_node_addresses)"

    # --------------------------------------------------------
    # Find nodes whose Floating IP does not match the prefix
    # --------------------------------------------------------

    mapfile -t INCORRECT_NODES < <(
        echo "$NODE_ADDRESSES" |
        jq -r --arg prefix "$DESIRED_PREFIX" '
            to_entries[]
            | select(.value.floating_ip | startswith($prefix) | not)
            | .key
        '
    )

    # --------------------------------------------------------
    # Everything is correct
    # --------------------------------------------------------

    if [[ ${#INCORRECT_NODES[@]} -eq 0 ]]; then
        echo "All floating IPs match the required prefix ${DESIRED_PREFIX}"
        break
    fi


    # --------------------------------------------------------
    # Replace incorrect Floating IPs
    # --------------------------------------------------------

    for NODE_NAME in "${INCORRECT_NODES[@]}"; do

        FLOATING_IP="$(
            echo "$NODE_ADDRESSES" |
            jq -r --arg node "$NODE_NAME" '
                .[$node].floating_ip
            '
        )"

        PREVIOUS_ATTEMPTS="${ATTEMPTS[$NODE_NAME]:-0}"
        CURRENT_ATTEMPTS=$((PREVIOUS_ATTEMPTS + 1))

        ATTEMPTS["$NODE_NAME"]="$CURRENT_ATTEMPTS"


        # ----------------------------------------------------
        # Maximum attempts reached
        # ----------------------------------------------------

        if (( CURRENT_ATTEMPTS > MAX_ATTEMPTS_PER_NODE )); then
            echo "Error: Node ${NODE_NAME} did not receive a ${DESIRED_PREFIX} floating IP after ${MAX_ATTEMPTS_PER_NODE} replacement attempts." >&2
            echo "Latest IP: ${FLOATING_IP}" >&2
            exit 1
        fi


        # ----------------------------------------------------
        # Terraform resource address
        # ----------------------------------------------------

        RESOURCE_ADDRESS="module.floating_ip.openstack_networking_floatingip_v2.node[\"${NODE_NAME}\"]"


        echo
        echo "WARNING: Node ${NODE_NAME} received ${FLOATING_IP};"
        echo "         replacing its floating IP"
        echo "         attempt ${CURRENT_ATTEMPTS} of ${MAX_ATTEMPTS_PER_NODE}"
        echo


        # ----------------------------------------------------
        # Force replacement
        # ----------------------------------------------------

        invoke_terraform \
            apply \
            -auto-approve \
            "-replace=${RESOURCE_ADDRESS}"

    done

done
