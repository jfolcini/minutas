#!/usr/bin/env bash
# Installs and updates Minutas; safe to run any number of times. Each step prints
# `ok` when nothing needed doing, else what it changed; a failed step stops with
# the fix, and running the script again carries on (docs/design.md § Installing
# and updating). Sourcing it defines the steps without running them.
set -euo pipefail

# The clone itself, not the link minutas update runs it through: the App step compares the two.
repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

# The window, its player and the calendar, then the recorder (GStreamer through PyGObject,
# pulsesrc through pipewire-pulse, opusenc) and the mix that is published (gst-launch-1.0, gio);
# curl downloads uv and libsecret-tools keeps the Hugging Face token.
system_packages=(
  python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-media-gstreamer gir1.2-ecal-2.0 gir1.2-edataserver-1.2
  gir1.2-gstreamer-1.0 gstreamer1.0-plugins-good gstreamer1.0-plugins-base gstreamer1.0-tools
  libglib2.0-bin pipewire-pulse
  curl libsecret-tools
)
uv_version=0.13.6
# The sha256 of that release's uv-<machine>-unknown-linux-gnu.tar.gz, as uv publishes it beside
# each archive: a download that doesn't match is never unpacked.
declare -A uv_sha256=(
  [x86_64]=f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645
  [aarch64]=229e194255c7958d86d56e8790d969a6f74736592133f0dfd0076f754c642c1d
)
uv_bin=$HOME/.local/bin/uv
speech_env=$HOME/.local/share/minutas/speech
app_dir=$HOME/.local/share/minutas/app
command_link=$HOME/.local/bin/minutas
# Where GNOME looks for launchers and extensions.
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
extension_uuid=minutas@jfolcini.github.io
extension_dir=$data_home/gnome-shell/extensions/$extension_uuid
extension_files=(metadata.json extension.js status.js)
models_dir=$HOME/.cache/minutas/models
# Hugging Face repository, commit, and the files the speech stack loads.
models=(
  "nvidia/parakeet-tdt-0.6b-v3 541d1f99c6b0c3cd0b11a95167540bb8edefd82b *.nemo"
  "pyannote/speaker-diarization-community-1 3533c8cf8e369892e6b79ff1bf80f7b0286a54ee *"
)
token_attributes=(service minutas username huggingface)
# For the summary: the optional tools missing, and whether GNOME Shell has a new extension to load.
missing_tools=()
log_out=false

intro() {
  cat <<'EOF'
Installs Minutas, or brings it up to date: the system packages it needs, uv,
the speech environment and its models, the app with its launcher, and the
top-bar extension. All but the system packages go in your home folder, and all
it downloads besides Ubuntu's own packages is pinned to an exact version.
It asks for two things, and only when it needs them: your password, through
sudo, to install missing system packages with apt; and once, a Hugging Face
token, to download the speaker model.
Each step prints ok when there was nothing to do, else what it did. A step that
fails says how to fix it, and running install.sh again carries on from there.

EOF
}

report() { printf '%s: %s\n' "$1" "$2"; }

fail() {
  printf '%s: failed: %s\n' "$1" "$2" >&2
  exit 1
}

