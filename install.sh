#!/usr/bin/env bash
# Installs and updates Minutas; safe to run any number of times. Each step prints
# `ok` when nothing needed doing, else what it changed; a failed step stops with
# the fix, and running the script again carries on (docs/design.md § Installing
# and updating). Sourcing it defines the steps without running them.
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# The window and calendar, then the recorder (pulsesrc through pipewire-pulse,
# opusenc, gst-launch-1.0, gio); curl fetches uv's installer and
# libsecret-tools keeps the Hugging Face token.
system_packages=(
  python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-ecal-2.0 gir1.2-edataserver-1.2
  gstreamer1.0-plugins-good gstreamer1.0-plugins-base gstreamer1.0-tools libglib2.0-bin pipewire-pulse
  curl libsecret-tools
)
uv_version=0.13.6
uv_bin=$HOME/.local/bin/uv
speech_env=$HOME/.local/share/minutas/speech
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
    --frozen --managed-python --no-install-project --no-dev --group speech \
    || fail "Speech environment" "uv sync failed (see above). Fix that, then run install.sh again."
  cp "$repo_dir/uv.lock" "$speech_env/uv.lock"
  report "Speech environment" "synced $speech_env from uv.lock"
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

step_models() {
  local token
  token=$(secret-tool lookup "${token_attributes[@]}" 2>/dev/null) || token=
  # huggingface_hub restarts an interrupted file under a new name and never removes the old one.
  if [[ -d $models_dir ]]; then find "$models_dir" -name '*.incomplete' -delete; fi
  if ! HF_TOKEN=$token "$speech_env/bin/python" - "$models_dir" "${models[@]}" <<'PY'
import sys

from huggingface_hub import snapshot_download
from huggingface_hub.errors import GatedRepoError, LocalEntryNotFoundError

cache_dir, specs = sys.argv[1], [spec.split() for spec in sys.argv[2:]]
downloaded = []
for repo_id, revision, pattern in specs:
    where = {"repo_id": repo_id, "revision": revision, "cache_dir": cache_dir, "allow_patterns": pattern}
    try:
        snapshot_download(**where, local_files_only=True)
        continue
    except LocalEntryNotFoundError:
        pass
    try:
        snapshot_download(**where)
    except GatedRepoError:
        sys.exit(
            f"Hugging Face refused {repo_id}. Accept its conditions at "
            f"https://huggingface.co/{repo_id} with the account the token belongs to. To replace "
            "the token: secret-tool clear service minutas username huggingface"
        )
    downloaded.append(f"{repo_id}@{revision[:7]}")
print("Models:", f"downloaded {', '.join(downloaded)}" if downloaded else "ok")
PY
  then
    fail Models "see above, then run install.sh again."
  fi
}

main() {
  step_system_packages
  step_uv
  step_speech_environment
  step_huggingface_token
  step_models
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
