#!/usr/bin/env bash
# Installs and updates Minutas; safe to run any number of times. Each step prints
# `ok` when nothing needed doing, else what it changed; a failed step stops with
# the fix, and running the script again carries on (docs/design.md § Installing
# and updating). Sourcing it defines the steps without running them.
set -euo pipefail

# The clone itself, not the link minutas update runs it through: the App step compares the two.
repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

# The window, its player and the calendar, then the recorder (pulsesrc through
# pipewire-pulse, opusenc, gst-launch-1.0, gio); curl fetches uv's installer and
# libsecret-tools keeps the Hugging Face token.
system_packages=(
  python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-media-gstreamer gir1.2-ecal-2.0 gir1.2-edataserver-1.2
  gstreamer1.0-plugins-good gstreamer1.0-plugins-base gstreamer1.0-tools libglib2.0-bin pipewire-pulse
  curl libsecret-tools
)
uv_version=0.13.6
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
  if ! sudo -n true 2>/dev/null && [[ ! -t 0 ]]; then
    fail "System packages" "missing ${missing[*]}, and sudo needs a password. Run: sudo apt install ${missing[*]}"
  fi
  sudo apt-get install -y "${missing[@]}" \
    || fail "System packages" "apt could not install ${missing[*]} (see above). Fix that, then run install.sh again."
  report "System packages" "installed ${missing[*]}"
}

step_uv() {
  if [[ -x $uv_bin && $("$uv_bin" --version) == "uv $uv_version "* ]]; then
    report uv ok
    return
  fi
  curl -LsSf "https://astral.sh/uv/$uv_version/install.sh" \
    | UV_INSTALL_DIR=$HOME/.local/bin UV_NO_MODIFY_PATH=1 sh \
    || fail uv "could not install uv $uv_version (see above). Check the network, then run install.sh again."
  report uv "installed uv $uv_version in $HOME/.local/bin"
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
  local conditions="Accept the conditions of https://huggingface.co/pyannote/speaker-diarization-community-1, then create a read token at https://huggingface.co/settings/tokens"
  if [[ ! -t 0 ]]; then
    fail "Hugging Face token" "none in the GNOME keyring. $conditions, and run install.sh again in a terminal, or store it with: secret-tool store --label='Minutas: Hugging Face token' ${token_attributes[*]}"
  fi
  printf '%s.\n' "$conditions"
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

# Publish to Agaric runs `agaric-mcp call`, which a stale copy of the bridge lacks.
step_agaric_mcp() {
  if [[ $(agaric-mcp --help 2>/dev/null) == *"call <tool>"* ]]; then
    report agaric-mcp ok
    return
  fi
  report agaric-mcp "no agaric-mcp with \`call\` on the PATH; Publish to Agaric needs it. Put the bridge that ships with Agaric 0.25 or newer there (https://github.com/jfolcini/agaric/issues/6980)"
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
  report glab "$found; Publish to GitLab stays off until glab 1.91 or newer is installed from GitLab's own .deb: https://gitlab.com/gitlab-org/cli/-/releases"
}

step_devin() {
  if command -v devin >/dev/null; then
    report "Devin CLI" ok
    return
  fi
  report "Devin CLI" "not installed; Summarize needs it. Install it with \`curl -fsSL https://cli.devin.ai/install.sh | bash\`, then log in with \`devin auth login\`"
}

main() {
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
  step_devin
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
