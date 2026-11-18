#!/usr/bin/env bash
# prepare-commit-msg hook: add the author's `Signed-off-by:` trailer, as
# `git commit -s` would, so the DCO check on pull requests never fails on a
# forgotten `-s`. A message that already carries it is left alone.
set -euo pipefail

msg_file=$1
author=$(git var GIT_AUTHOR_IDENT)
author=${author%>*}'>'

git interpret-trailers --in-place --if-exists addIfDifferent \
  --trailer "Signed-off-by: ${author}" "$msg_file"
