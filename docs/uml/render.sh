#!/usr/bin/env bash
# Rebuild the UML figures from src/*.puml.
#
# Only `out/pdf-notitle/` is committed. That is the set the report uses, where the
# embedded title is stripped so it does not compete with the LaTeX caption. PNGs and
# the title-bearing PDFs are ~6 MB and fully regenerable, so they are gitignored;
# run this script to get them locally for slides or advisor review.
#
# Requires: java, graphviz (`dot`), rsvg-convert for the PDFs, and plantuml.jar (downloaded
# on first run).
#
#   ./render.sh            # everything, into out/
#   ./render.sh png        # one format only: png | pdf | pdf-notitle

set -euo pipefail
cd "$(dirname "$0")"

PLANTUML_VERSION="${PLANTUML_VERSION:-1.2026.0}"
JAR="${PLANTUML_JAR:-.plantuml/plantuml-${PLANTUML_VERSION}.jar}"

command -v java >/dev/null || { echo "error: java not found"; exit 1; }
if [ "${1:-all}" != png ] && ! command -v rsvg-convert >/dev/null; then
  echo "error: rsvg-convert not found. The PDFs are converted from SVG with it."
  echo "  Fedora: sudo dnf install librsvg2-tools"
  echo "  Debian: sudo apt install librsvg2-bin"
  exit 1
fi
if ! command -v dot >/dev/null; then
  echo "error: graphviz not found. Several figures need it."
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

# PlantUML writes PDF only with Batik and FOP on its classpath, which the single jar does
# not carry, so a PDF is rendered as SVG and converted with rsvg-convert.
render() {
  local fmt="$1" outdir="out/$2" src="${3:-src}"
  mkdir -p "$outdir"
  echo "  $2/"
  if [ "$fmt" = pdf ]; then
    local svgdir f
    svgdir="$(mktemp -d)"
    java -jar "$JAR" -tsvg -o "$svgdir" "$src"/*.puml
    for f in "$svgdir"/*.svg; do
      rsvg-convert -f pdf -o "$outdir/$(basename "${f%.svg}").pdf" "$f"
    done
    rm -rf "$svgdir"
  else
    java -jar "$JAR" -t"$fmt" -o "$(pwd)/$outdir" "$src"/*.puml
  fi
}

# The same PDFs with each figure's `title` line removed, rendered from a stripped copy of
# src/ so the sources keep their titles for the PNG and titled PDF sets.
render_notitle() {
  local tmp f
  tmp="$(mktemp -d)"
  cp src/*.iuml "$tmp"/
  for f in src/*.puml; do
    sed '/^title /d' "$f" > "$tmp/$(basename "$f")"
  done
  render pdf pdf-notitle "$tmp"
  rm -rf "$tmp"
}

case "${1:-all}" in
  png)         render png png ;;
  pdf)         render pdf pdf ;;
  pdf-notitle) render_notitle ;;
  all)         render png png; render pdf pdf; render_notitle ;;
  *) echo "usage: $0 [all|png|pdf|pdf-notitle]"; exit 1 ;;
esac
echo "done."
