import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Dialog from 'resource:///org/gnome/shell/ui/dialog.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {
    EMPTY_STATUS,
    elapsedSeconds,
    formatClock,
    hasWarning,
    lastLine,
    notificationsFor,
    parseStatus,
    recentControls,
    recentLabel,
    silenceReminder,
} from './status.js';

const ICON = 'audio-input-microphone-symbolic';
const PAUSED_ICON = 'media-playback-pause-symbolic';
// The Shell's own screencast and screen sharing indicator styles: a pill around the icon and
// timer, red while it records and orange while it is paused.
const RECORDING_STYLE = 'screen-recording-indicator';
const PAUSED_STYLE = 'screen-sharing-indicator';
// A transcription queued or running, the work that can slow the computer down: libadwaita's yellow.
const TRANSCRIBING_STYLE =
    'width: 8px; height: 8px; border-radius: 4px; background-color: #f6d32d;';
// The Shell's theme has no destructive button: libadwaita's red, readable on its light and dark.
const DESTRUCTIVE_STYLE = 'background-color: #c01c28; color: #ffffff;';
const INSTALL_URL = 'https://github.com/jfolcini/minutas#install';

export default class MinutasExtension extends Extension {
    enable() {
        this._cancellable = new Gio.Cancellable();
        // Kept across disable: the Shell disables extensions while the screen is locked, and
        // what happened meanwhile (a transcript, a failed run) is announced after unlocking.
        this._status ??= EMPTY_STATUS;
        this._statusReads = 0;
        this._timerId = null;
        this._source = null;
        this._dialog = null;
        this._installed = false;
        this._recentItems = [];
        this._recentShape = null;

        this._button = new PanelMenu.Button(0.0, this.metadata.name);
        const box = new St.BoxLayout({style_class: 'panel-status-indicators-box'});
        this._icon = new St.Icon({icon_name: ICON, style_class: 'system-status-icon'});
        box.add_child(this._icon);
        this._elapsedLabel = new St.Label({y_align: Clutter.ActorAlign.CENTER, visible: false});
        box.add_child(this._elapsedLabel);
        this._transcribingDot = new St.Widget({
            style: TRANSCRIBING_STYLE,
            y_align: Clutter.ActorAlign.CENTER,
            visible: false,
        });
        box.add_child(this._transcribingDot);
        this._button.add_child(box);

        const menu = this._button.menu;
        // Connected before the items are added: the menu hides its separators on the same signal,
        // after looking at what this shows.
        this._menuOpenId = menu.connect('open-state-changed', (_menu, open) => {
            if (open) this._showInstallState();
        });
        this._installItem = menu.addAction('Install Minutas…', () => this._openInstallPage());
        this._recordItem = menu.addAction('Record', () => this._startRecording());
        this._pauseItem = menu.addAction('Pause', () => this._togglePause());
        this._recordingItems = [this._pauseItem, menu.addAction('Finish', () => this._finish())];
        this._discardItem = menu.addAction('Discard…', () => this._askToDiscard());
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._recentSection = new PopupMenu.PopupMenuSection();
        menu.addMenuItem(this._recentSection);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._commandItems = [
            this._recentSection.actor,
            menu.addAction('Recordings', () =>
                this._run(['history'], "Couldn't open the recordings")
            ),
            menu.addAction('Settings', () => this._run(['settings'], "Couldn't open the settings")),
        ];
        this._showInstallState();
        Main.panel.addToStatusArea(this.uuid, this._button);

        const path = GLib.build_filenamev([GLib.get_user_data_dir(), 'minutas', 'status.json']);
        this._statusFile = Gio.File.new_for_path(path);
        // WATCH_MOVES: the program replaces status.json by renaming a temporary file onto it.
        this._monitor = this._statusFile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, null);
        this._monitorId = this._monitor.connect('changed', () => this._readStatus());