step_system_packages() {
  local missing=() package
  for package in "${system_packages[@]}"; do
    if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null) != installed ]]; then
      missing+=("$package")
    fi
  done
  if ((${#missing[@]} == 0)); then
    report "System packages" ok
    return
  fi
  if ! sudo -n true 2>/dev/null; then
    [[ -t 0 ]] || fail "System packages" "missing ${missing[*]}, and sudo can ask for its password only in a terminal. Run: sudo apt install ${missing[*]}, then $repo_dir/install.sh again."
    echo "sudo asks for your password only to install these with apt: ${missing[*]}"
  fi
  sudo apt-get install -y "${missing[@]}" \
    || fail "System packages" "apt could not install ${missing[*]} (see above). Fix that, then run install.sh again."
  report "System packages" "installed ${missing[*]}"
}

# The archive stays in the cache, where the next uv update overwrites it.
step_uv() {
  if [[ -x $uv_bin && $("$uv_bin" --version) == "uv $uv_version "* ]]; then
    report uv ok
    return
  fi
  local machine archive download
  machine=$(uname -m)
  [[ -n ${uv_sha256[$machine]:-} ]] \
    || fail uv "uv $uv_version is pinned for x86_64 and aarch64 only, and this computer is $machine."
  archive=uv-$machine-unknown-linux-gnu
  download=$HOME/.cache/minutas/$archive.tar.gz
  mkdir -p "${download%/*}"
  curl -fLsS -o "$download" "https://github.com/astral-sh/uv/releases/download/$uv_version/$archive.tar.gz" \
    || fail uv "could not download uv $uv_version (see above). Check the network, then run install.sh again."
  sha256sum --check --status <<<"${uv_sha256[$machine]}  $download" \
    || fail uv "the uv $uv_version download doesn't match the checksum install.sh pins for it, so it wasn't installed. Run install.sh again; if it fails the same way, report it at https://github.com/jfolcini/minutas/issues"
  mkdir -p "${uv_bin%/*}"
  tar -xzf "$download" -C "${uv_bin%/*}" --strip-components=1 "$archive/uv" "$archive/uvx" \
    || fail uv "could not unpack $download (see above). Run install.sh again."
  report uv "installed uv $uv_version in ${uv_bin%/*}"
}

# The environment holds only what uv.lock pins, so an unchanged lock means nothing to do.
step_speech_environment() {
  if cmp -s "$repo_dir/uv.lock" "$speech_env/uv.lock"; then
    report "Speech environment" ok
    return
  fi
  UV_PROJECT_ENVIRONMENT=$speech_env "$uv_bin" sync --project "$repo_dir" \
    --frozen --managed-python --no-install-project --no-dev --extra speech \
    || fail "Speech environment" "uv sync failed (see above). Fix that, then run install.sh again."
  cp "$repo_dir/uv.lock" "$speech_env/uv.lock"
  report "Speech environment" "synced $speech_env from uv.lock"
}

desktop_entry() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=Minutas
Comment=Recorded meetings, their transcripts and what was published
Exec="$command_link" history
Icon=audio-input-microphone
Terminal=false
Categories=AudioVideo;Office;
EOF
}

# The window needs Ubuntu's Python, for its PyGObject, and the units the program starts run this
# environment's python with no PYTHONPATH, so the package and its dependencies live in it. Without
# --reinstall-package, uv reuses the build of minutas it cached until pyproject.toml changes.
step_app() {
  local desktop_file=$data_home/applications/io.github.jfolcini.Minutas.desktop
  if cmp -s "$repo_dir/uv.lock" "$app_dir/uv.lock" \
    && diff -rq -x __pycache__ "$repo_dir/src/minutas" "$app_dir"/lib/python3*/site-packages/minutas >/dev/null 2>&1 \
    && [[ $(readlink "$app_dir/repository") == "$repo_dir" && $(readlink "$command_link") == "$app_dir/bin/minutas" ]] \
    && "$app_dir/bin/minutas" --version >/dev/null 2>&1 \
    && desktop_entry | cmp -s - "$desktop_file"; then
    report App ok
    return
  fi
  "$uv_bin" venv --quiet --clear --system-site-packages --python /usr/bin/python3 "$app_dir" \
    && UV_PROJECT_ENVIRONMENT=$app_dir "$uv_bin" sync --quiet --project "$repo_dir" \
      --frozen --no-dev --no-editable --reinstall-package minutas --python /usr/bin/python3 \
    || fail App "uv could not install it (see above). Fix that, then run $repo_dir/install.sh again."
  cp "$repo_dir/uv.lock" "$app_dir/uv.lock"
  # Where minutas update finds the clone to update.
  ln -sfn "$repo_dir" "$app_dir/repository"
  mkdir -p "${command_link%/*}" "${desktop_file%/*}"
  ln -sfn "$app_dir/bin/minutas" "$command_link"
  desktop_entry >"$desktop_file"
  report App "installed $app_dir and the minutas command"
}

# GNOME Shell reads extensions only at login, and `gnome-extensions enable` asks the running Shell,
# which refuses one it found no files for then; the setting written here takes effect at login.
step_gnome_extension() {
  local enabled changed=false file
  enabled=$(gsettings get org.gnome.shell enabled-extensions) \
    || fail "GNOME extension" "could not read GNOME Shell's settings (see above). Install GNOME Shell 46 or newer, then run install.sh again."
  for file in "${extension_files[@]}"; do
    cmp -s "$repo_dir/extension/$file" "$extension_dir/$file" || changed=true
  done
  if [[ $changed == false && $enabled == *"'$extension_uuid'"* ]]; then
    report "GNOME extension" ok
    return
  fi
  local outcome=enabled
  if [[ $changed == true ]]; then
    mkdir -p "$extension_dir"
    cp "${extension_files[@]/#/$repo_dir/extension/}" "$extension_dir/"
    outcome="installed in $extension_dir; log out and back in once to load it"
    log_out=true
  fi
  if [[ $enabled != *"'$extension_uuid'"* ]]; then
    # The list as gsettings prints it: "@as []" when empty, else "['a', 'b']".
    enabled=${enabled#@as }
    enabled=${enabled%]}
    [[ $enabled == "[" ]] || enabled+=", "
    gsettings set org.gnome.shell enabled-extensions "$enabled'$extension_uuid']"
  fi
  report "GNOME extension" "$outcome"
}

step_huggingface_token() {
  if secret-tool lookup "${token_attributes[@]}" >/dev/null 2>&1; then
    report "Hugging Face token" ok
    return
  fi
  local why="pyannote's speaker model, which tells who said what, downloads only with the token of a Hugging Face account that accepted its conditions"
  local conditions="Accept the conditions of https://huggingface.co/pyannote/speaker-diarization-community-1, then create a read token at https://huggingface.co/settings/tokens"
  if [[ ! -t 0 ]]; then
    fail "Hugging Face token" "none in the GNOME keyring; $why. $conditions, and run $repo_dir/install.sh again in a terminal, or store it with: secret-tool store --label='Minutas: Hugging Face token' ${token_attributes[*]}"
  fi
  printf '%s. %s; it is kept in the GNOME keyring.\n' "$why" "$conditions"
  local token
  read -rsp "Hugging Face token: " token
  echo
  [[ -n $token ]] || fail "Hugging Face token" "no token entered. Run install.sh again."
  printf '%s' "$token" | secret-tool store --label="Minutas: Hugging Face token" "${token_attributes[@]}" \
    || fail "Hugging Face token" "could not store it in the GNOME keyring (see above). Unlock the keyring, then run install.sh again."
  report "Hugging Face token" "stored in the GNOME keyring"
}

# The list is copied in only once every download finished: snapshot_download's offline check
# accepts the README-only snapshot folder that a refused gated download leaves behind. The
# transcriber finds Parakeet through it.
step_models() {
  local revisions=$models_dir/revisions
  if printf '%s\n' "${models[@]}" | cmp -s - "$revisions"; then
    report Models ok
    return
  fi
  local token
  token=$(secret-tool lookup "${token_attributes[@]}" 2>/dev/null) || token=
  # A killed download leaves its partial file under a name huggingface_hub never reuses.
  if [[ -d $models_dir ]]; then find "$models_dir" -name '*.incomplete' -delete; fi
  HF_TOKEN=$token "$speech_env/bin/python" - "$models_dir" "${models[@]}" <<'PY' || fail Models "see above, then run install.sh again."
import sys

from huggingface_hub import snapshot_download
from huggingface_hub.errors import GatedRepoError

for repo_id, revision, pattern in (spec.split() for spec in sys.argv[2:]):
    try:
        snapshot_download(
            repo_id=repo_id, revision=revision, cache_dir=sys.argv[1], allow_patterns=pattern
        )
    except GatedRepoError:
        sys.exit(
            f"Hugging Face refused {repo_id}. Accept its conditions at "
            f"https://huggingface.co/{repo_id} with the account the token belongs to. To replace "
            "the token: secret-tool clear service minutas username huggingface"
        )
PY
  printf '%s\n' "${models[@]}" >"$revisions"
  report Models "fetched the pinned revisions into $models_dir"
}

# Publish to Agaric runs `agaric-mcp call`, which a stale copy of the bridge lacks. Like Publish
# to Agaric, it checks Agaric's own bridge first, which an AppImage of Agaric keeps current.
step_agaric_mcp() {
  local own=$data_home/com.agaric.app/bin/agaric-mcp bridge=agaric-mcp
  [[ -f $own ]] && bridge=$own
  if [[ $("$bridge" --help 2>/dev/null) == *"call <tool>"* ]]; then
    report agaric-mcp ok
    return
  fi
  missing_tools+=(agaric-mcp)
  report agaric-mcp "no agaric-mcp with \`call\` at $own or on the PATH; Publish to Agaric needs it. Install Agaric 0.26 or newer and start it once"
}

# GitLab's own .deb replaces Ubuntu's older glab in place; the snap can't read hidden folders in the
# home, such as ~/.local/share/minutas, so it couldn't upload the recordings.
step_glab() {
  local found="glab isn't on the PATH"
  # /snap/bin/glab links to snap.
  if [[ $(readlink -f "$(command -v glab)") == */snap ]]; then
    found="the PATH has the glab snap, which can't read the recordings"
  elif [[ $(glab --version 2>/dev/null) =~ ([0-9]+)\.([0-9]+)(\.[0-9]+)? ]]; then
    if ((BASH_REMATCH[1] > 1 || (BASH_REMATCH[1] == 1 && BASH_REMATCH[2] >= 91))); then
      report glab ok
      return
    fi
    found="the PATH has glab ${BASH_REMATCH[0]}"
  fi
  missing_tools+=(glab)
  report glab "$found; Publish to GitLab stays off until glab 1.91 or newer is installed from GitLab's own .deb: https://gitlab.com/gitlab-org/cli/-/releases"
}

# Publish to GitHub posts through gh, and Ubuntu's own is new enough: 2.45 has every flag it uses.
step_gh() {
  local found="gh isn't on the PATH"
  if [[ $(gh --version 2>/dev/null) =~ ([0-9]+)\.([0-9]+)(\.[0-9]+)? ]]; then
    if ((BASH_REMATCH[1] > 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] >= 45))); then
      report gh ok
      return
    fi
    found="the PATH has gh ${BASH_REMATCH[0]}"
  fi
  missing_tools+=(gh)
  report gh "$found; Publish to GitHub stays off until gh 2.45 or newer is installed: \`sudo apt install gh\`, or see https://cli.github.com"
}

