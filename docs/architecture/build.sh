#!/usr/bin/env bash
# Regenerate the system-architecture diagrams from architecture.dot.
#
# Per agent imperative #7: run this whenever a service, driver, hardware
# item, or topic is added/removed/renamed.
#
# Outputs:
#   docs/architecture/architecture.pdf  (vector, primary deliverable)
#   docs/architecture/architecture.svg  (web-friendly preview)
#   docs/architecture/architecture.png  (for README embedding)

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v dot >/dev/null 2>&1; then
    echo "graphviz 'dot' not found. Install with: sudo apt-get install -y graphviz" >&2
    exit 1
fi

dot -Tpdf architecture.dot -o architecture.pdf
dot -Tsvg architecture.dot -o architecture.svg
dot -Tpng -Gdpi=144 architecture.dot -o architecture.png

echo "✓ Generated:"
ls -la architecture.{pdf,svg,png}
