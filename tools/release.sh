#!/bin/sh
# Builds the programs for a release into dist/: tools/release.sh 1.2.0
set -e
version="${1:?usage: tools/release.sh VERSION}"
mkdir -p dist
for target in linux/amd64 linux/arm64 windows/amd64 darwin/amd64 darwin/arm64; do
  os="${target%/*}"; arch="${target#*/}"
  ext=""; [ "$os" = windows ] && ext=".exe"
  GOOS="$os" GOARCH="$arch" CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$version" -o "dist/termdeck-$version-$os-$arch$ext" .
  echo "dist/termdeck-$version-$os-$arch$ext"
done
