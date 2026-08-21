#!/usr/bin/env bash
# Rebuild the UML figures from src/*.puml.
#
# Only `out/pdf-notitle/` is committed — that is the set the report uses, where the
# embedded title is stripped so it does not compete with the LaTeX caption. PNGs and
# the title-bearing PDFs are ~6 MB and fully regenerable, so they are gitignored;
# run this script to get them locally for slides or advisor review.
#
# Requires: java, graphviz (`dot`), and plantuml.jar (downloaded on first run).
#
#   ./render.sh            # everything, into out/
#   ./render.sh png        # one format only: png | pdf | pdf-notitle

set -euo pipefail
cd "$(dirname "$0")"

PLANTUML_VERSION="${PLANTUML_VERSION:-1.2026.0}"
JAR="${PLANTUML_JAR:-.plantuml/plantuml-${PLANTUML_VERSION}.jar}"

command -v java >/dev/null || { echo "error: java not found"; exit 1; }
if ! command -v dot >/dev/null; then
  echo "error: graphviz not found — several figures need it."
  echo "  Fedora: sudo dnf install graphviz"
  echo "  Debian: sudo apt install graphviz"
  exit 1
fi

if [ ! -f "$JAR" ]; then
  echo "fetching plantuml ${PLANTUML_VERSION}..."
  mkdir -p "$(dirname "$JAR")"
  curl -fsSL -o "$JAR" \
    "https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar"
fi

render() {
  local fmt="$1" outdir="out/$2"
  mkdir -p "$outdir"
  echo "  $2/"
  java -jar "$JAR" -t"$fmt" -o "$(pwd)/$outdir" src/*.puml
}

case "${1:-all}" in
  png)         render png png ;;
  pdf)         render pdf pdf ;;
  pdf-notitle) render pdf pdf-notitle ;;
  all)         render png png; render pdf pdf; render pdf pdf-notitle ;;
  *) echo "usage: $0 [all|png|pdf|pdf-notitle]"; exit 1 ;;
esac
echo "done."
