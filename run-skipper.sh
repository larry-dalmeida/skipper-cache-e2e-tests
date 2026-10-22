#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
: "${SKIPPER_CHECKOUT:=${1:?Pass a clean Skipper checkout or set SKIPPER_CHECKOUT}}"
exec python3 lifecycle.py skipper "$SKIPPER_CHECKOUT"
