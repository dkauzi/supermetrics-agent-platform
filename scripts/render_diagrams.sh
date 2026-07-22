#!/usr/bin/env bash
# Render the architecture diagrams to SVG and PNG.
#
# The .mmd files are the single source. ARCHITECTURE.md embeds the rendered
# images rather than duplicating the mermaid, so there is no second copy to
# drift. Re-run this after editing any .mmd and commit the output.
#
#   ./scripts/render_diagrams.sh
#
# Needs node (uses npx, no install required). Rendered files are committed so
# neither the repo nor GitHub Pages depends on a toolchain to display them.

set -euo pipefail
cd "$(dirname "$0")/../docs/diagrams"

render() {
  local name="$1" width="$2"
  echo "  ${name}"
  # SVG for docs (scales cleanly), PNG for slides and anywhere SVG is awkward.
  npx -y @mermaid-js/mermaid-cli@11 -i "${name}.mmd" -o "${name}.svg" \
      -b transparent -w "${width}" >/dev/null 2>&1
  npx -y @mermaid-js/mermaid-cli@11 -i "${name}.mmd" -o "${name}.png" \
      -b white -w "$((width + 200))" >/dev/null 2>&1
}

echo "Rendering diagrams..."
render cloud-architecture 1800
render bigquery-schema 1400

echo
ls -la ./*.svg ./*.png | awk '{printf "  %-30s %6.0f KB\n", $9, $5/1024}'
