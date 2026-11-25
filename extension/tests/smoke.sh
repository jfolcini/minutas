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
  # What the session starts can outlive dbus-run-session: orphaned ibus-daemons, which gnome-shell
  # spawns, grew until they swapped the desktop to death. Each carries the session's environment.
  # Services the session bus starts (the document portal) mount FUSE file systems in RUNTIME.
  end_session() {
    grep -lzxF "MINUTAS_SMOKE_WORK=$WORK" /proc/[0-9]*/environ 2>/dev/null | cut -d/ -f3 | xargs -r kill -KILL 2>/dev/null
    for mount in "$RUNTIME"/*/; do fusermount -uzq "$mount" 2>/dev/null; done
    rm -rf "$RUNTIME"
  }
  trap end_session EXIT
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
  "record pause")
    read -r id at <"$WORK/active"
    write "{\"version\":1,\"recording\":{\"id\":\"\$id\",\"started_at\":\"\$at\",\"paused_at\":\"\$(date --iso-8601=seconds)\"},\"recent\":[]}"
    ;;
  "record resume")
    read -r id at <"$WORK/active"
    write "{\"version\":1,\"recording\":{\"id\":\"\$id\",\"started_at\":\"\$at\",\"paused_seconds\":2},\"recent\":[]}"
    ;;
  "record stop")
    read -r id at <"$WORK/active"
    write "{\"version\":1,\"recording\":null,\"recent\":[{\"id\":\"\$id\",\"name\":\"Meeting\",\"started_at\":\"\$at\",\"duration_seconds\":7,\"incomplete\":false,\"transcribe\":\"queued\",\"failed\":[]}]}"
    ;;
  "record discard")
    write '{"version":1,"recording":null,"recent":[]}'
    ;;
esac
EOF
  chmod +x "$WORK/bin/minutas"
  # The default browser: logs the links the extension opens.
  printf '#!/bin/bash\necho "$1" >>"%s/browser.log"\n' "$WORK" >"$WORK/bin/smoke-browser"
  chmod +x "$WORK/bin/smoke-browser"
  mkdir -p "$WORK/data/applications"
  printf '[Desktop Entry]\nType=Application\nName=Smoke browser\nExec=%s/bin/smoke-browser %%u\nMimeType=x-scheme-handler/https;\n' "$WORK" \
    >"$WORK/data/applications/smoke-browser.desktop"
  printf '[Default Applications]\nx-scheme-handler/https=smoke-browser.desktop\n' >"$WORK/config/mimeapps.list"
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
# The visible items, and every separator: the menu shows or hides those only as it opens or closes.
MENU="$B.menu._getMenuItems().filter(i => i.actor.visible || i.label?.text === '').flatMap(i => i._getMenuItems ? i._getMenuItems().map(j => j.label.text + (j.get_n_children() > 2 ? ' [warning icon]' : '')) : [i.label.text || '---'])"
PANEL="({recordingStyle: $B.has_style_class_name('screen-recording-indicator'), timerVisible: $B.get_first_child().get_child_at_index(1).visible})"
PILL="({recording: $B.has_style_class_name('screen-recording-indicator'), paused: $B.has_style_class_name('screen-sharing-indicator'), icon: $B.get_first_child().get_child_at_index(0).icon_name})"
RECORDING_ITEMS='"Finish","Discard…",'
# The confirmation dialogs on screen (the Shell keeps hidden ones of its own); of the open ones
# (0 is the Shell's State.OPENED), the title and the buttons.
DIALOGS="Main.layoutManager.modalDialogGroup.get_children().filter(d => d.buttonLayout && d.visible)"
DIALOG="$DIALOGS.filter(d => d.state === 0).map(d => [d.contentLayout.get_first_child().title, ...d.buttonLayout.get_children().map(b => b.label)])"
FOCUS="global.stage.key_focus?.label"
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
INSTALL_URL=https://github.com/jfolcini/minutas#install
# Polls, because the browser starts asynchronously.
expect_opened() {
  for _ in $(seq 40); do
    [ "$(grep -cxF "$INSTALL_URL" "$WORK/browser.log" 2>/dev/null)" = "$1" ] && echo "  ok: opened $INSTALL_URL ($1)" && return
    sleep 0.25
  done
  fail "expected $INSTALL_URL $1 time(s) in $WORK/browser.log"
}
expect_log() { check "ran minutas $1" grep -qxF -- "$1" "$WORK/minutas.log"; }
# Polls, because the command starts asynchronously.
expect_ran() {
  for _ in $(seq 40); do
    grep -qxF -- "$1" "$WORK/minutas.log" && echo "  ok: ran minutas $1" && return
    sleep 0.25
  done
  fail "expected minutas $1 in $WORK/minutas.log"
}
expect_not_ran() { check "did not run minutas $1" bash -c '! grep -qxF -- "$1" "$2"' _ "$1" "$WORK/minutas.log"; }
# A key pressed and released on a keyboard of the test's own.
press() {
  ev "(() => { const {Clutter} = imports.gi;
    globalThis.__smokeKeyboard ??= Clutter.get_default_backend().get_default_seat().create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
    const time = Clutter.get_current_event_time();
    __smokeKeyboard.notify_keyval(time, Clutter.KEY_$1, Clutter.KeyState.PRESSED);
    __smokeKeyboard.notify_keyval(time, Clutter.KEY_$1, Clutter.KeyState.RELEASED); })()" >/dev/null
}
click() { ev "$B.menu._getMenuItems().flatMap(i => i._getMenuItems ? i._getMenuItems() : [i]).find(i => i.label.text.startsWith('$1')).activate(null)" >/dev/null; }
write_status() { mkdir -p "$DATA" && printf '%s' "$1" >"$DATA/.status.tmp" && mv "$DATA/.status.tmp" "$DATA/status.json"; }
# A recent recording; an eighth argument is its progress.
recent() { echo "{\"id\":\"$1\",\"name\":\"$2\",\"started_at\":\"$3\",\"duration_seconds\":$4,\"incomplete\":$5,\"transcribe\":$6,\"failed\":$7${8:+,\"progress\":$8}}"; }
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
expect "$MENU" '["Record","---","---","Recordings","Settings"]'

echo "== Record"
click Record
expect "$MENU" "[\"Pause\",$RECORDING_ITEMS"
expect_log "record start"
expect "$PANEL" '{"recordingStyle":true,"timerVisible":true}'
expect "$NOTES" "Tell participants you're recording"
expect "Main.messageTray.getSources().filter(s => s.title === 'Minutas').flatMap(s => s.notifications).find(n => n.title.startsWith('Tell')).urgency" "'3'" # CRITICAL
first_tick=$(ev "$TIMER")
sleep 2
check "timer advances from $first_tick" test "$(ev "$TIMER")" != "$first_tick"
expect "$PILL" '{"recording":true,"paused":false,"icon":"audio-input-microphone-symbolic"}'
screenshot menu-recording.png

echo "== Pause: the indicator says so, and the timer stands still"
click Pause
expect "$MENU" "[\"Resume\",$RECORDING_ITEMS"
expect_log "record pause"
expect "$PILL" '{"recording":false,"paused":true,"icon":"media-playback-pause-symbolic"}'
expect "$PANEL" '{"recordingStyle":false,"timerVisible":true}'
paused_tick=$(ev "$TIMER")
sleep 2
check "timer stays at $paused_tick" test "$(ev "$TIMER")" = "$paused_tick"
screenshot menu-paused.png

echo "== Resume"
click Resume
expect "$MENU" "[\"Pause\",$RECORDING_ITEMS"
expect_log "record resume"
expect "$PILL" '{"recording":true,"paused":false,"icon":"audio-input-microphone-symbolic"}'

echo "== Finish"
click Finish
expect_ran "record stop"
expect "$DIALOG" "'[]'"
expect "$MENU" '["Record","---","Meeting · '
expect "$MENU" ' · 0:07 · transcribing…","Cancel transcription","---","Recordings","Settings"]'
expect "$PANEL" '{"recordingStyle":false,"timerVisible":false}'
read -r id at <"$WORK/active"
meeting=$(recent "$id" Meeting "$at" 7 false '"succeeded"' '["summarize"]')

echo "== a running transcription says how far it has got, on the same item"
speakers() { recent "$id" Meeting "$at" 7 false '"running"' '[]' "{\"phase\":\"identifying speakers\",\"percent\":$1}"; }
ITEM="$B.menu._getMenuItems().find(i => i._getMenuItems)._getMenuItems()[0]"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(speakers 40)]}"
expect "$MENU" ' · 0:07 · identifying speakers… 40%"'
ev "$ITEM._smokeMark = true" >/dev/null
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(speakers 41)]}"
expect "$MENU" ' · 0:07 · identifying speakers… 41%"'
expect "$ITEM._smokeMark === true" "'true'"

echo "== a queued or running transcription can be cancelled, and a cancelled or failed one run again"
click Cancel
expect_ran "transcribe --cancel $id"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "$id" Meeting "$at" 7 false '"cancelled"' '[]')]}"
expect "$MENU" ' · 0:07 · transcription cancelled","Transcribe again","---","Recordings","Settings"]'
screenshot menu-cancelled.png
click Transcribe
expect_ran "transcribe $id"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "$id" Meeting "$at" 7 false '"failed"' '["transcribe"]')]}"
expect "$MENU" ' · 0:07 · transcription failed [warning icon]","Transcribe again","---","Recordings","Settings"]'
expect "$NOTES" "Transcribe failed | Meeting"

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

echo "== Discard asks first, with Cancel focused: Enter and Escape leave the recording going"
click Record
expect "$MENU" "[\"Pause\",$RECORDING_ITEMS"
for key in Return Escape; do
  click Discard
  expect "$DIALOG" '[["Discard the recording?","Cancel","Discard"]]'
  expect "$FOCUS" "'\"Cancel\"'"
  expect "${DIALOGS}[0].buttonLayout.get_last_child().style" '#c01c28'
  gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell/Screenshot \
    --method org.gnome.Shell.Screenshot.Screenshot false false "$OUT/dialog-discard.png" >/dev/null || fail "screenshot dialog-discard.png"
  press "$key"
  expect "$DIALOG" "'[]'"
done
sleep 1
expect_not_ran "record discard"
expect "$MENU" "[\"Pause\",$RECORDING_ITEMS"

echo "== Discard, confirmed from the keyboard"
click Discard
expect "$DIALOG" '[["Discard the recording?","Cancel","Discard"]]'
press Tab
expect "$FOCUS" "'\"Discard\"'"
press Return
expect_ran "record discard"
expect "$DIALOG" "'[]'"
expect "$MENU" '["Record","---","---","Recordings","Settings"]'
expect "$PANEL" '{"recordingStyle":false,"timerVisible":false}'

echo "== the recorder dies"
now=$(date --iso-8601=seconds) id2=$(date +%Y-%m-%dT%H-%M-%S)
write_status "{\"version\":1,\"recording\":{\"id\":\"$id2\",\"started_at\":\"$now\"},\"recent\":[$meeting]}"
expect "$MENU" "[\"Pause\",$RECORDING_ITEMS"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "$id2" Standup "$now" 3 true null '[]'),$meeting]}"
expect "$NOTES" "Recording stopped unexpectedly | Standup"
expect "$MENU" '["Record","---","Standup · '

echo "== a failing command shows stderr's last line"
touch "$WORK/fail-next"
click Record
expect "$NOTES" "Couldn't start recording | error: already recording"

echo "== no minutas on the PATH or in ~/.local/bin: the notification and the menu open the install instructions"
mv "$WORK/bin/minutas" "$WORK/minutas.away"
click Record
expect "$NOTES" "Minutas isn't installed"
ev "Main.messageTray.getSources().find(s => s.title === 'Minutas').notifications.find(n => n.title.startsWith('Minutas isn')).activate()" >/dev/null
expect_opened 1
ev "$B.menu.open()" >/dev/null
expect "$MENU" '["Install Minutas…","---","---"]'
# Its separators hidden too, on this first opening: closing the menu hides them anyway.
expect "$B.menu._getMenuItems().filter(i => i.actor.visible).length" "'1'"
screenshot menu-not-installed.png
click Install
expect_opened 2
# Back in ~/.local/bin, where install.sh puts it and the Shell's PATH often doesn't reach.
mkdir -p "$HOME/.local/bin"
mv "$WORK/minutas.away" "$HOME/.local/bin/minutas"
ev "$B.menu.open()" >/dev/null
expect "$MENU" '["Record","---","Standup · '
ev "$B.menu.close()" >/dev/null

echo "== a recent recording and Recordings open the recordings; Settings, the settings"
click Standup
click Recordings
click Settings
sleep 1
expect_log "history --recording $id2"
expect_log "history"
expect_log "settings"

echo "== disable removes everything; enable announces only what changed meanwhile (the lock screen)"
gnome-extensions disable "$UUID"
expect "$B === undefined" "'true'"
expect "Main.messageTray.getSources().filter(s => s.title === 'Minutas').length" "'0'"
write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "$id2" Standup "$now" 3 true '"succeeded"' '[]'),$meeting]}"
gnome-extensions enable "$UUID"
expect "$MENU" '["Record","---","Standup · '
expect "$NOTES" "'[\"Transcript ready | Standup, ${now:0:10} ${now:11:5}\"]'"

echo "== every enable runs minutas repair once (a crash or a logout may have left something open)"
repairs=$(grep -cxF repair "$WORK/minutas.log")
gnome-extensions disable "$UUID"
expect "$B === undefined" "'true'"
gnome-extensions enable "$UUID"
expect "$MENU" '["Record","---","Standup · '
sleep 1
check "enable ran minutas repair" test "$(grep -cxF repair "$WORK/minutas.log")" -eq $((repairs + 1))

echo "== soak: status churn, notifications, the timer and disable/enable cycles leave nothing behind"
gnome-extensions disable "$UUID"
expect "$B === undefined" "'true'"
# From here on every GLib source and signal handler created with the extension's code on the
# stack is recorded, the Shell's on its behalf included, each with a test of whether it is still
# there; so are the extension's objects, held weakly. Whatever is still there once disabled leaked.
ev "const {GLib, GObject} = imports.gi; globalThis.__soak = [];
  const ours = () => new Error().stack.includes('/$UUID/');
  for (const name of ['timeout_add', 'timeout_add_seconds', 'idle_add']) {
    const original = GLib[name];
    GLib[name] = (...args) => {
      const id = original(...args);
      if (ours()) __soak.push(['GLib source', 'GLib source', () => GLib.MainContext.default().find_source_by_id(id) !== null]);
      return id;
    };
  }
  for (const name of ['connect', 'connect_after']) {
    const original = GObject.Object.prototype[name];
    GObject.Object.prototype[name] = function (...args) {
      const id = original.apply(this, args);
      if (ours()) {
        const ref = new WeakRef(this);
        // Disposing drops an object's handlers; checking a disposed one logs a critical.
        const live = () => ref.deref() && !GObject.Object.prototype.toString.call(ref.deref()).includes('DISPOSED');
        __soak.push(['signal handler', args[0] + ' handler on ' + this.constructor.name, () => live() && GObject.signal_handler_is_connected(ref.deref(), id)]);
      }
      return id;
    };
  }" >/dev/null
TRACK="(() => { const e = Main.extensionManager.lookup('$UUID').stateObj;
  const items = e._button.menu._getMenuItems().flatMap(i => [i, ...(i._getMenuItems?.() ?? [])]);
  const objects = [['button', e._button], ['menu', e._button.menu], ['notification source', e._source], ['dialog', e._dialog],
    ...(e._source?.notifications ?? []).map(n => ['notification', n]), ...items.map(i => ['menu item', i])];
  for (const [kind, object] of objects.filter(([, o]) => o)) { const ref = new WeakRef(object); __soak.push([kind, kind, () => ref.deref() !== undefined]); }
  const monitor = new WeakRef(e._monitor); __soak.push(['file monitor', 'file monitor', () => monitor.deref()?.is_cancelled() === false]); })()"
count() { echo "Object.entries($1.reduce((n, kind) => ({...n, [kind]: (n[kind] ?? 0) + 1}), {})).map(([kind, n]) => n + ' ' + kind).join(', ') || 'none'"; }
# What is still there, by detail; the argument narrows it to one category.
there() { echo "imports.system.gc(); $(count "__soak.filter(([category, , there]) => ${1:-true} && there()).map(([, detail]) => detail)")"; }
baseline_rss=$(awk '/^VmRSS/ {print $2}' "/proc/$shell_pid/status")
# Quiet, unlike expect: the soak waits on the menu many times.
until_menu() {
  for _ in $(seq 200); do
    case "$(ev "$MENU")" in *"$1"*) return ;; esac
    sleep 0.05
  done
  fail "expected '$1' in $(ev "$MENU")"
}

write_status '{"version":1,"recording":null,"recent":[]}'
gnome-extensions enable "$UUID"
until_menu '["Record","---","---","Recordings","Settings"]'
echo "  idle: no timer"
expect "$(there "category === 'GLib source'")" '"none"'
entries=()
for i in $(seq 5); do
  history=$(
    IFS=,
    echo "${entries[*]}"
  )
  now=$(date --iso-8601=seconds)
  write_status "{\"version\":1,\"recording\":{\"id\":\"soak-$i\",\"started_at\":\"$now\"},\"recent\":[$history]}"
  until_menu '["Pause",'
  if [ "$i" -eq 1 ]; then
    echo "  recording: the timer"
    expect "$(there "category === 'GLib source'")" '"1 GLib source"'
  fi
  # A confirmation dialog left open when the extension is disabled goes with it.
  click Discard
  ev "$TRACK" >/dev/null
  gnome-extensions disable "$UUID"
  if [ "$i" -eq 1 ]; then
    echo "  disabled with a dialog open: it is gone"
    expect "$DIALOGS.length" "'0'"
  fi
  gnome-extensions enable "$UUID"
  until_menu '["Pause",'
  incomplete=$(recent "soak-$i" "Soak $i" "$now" 60 true '"queued"' '[]')
  write_status "{\"version\":1,\"recording\":null,\"recent\":[$incomplete${history:+,$history}]}"
  until_menu "[\"Record\",\"---\",\"Soak $i · "
  ev "$TRACK" >/dev/null
  finished=$(recent "soak-$i" "Soak $i" "$now" 60 true '"succeeded"' '["summarize","publish_gitlab"]')
  write_status "{\"version\":1,\"recording\":null,\"recent\":[$finished${history:+,$history}]}"
  until_menu "\"Soak $i · ${now:0:10} ${now:11:5} · 1:00 [warning icon]\""
  ev "$TRACK" >/dev/null
  # Recent-list churn: entries come and go, the newest four stay.
  entries=("$finished" "${entries[@]:0:3}")
done
# Overlapping reads: replaces faster than the Shell reads them.
for i in $(seq 100); do write_status "{\"version\":1,\"recording\":null,\"recent\":[$(recent "burst-$i" "Burst $i" "$now" 1 false null '[]')]}"; done
until_menu '"Burst 100 · '
ev "$TRACK" >/dev/null
echo "  recorded: $(ev "$(count "__soak.map(([category]) => category)")")"
gnome-extensions disable "$UUID"
echo "  disabled again: none of them is left"
expect "$(there)" '"none"'
after_rss=$(awk '/^VmRSS/ {print $2}' "/proc/$shell_pid/status")
check "gnome-shell's RSS $((baseline_rss / 1024)) MB -> $((after_rss / 1024)) MB, within 32 MB" test $((after_rss - baseline_rss)) -lt 32768

echo "== shell log"
if grep -E 'JS ERROR|JS WARNING' "$OUT/shell.log"; then fail "JS errors in $OUT/shell.log"; else echo "  ok: no JS errors"; fi

echo "$failures failed"
[ "$failures" -eq 0 ]
