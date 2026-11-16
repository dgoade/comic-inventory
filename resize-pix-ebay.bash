#!/usr/bin/env bash
mkdir -p ~/Pictures/comics-ebay
sips -s format jpeg -s formatOptions 80 -Z 1600 \
  "${HOME}/Downloads/${1}" \
  --out "${HOME}/Pictures/comics-ebay/${1}"
