#!/bin/sh
# Launch the candidate matching this source checkout from any working directory.
set -eu
project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
version=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$project_dir/src/pumpwizard_runner/__init__.py")
if [ -z "$version" ]; then
    printf '%s\n' 'The Runner source version could not be read.' >&2
    exit 1
fi
build_name=${version##*.}
candidate=${PUMPWIZARD_CANDIDATE:-"$project_dir/dist/$build_name/SolanaMarketWizard/SolanaMarketWizard"}
candidate_dir=$(dirname -- "$candidate")
if [ ! -x "$candidate" ] || [ ! -d "$candidate_dir/_internal" ]; then
    printf '%s\n' "The $version candidate is missing. Build it in an isolated staging directory; see docs/PILOT-LINUX-CANDIDATE.md." >&2
    exit 1
fi
if [ "$#" -eq 0 ]; then
    set -- service launch-dashboard
fi
exec "$candidate" "$@"