# Claude Code's deny rule "*", which keeps every tool from the summary, came in 2.1.166.
step_summarizer() {
  if [[ $(claude --version 2>/dev/null) =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
    local major=${BASH_REMATCH[1]} minor=${BASH_REMATCH[2]} patch=${BASH_REMATCH[3]}
    if ((major > 2 || (major == 2 && (minor > 1 || (minor == 1 && patch >= 166))))); then
      report Summarizer ok
      return
    fi
    # The Devin CLI can still summarize.
    command -v devin >/dev/null || missing_tools+=(Summarizer)
    report Summarizer "the PATH has claude ${BASH_REMATCH[0]}; Summarize with Claude Code stays off until it is updated to 2.1.166 or newer: \`claude update\`"
    return
  fi
  if command -v devin >/dev/null; then
    report Summarizer ok
    return
  fi
  missing_tools+=(Summarizer)
  report Summarizer "not installed; Summarize needs the Devin CLI or Claude Code. Install the Devin CLI with \`curl -fsSL https://cli.devin.ai/install.sh | bash\`, then log in with \`devin auth login\`; or Claude Code with \`curl -fsSL https://claude.ai/install.sh | bash\`, then log in with \`claude auth login\`"
}

# What is left for the person to do, if anything.
summary() {
  local left=() list
  if [[ $log_out == true ]]; then
    left+=("Log out and back in to load the top-bar extension.")
  fi
  if ((${#missing_tools[@]} > 0)); then
    list=$(printf '%s, ' "${missing_tools[@]}")
    left+=("Optional, still missing: ${list%, }; their lines above say what needs them and how to get them.")
  fi
  if ((${#left[@]} > 0)); then
    printf '\nLeft to do:\n'
    printf -- '- %s\n' "${left[@]}"
  fi
}

main() {
  # The intro says what it may ask for, which it can ask only in a terminal: not under Update Minutas.
  if [[ -t 0 ]]; then intro; fi
  step_system_packages
  step_uv
  step_speech_environment
  step_app
  step_gnome_extension
  # CI has no GNOME keyring or Hugging Face account, so it leaves these two out.
  if [[ ${MINUTAS_SKIP_MODELS:-} == 1 ]]; then
    report "Hugging Face token" "skipped: MINUTAS_SKIP_MODELS=1"
    report Models "skipped: MINUTAS_SKIP_MODELS=1"
  else
    step_huggingface_token
    step_models
  fi
  step_agaric_mcp
  step_glab
  step_gh
  step_summarizer
  summary
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
