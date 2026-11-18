#!/bin/bash
# Loads the extension into a headless GNOME Shell with its own HOME, XDG dirs and session
# bus, so the real session and its dconf are never touched, and drives it through a fake
# `minutas` that writes status.json the way the program does. Exits 1 on any failed check.
#
#   extension/tests/smoke.sh [OUT_DIR]    # OUT_DIR keeps the logs and the menu screenshots
set -u

UUID=minutas@jfolcini.github.io

if [ -z "${MINUTAS_SMOKE_WORK:-}" ]; then
  EXTENSION_DIR=$(cd "$(dirname "$0")/.." && pwd)
  OUT=${1:-$(mktemp -d)}
  mkdir -p "$OUT" && OUT=$(cd "$OUT" && pwd)
  WORK="$OUT/work"
  rm -rf "$WORK" "$OUT"/*.png "$OUT/shell.log"
  mkdir -p "$WORK"/{home,config,data,cache,state,bin}
  # A Wayland socket path must fit in 108 bytes, so the runtime dir gets a short path.
  RUNTIME=$(mktemp -d /tmp/minutas-smoke.XXXXXX)
  # Services the session bus starts (the document portal) mount FUSE file systems in it.
  remove_runtime() {
    for mount in "$RUNTIME"/*/; do fusermount -uzq "$mount" 2>/dev/null; done
    rm -rf "$RUNTIME"
  }
  trap remove_runtime EXIT
  mkdir -p "$WORK/data/gnome-shell/extensions/smoke-unsafe@minutas.test"
  # The sources only: node_modules holds the dev tools.
  mkdir -p "$WORK/data/gnome-shell/extensions/$UUID"
  cp "$EXTENSION_DIR"/metadata.json "$EXTENSION_DIR"/*.js "$WORK/data/gnome-shell/extensions/$UUID"
  # Test-only helper: unsafe mode lets this script call org.gnome.Shell.Eval and Screenshot.
  echo '{"uuid": "smoke-unsafe@minutas.test", "name": "Smoke unsafe mode", "description": "Test only", "shell-version": ["46"]}' \
    >"$WORK/data/gnome-shell/extensions/smoke-unsafe@minutas.test/metadata.json"
  cat >"$WORK/data/gnome-shell/extensions/smoke-unsafe@minutas.test/extension.js" <<'EOF'
export default class {
    enable() { global.context.unsafe_mode = true; }
    disable() { global.context.unsafe_mode = false; }
}
EOF
  cat >"$WORK/bin/minutas" <<EOF
#!/bin/bash
echo "\$*" >>"$WORK/minutas.log"
data="\$XDG_DATA_HOME/minutas"
write() { mkdir -p "\$data" && printf '%s' "\$1" >"\$data/.status.tmp" && mv "\$data/.status.tmp" "\$data/status.json"; }
if [ -e "$WORK/fail-next" ]; then
  rm "$WORK/fail-next"
  printf 'warning: something minor\nerror: already recording\n' >&2
  exit 1
fi
case "\$*" in
  "record start")
    id=\$(date +%Y-%m-%dT%H-%M-%S) at=\$(date --iso-8601=seconds)
    echo "\$id \$at" >"$WORK/active"
    write "{\"version\":1,\"recording\":{\"id\":\"\$id\",\"started_at\":\"\$at\"},\"recent\":[]}"
    echo "{\"id\": \"\$id\", \"started_at\": \"\$at\"}"
    ;;
  "record stop")
    read -r id at <"$WORK/active"
    write "{\"version\":1,\"recording\":null,\"recent\":[{\"id\":\"\$id\",\"name\":\"Meeting\",\"started_at\":\"\$at\",\"duration_seconds\":7,\"incomplete\":false,\"transcribe\":\"queued\",\"failed\":[]}]}"
    ;;
esac
EOF
  chmod +x "$WORK/bin/minutas"
  env -i HOME="$WORK/home" XDG_CONFIG_HOME="$WORK/config" XDG_DATA_HOME="$WORK/data" \
    XDG_CACHE_HOME="$WORK/cache" XDG_STATE_HOME="$WORK/state" XDG_RUNTIME_DIR="$RUNTIME" \
    PATH="$WORK/bin:/usr/local/bin:/usr/bin:/bin" LANG=C.UTF-8 GIO_USE_VFS=local \
    MINUTAS_SMOKE_WORK="$WORK" MINUTAS_SMOKE_OUT="$OUT" \
    timeout 300 dbus-run-session -- bash "$0" 2>"$OUT/session.log"
  exit $?
fi

# From here on: inside the private session bus, with the temporary HOME and XDG dirs.
WORK=$MINUTAS_SMOKE_WORK
OUT=$MINUTAS_SMOKE_OUT
DATA="$XDG_DATA_HOME/minutas"
B="Main.panel.statusArea['$UUID']"
MENU="$B.menu._getMenuItems().flatMap(i => i._getMenuItems ? i._getMenuItems().map(j => j.label.text + (j.get_n_children() > 2 ? ' [warning icon]' : '')) : [i.label.text || '---'])"
PANEL="({recordingStyle: $B.has_style_class_name('screen-recording-indicator'), timerVisible: $B.get_first_child().get_child_at_index(1).visible})"
TIMER="$B.get_first_child().get_child_at_index(1).text"
NOTES="Main.messageTray.getSources().filter(s => s.title === 'Minutas').flatMap(s => s.notifications.map(n => n.title + ' | ' + n.body))"
failures=0

ev() { gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell --method org.gnome.Shell.Eval "$1"; }
fail() {
  echo "  FAIL: $1"
  failures=$((failures + 1))
}
# Polls, because the file monitor and spawned commands answer asynchronously.
expect() {
  local result
  for _ in $(seq 40); do
    result=$(ev "$1")
    case "$result" in *"$2"*) echo "  ok: $2" && return ;; esac
    sleep 0.25
  done
  fail "expected '$2' in $result"
}
check() {
  local description=$1
  shift
  if "$@"; then echo "  ok: $description"; else fail "$description"; fi
}
expect_log() { check "ran minutas $1" grep -qxF -- "$1" "$WORK/minutas.log"; }
click() { ev "$B.menu._getMenuItems().flatMap(i => i._getMenuItems ? i._getMenuItems() : [i]).find(i => i.label.text.startsWith('$1')).activate(null)" >/dev/null; }
write_status() { mkdir -p "$DATA" && printf '%s' "$1" >"$DATA/.status.tmp" && mv "$DATA/.status.tmp" "$DATA/status.json"; }
recent() { echo "{\"id\":\"$1\",\"name\":\"$2\",\"started_at\":\"$3\",\"duration_seconds\":$4,\"incomplete\":$5,\"transcribe\":$6,\"failed\":$7}"; }
screenshot() {
  ev "$B.menu.open()" >/dev/null
  sleep 1
  gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell/Screenshot \
    --method org.gnome.Shell.Screenshot.Screenshot false false "$OUT/$1" >/dev/null || fail "screenshot $1"
  ev "$B.menu.close()" >/dev/null
}

gsettings set org.gnome.shell enabled-extensions "['smoke-unsafe@minutas.test', '$UUID']"
gsettings set org.gnome.shell disable-extension-version-validation true
gsettings set org.gnome.shell welcome-dialog-last-shown-version '999'
gnome-shell --headless --virtual-monitor 1280x800 --no-x11 >"$OUT/shell.log" 2>&1 &
shell_pid=$!
trap 'kill $shell_pid 2>/dev/null; wait $shell_pid 2>/dev/null' EXIT
for _ in $(seq 120); do
  [ "$(ev true 2>/dev/null)" = "(true, 'true')" ] && break
  sleep 0.5
done
ev 'Main.overview.hide()' >/dev/null

echo "== enabled, no status.json yet"
expect "Main.extensionManager.lookup('$UUID').state" "'1'"
expect "$MENU" '["Record","---","---","Open history"]'

echo "== Record"
click Record
expect "$MENU" '["Stop",'
expect_log "record start"
expect "$PANEL" '{"recordingStyle":true,"timerVisible":true}'
expect "$NOTES" "Tell participants you're recording"
expect "Main.messageTray.getSources().filter(s => s.title === 'Minutas').flatMap(s => s.notifications).find(n => n.title.startsWith('Tell')).urgency" "'3'" # CRITICAL
first_tick=$(ev "$TIMER")
sleep 2
check "timer advances from $first_tick" test "$(ev "$TIMER")" != "$first_tick"
screenshot menu-recording.png

echo "== Stop"
click Stop
expect "$MENU" '["Record","---","Meeting · '
expect "$MENU" ' · 0:07 · transcribing…","---","Open history"]'
expect "$PANEL" '{"recordingStyle":false,"timerVisible":false}'
expect_log "record stop"
read -r id at <"$WORK/active"
meeting=$(recent "$id" Meeting "$at" 7 false '"succeeded"' '["summarize"]')

echo "== transcript ready, and clicking it opens the history"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "$id" Meeting "$at" 7 false '"succeeded"' '[]')]}"
expect "$NOTES" "Transcript ready | Meeting, ${at:0:10} ${at:11:5}"
ev "Main.messageTray.getSources().find(s => s.title === 'Minutas').notifications.find(n => n.title === 'Transcript ready').activate()" >/dev/null
sleep 1
expect_log "history --recording $id"

echo "== Summarize failed"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$meeting]}"
expect "$NOTES" "Summarize failed | Meeting"
expect "$MENU" ' · 0:07 [warning icon]'
screenshot menu-idle.png

echo "== a half-written status.json keeps the last snapshot"
printf '{"version": 1, "recording": {"id": "2026' >"$DATA/status.json"
sleep 1
expect "$MENU" ' · 0:07 [warning icon]'
check "the invalid file is logged" grep -q 'Minutas: keeping the last status' "$OUT/shell.log"

echo "== the recorder dies"
now=$(date --iso-8601=seconds) id2=$(date +%Y-%m-%dT%H-%M-%S)
write_status "{\"version\":1,\"recording\":{\"id\":\"$id2\",\"started_at\":\"$now\"},\"recent\":[$meeting]}"
expect "$MENU" '["Stop",'
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "$id2" Standup "$now" 3 true null '[]'),$meeting]}"
expect "$NOTES" "Recording stopped unexpectedly | Standup"
expect "$MENU" '["Record","---","Standup · '

echo "== a failing command shows stderr's last line"
touch "$WORK/fail-next"
click Record
expect "$NOTES" "Couldn't start recording | error: already recording"

echo "== no minutas on the PATH or in ~/.local/bin"
mv "$WORK/bin/minutas" "$WORK/minutas.away"
click Record
expect "$NOTES" "Minutas isn't installed"
mv "$WORK/minutas.away" "$WORK/bin/minutas"

echo "== a recent recording and Open history open the history"
click Standup
click "Open history"
sleep 1
expect_log "history --recording $id2"
expect_log "history"

echo "== disable removes everything; enable announces only what changed meanwhile (the lock screen)"
gnome-extensions disable "$UUID"
expect "$B === undefined" "'true'"
expect "Main.messageTray.getSources().filter(s => s.title === 'Minutas').length" "'0'"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "$id2" Standup "$now" 3 true '"succeeded"' '[]'),$meeting]}"
gnome-extensions enable "$UUID"
expect "$MENU" '["Record","---","Standup · '
expect "$NOTES" "'[\"Transcript ready | Standup, ${now:0:10} ${now:11:5}\"]'"

echo "== shell log"
if grep -E 'JS ERROR|JS WARNING' "$OUT/shell.log"; then fail "JS errors in $OUT/shell.log"; else echo "  ok: no JS errors"; fi

echo "$failures failed"
[ "$failures" -eq 0 ]
