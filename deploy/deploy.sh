#!/usr/bin/env bash
#
# Deploy the hot tub controller to the Pi. Run on your own computer, from
# anywhere inside the repository:
#
#     deploy/deploy.sh                      # to james-brown.local
#     deploy/deploy.sh you@some-other-host
#
# What it does:
#   1. Runs the tests, so a broken version is not sent.
#   2. Builds a release in build/release: the program as one .whl file,
#      the exact library versions it was tested with, and the files in
#      deploy/.
#   3. Copies that to ~/hottub-release on the Pi.
#   4. Runs install.sh there with sudo (it explains itself as it goes),
#      which installs the new version, restarts the service and checks
#      that it answers.
#   5. Checks from here that the pages are reachable over the network.
#
# You are asked for your password on the Pi (once for SSH unless you use a
# key, and once for sudo). You log in as yourself; nobody ever logs in as
# the service's user "hottub".
#
# Options:
#   --skip-tests     do not run the tests first
#   --allow-dirty    deploy even with uncommitted changes
#   --no-nginx       passed on to install.sh: leave nginx on the Pi alone

set -euo pipefail

HOST=james-brown.local
RUN_TESTS=yes
ALLOW_DIRTY=no
INSTALL_OPTIONS=()
for argument in "$@"; do
    case "$argument" in
        --skip-tests)  RUN_TESTS=no ;;
        --allow-dirty) ALLOW_DIRTY=yes ;;
        --no-nginx)    INSTALL_OPTIONS+=(--no-nginx) ;;
        -h|--help)     sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)            echo "Unknown option: $argument" >&2; exit 2 ;;
        *)             HOST="$argument" ;;
    esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v uv >/dev/null || die "uv is not installed (https://docs.astral.sh/uv/)."

# --- What is being deployed ---------------------------------------------------

if [ -n "$(git status --porcelain)" ]; then
    [ "$ALLOW_DIRTY" = yes ] || die "There are uncommitted changes. Commit them, or pass --allow-dirty."
fi
RELEASE_NAME="$(git describe --always --dirty) ($(date '+%Y-%m-%d %H:%M'))"

if [ "$RUN_TESTS" = yes ]; then
    say "Running the tests"
    uv run pytest -q
fi

# --- Build --------------------------------------------------------------------

say "Building release ${RELEASE_NAME}"
RELEASE_DIR=build/release
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR/nginx"
uv build --wheel --out-dir "$RELEASE_DIR" --quiet
# The libraries at exactly the versions in uv.lock. Without hashes: on a
# Pi, pip may fetch the same versions from piwheels.org, whose files are
# built for the Pi and so differ from the ones the hashes describe.
uv export --no-dev --no-emit-project --no-hashes --format requirements-txt \
    --quiet --output-file "$RELEASE_DIR/requirements.txt"
cp deploy/install.sh deploy/hottub.service deploy/hottub-backup.service \
   deploy/hottub-backup.timer deploy/hottub.toml deploy/secrets.env "$RELEASE_DIR/"
cp deploy/nginx/*.conf "$RELEASE_DIR/nginx/"
cp docs/operations.md "$RELEASE_DIR/"
echo "$RELEASE_NAME" > "$RELEASE_DIR/RELEASE"
rm -f "$RELEASE_DIR/.gitignore"
ls "$RELEASE_DIR"

# --- Copy and install ---------------------------------------------------------

# One SSH connection shared by the steps below, so the password is asked once.
SSH_OPTIONS=(-o ControlMaster=auto -o ControlPersist=120 -o "ControlPath=$HOME/.ssh/hottub-deploy-%C")

say "Copying to ${HOST}"
ssh "${SSH_OPTIONS[@]}" "$HOST" 'rm -rf hottub-release && mkdir hottub-release'
scp "${SSH_OPTIONS[@]}" -q -r "$RELEASE_DIR"/. "$HOST":hottub-release/

say "Installing on ${HOST} (sudo will ask for your password there)"
ssh "${SSH_OPTIONS[@]}" -t "$HOST" "sudo bash hottub-release/install.sh ${INSTALL_OPTIONS[*]:-}"

# --- Check from here ----------------------------------------------------------

say "Checking from this computer"
ADDRESS="${HOST#*@}"
URL="http://${ADDRESS}/hot-tub/api/v1/healthz"
if answer="$(curl -fsS --max-time 10 "$URL")"; then
    echo "    ${URL} answers: ${answer}"
    echo "    Open http://${ADDRESS}/hot-tub/"
else
    die "${URL} does not answer from here, although the install succeeded on the Pi.
       Check nginx on the Pi (docs/operations.md, \"The pages do not load\")."
fi
