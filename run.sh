#!/usr/bin/env bash
# Start Feedback Pulse in the DKubeX workspace, under the base path that
# `d3x app create` assigned. Postgres/MinIO settings must already be in the
# environment, or pass a file of them:  bash run.sh path/to/env-file
set -euo pipefail
cd "$(dirname "$0")"

set -a
. ./.dkubex-app.env
[ $# -gt 0 ] && . "$1"
set +a

exec uv run --with-requirements requirements.txt \
  streamlit run app.py --server.address 0.0.0.0 --server.port "$PORT" \
  --server.baseUrlPath "$DKUBEX_BASE_PATH" --server.headless true
