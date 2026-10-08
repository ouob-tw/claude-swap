#!/usr/bin/env bash
# Record timing metadata, then preserve the user's existing status line.
input=$(cat)
printf '%s' "$input" | "$2" >/dev/null 2>&1 || true
printf '%s' "$input" | bash -c "$1"