        this._render();
        this._readStatus();
        // Enable runs at login, after a crash or a logout may have left a recording open or
        // transcriptions queued, some on recordings older than status.json lists.
        this._run(['repair'], "Couldn't start transcribing");
    }

    disable() {
        this._cancellable.cancel();
        this._monitor.disconnect(this._monitorId);
        this._monitor.cancel();
        this._stopTimer();
        this._source?.destroy();
        // The silence reminder went with the source, so a silence still going once the screen
        // is unlocked reminds again.
        if (this._status.recording)
            this._status = {
                ...this._status,
                recording: {...this._status.recording, silent_since: undefined},
            };
        this._dialog?.destroy();
        // The Shell ties handlers between the panel, the menu and the section to owners without a
        // destroy signal, so destroy() leaves them connected, and they would keep every disabled
        // indicator alive.
        const menu = this._button.menu;
        menu.disconnect(this._menuOpenId);
        menu.disconnectObject(Main.panel);
        menu.disconnectObject(this._recentSection);
        this._recentSection.disconnectObject(menu);
        this._button.destroy();
        this._cancellable = null;
        this._monitor = null;
        this._statusFile = null;
        this._button = null;
        this._icon = null;
        this._elapsedLabel = null;
        this._transcribingDot = null;
        this._installItem = null;
        this._recordItem = null;
        this._pauseItem = null;
        this._recordingItems = null;
        this._discardItem = null;
        this._recentSection = null;
        this._recentItems = null;
        this._commandItems = null;
    }

    _readStatus() {
        const read = ++this._statusReads;
        this._statusFile.load_contents_async(this._cancellable, (file, result) => {
            let status;
            try {
                const [, contents] = file.load_contents_finish(result);
                status = parseStatus(new TextDecoder().decode(contents));
            } catch (e) {
                if (e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED)) return;
                if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_FOUND)) {
                    console.warn(`Minutas: cannot read ${file.get_path()}: ${e.message}`);
                    return;
                }
                status = EMPTY_STATUS;
            }
            // Reads overlap when one replace fires several events; only the newest counts.
            if (read !== this._statusReads) return;
            if (status === null) {
                console.warn(`Minutas: keeping the last status; ${file.get_path()} is not valid`);
                return;
            }
            for (const {recordingId, title, body} of notificationsFor(this._status, status))
                this._notify(title, body, recordingId);
            const reminder = silenceReminder(this._status, status, Date.now());
            // CRITICAL shows through Do Not Disturb, often still on from the meeting that was
            // forgotten, and stays up until dismissed.
            if (reminder) {
                this._notify(
                    reminder.title,
                    reminder.body,
                    null,
                    MessageTray.Urgency.CRITICAL
                ).addAction('Finish', () => this._finish());
            }
            this._status = status;
            this._render();
        });
    }

    _render() {
        const {recording, recent, transcribing} = this._status;
        const paused = Boolean(recording?.paused_at);
        this._showItems();
        this._transcribingDot.visible = transcribing;
        this._pauseItem.label.text = paused ? 'Resume' : 'Pause';
        this._icon.icon_name = paused ? PAUSED_ICON : ICON;
        this._elapsedLabel.visible = recording !== null;
        const [pill, other] = paused
            ? [PAUSED_STYLE, RECORDING_STYLE]
            : [RECORDING_STYLE, PAUSED_STYLE];
        this._button.remove_style_class_name(other);
        if (recording) {
            this._button.add_style_class_name(pill);
            this._showElapsed();
            this._timerId ??= GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                this._showElapsed();
                return GLib.SOURCE_CONTINUE;
            });
        } else {
            this._button.remove_style_class_name(pill);
            this._stopTimer();
        }

        // While a transcription runs only its label changes, every few seconds: set in place, the
        // items of an open menu keep the pointer's hover and the keyboard's focus.
        const shape = recent
            .map(
                entry =>
                    `${entry.id} ${hasWarning(entry)} ${recentControls(entry).map(control => control.label)}`
            )
            .join('\n');
        if (shape === this._recentShape) {
            recent.forEach((entry, i) => (this._recentItems[i].label.text = recentLabel(entry)));
            return;
        }
        this._recentShape = shape;
        this._recentSection.removeAll();
        this._recentItems = recent.map(entry => {
            const item = this._recentSection.addAction(recentLabel(entry), () =>
                this._openHistory(entry.id)
            );
            if (hasWarning(entry))
                item.add_child(
                    new St.Icon({
                        icon_name: 'dialog-warning-symbolic',
                        style_class: 'popup-menu-icon',
                    })
                );
            for (const control of recentControls(entry)) {
                // Under its recording and set in from it, by the room an ornament would take.
                this._recentSection
                    .addAction(control.label, () => this._run(control.args, control.failureTitle))
                    .setOrnament(PopupMenu.Ornament.NONE);
            }
            return item;
        });
    }

    _showElapsed() {
        this._elapsedLabel.text = formatClock(elapsedSeconds(this._status.recording, Date.now()));
    }

    _stopTimer() {
        if (this._timerId) {
            GLib.Source.remove(this._timerId);
            this._timerId = null;
        }
    }

    _startRecording() {
        this._run(
            ['record', 'start'],
            "Couldn't start recording",
            // CRITICAL shows through Do Not Disturb, which is often on in exactly the
            // meetings this reminder is for; it stays up until dismissed. A warning, such as echo
            // cancellation not starting, comes with it while there is still time to act on it.
            warning =>
                this._notify(
                    "Tell participants you're recording",
                    warning || null,
                    null,
                    MessageTray.Urgency.CRITICAL
                )
        );
    }

    _finish() {
        this._run(['record', 'stop'], "Couldn't finish the recording");
    }

    _togglePause() {
        if (this._status.recording?.paused_at)
            this._run(['record', 'resume'], "Couldn't resume the recording");
        else this._run(['record', 'pause'], "Couldn't pause the recording");
    }

    // Escape, Cancel and Enter leave the recording going: Cancel, added first, has the
    // keyboard's focus, and neither button is the default.
    _askToDiscard() {
        const dialog = new ModalDialog.ModalDialog();
        dialog.contentLayout.add_child(
            new Dialog.MessageDialogContent({
                title: 'Discard the recording?',
                description: 'It stops and goes to the Trash, without a transcript.',
            })
        );
        dialog.addButton({label: 'Cancel', action: () => dialog.close(), key: Clutter.KEY_Escape});
        dialog
            .addButton({
                label: 'Discard',
                action: () => {
                    dialog.close();
                    this._run(['record', 'discard'], "Couldn't discard the recording");
                },
            })
            .set_style(DESTRUCTIVE_STYLE);
        // It destroys itself once closed.
        dialog.connect('destroy', () => {
            this._dialog = null;
        });
        this._dialog = dialog;
        dialog.open();
    }

    _openHistory(recordingId) {
        this._run(['history', '--recording', recordingId], "Couldn't open the recording");
    }

    _findCommand() {
        // The Shell's PATH often lacks ~/.local/bin, where install.sh puts the command.
        return (
            GLib.find_program_in_path('minutas') ??
            GLib.find_program_in_path(
                GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'minutas'])
            )
        );
    }

    // Checked each time the menu opens, so installing takes effect without logging out.
    _showInstallState() {
        this._installed = this._findCommand() !== null;
        this._showItems();
    }

    _showItems() {
        const recording = this._status.recording !== null;
        this._installItem.visible = !this._installed;
        this._recordItem.visible = this._installed && !recording;
        for (const item of this._recordingItems) item.visible = this._installed && recording;
        // The Trash would take what a continued recording held before its stop: Finish ends it.
        this._discardItem.visible =
            this._installed && recording && !this._status.recording.continued;
        for (const item of this._commandItems) item.visible = this._installed;
    }

    _openInstallPage() {
        Gio.AppInfo.launch_default_for_uri(INSTALL_URL, global.create_app_launch_context(0, -1));
    }

    _run(args, failureTitle, onSuccess) {
        const executable = this._findCommand();
        if (!executable) {
            this._notify(
                "Minutas isn't installed",
                'No minutas command on the PATH or in ~/.local/bin; click for how to install it',
                null
            ).connect('activated', () => this._openInstallPage());
            return;
        }
        let proc;
        try {
            proc = Gio.Subprocess.new(
                [executable, ...args],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE
            );
        } catch (e) {
            this._notify(failureTitle, e.message, null);
            return;
        }
        proc.communicate_utf8_async(null, this._cancellable, (_proc, result) => {
            let stderr;
            try {
                [, , stderr] = proc.communicate_utf8_finish(result);
            } catch (e) {
                if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    this._notify(failureTitle, e.message, null);
                return;
            }
            if (proc.get_successful()) onSuccess?.(lastLine(stderr));
            else
                this._notify(
                    failureTitle,
                    lastLine(stderr) || `minutas ${args.join(' ')} exited with an error`,
                    null
                );
        });
    }

    _notify(title, body, recordingId, urgency = MessageTray.Urgency.NORMAL) {
        if (!this._source) {
            this._source = new MessageTray.Source({title: this.metadata.name, iconName: ICON});
            // The tray destroys a source once its last notification is gone.
            this._source.connect('destroy', () => {
                this._source = null;
            });
            Main.messageTray.add(this._source);
        }
        const notification = new MessageTray.Notification({
            source: this._source,
            title,
            body,
            urgency,
        });
        if (recordingId) notification.connect('activated', () => this._openHistory(recordingId));
        this._source.addNotification(notification);
        return notification;
    }
}
