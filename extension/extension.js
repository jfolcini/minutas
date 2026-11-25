import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
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
    recentLabel,
} from './status.js';

const ICON = 'audio-input-microphone-symbolic';
// The Shell's own screencast indicator style: a red pill around the icon and timer.
const RECORDING_STYLE = 'screen-recording-indicator';
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
        this._recentItems = [];
        this._recentShape = null;

        this._button = new PanelMenu.Button(0.0, this.metadata.name);
        const box = new St.BoxLayout({style_class: 'panel-status-indicators-box'});
        box.add_child(new St.Icon({icon_name: ICON, style_class: 'system-status-icon'}));
        this._elapsedLabel = new St.Label({y_align: Clutter.ActorAlign.CENTER, visible: false});
        box.add_child(this._elapsedLabel);
        this._button.add_child(box);

        const menu = this._button.menu;
        // Connected before the items are added: the menu hides its separators on the same signal,
        // after looking at what this shows.
        this._menuOpenId = menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._showInstallState();
        });
        this._installItem = menu.addAction('Install Minutas…', () => this._openInstallPage());
        this._recordItem = menu.addAction('Record', () => this._toggleRecording());
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._recentSection = new PopupMenu.PopupMenuSection();
        menu.addMenuItem(this._recentSection);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._commandItems = [
            this._recordItem,
            this._recentSection.actor,
            menu.addAction('Recordings', () => this._run(['history'], "Couldn't open the recordings")),
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
        this._elapsedLabel = null;
        this._installItem = null;
        this._recordItem = null;
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
                if (e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
                if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_FOUND)) {
                    console.warn(`Minutas: cannot read ${file.get_path()}: ${e.message}`);
                    return;
                }
                status = EMPTY_STATUS;
            }
            // Reads overlap when one replace fires several events; only the newest counts.
            if (read !== this._statusReads)
                return;
            if (status === null) {
                console.warn(`Minutas: keeping the last status; ${file.get_path()} is not valid`);
                return;
            }
            for (const {recordingId, title, body} of notificationsFor(this._status, status))
                this._notify(title, body, recordingId);
            this._status = status;
            this._render();
        });
    }

    _render() {
        const {recording, recent} = this._status;
        this._recordItem.label.text = recording ? 'Stop' : 'Record';
        this._elapsedLabel.visible = recording !== null;
        if (recording) {
            this._button.add_style_class_name(RECORDING_STYLE);
            this._showElapsed();
            this._timerId ??= GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
                this._showElapsed();
                return GLib.SOURCE_CONTINUE;
            });
        } else {
            this._button.remove_style_class_name(RECORDING_STYLE);
            this._stopTimer();
        }

        // While a transcription runs only its label changes, every few seconds: set in place, the
        // items of an open menu keep the pointer's hover and the keyboard's focus.
        const shape = recent.map(entry => `${entry.id} ${hasWarning(entry)}`).join('\n');
        if (shape === this._recentShape) {
            recent.forEach((entry, i) => (this._recentItems[i].label.text = recentLabel(entry)));
            return;
        }
        this._recentShape = shape;
        this._recentSection.removeAll();
        this._recentItems = recent.map(entry => {
            const item = this._recentSection.addAction(recentLabel(entry), () => this._openHistory(entry.id));
            if (hasWarning(entry))
                item.add_child(new St.Icon({icon_name: 'dialog-warning-symbolic', style_class: 'popup-menu-icon'}));
            return item;
        });
    }

    _showElapsed() {
        this._elapsedLabel.text = formatClock(elapsedSeconds(this._status.recording.started_at, Date.now()));
    }

    _stopTimer() {
        if (this._timerId) {
            GLib.Source.remove(this._timerId);
            this._timerId = null;
        }
    }

    _toggleRecording() {
        if (this._status.recording) {
            this._run(['record', 'stop'], "Couldn't stop recording");
        } else {
            this._run(['record', 'start'], "Couldn't start recording",
                // CRITICAL shows through Do Not Disturb, which is often on in exactly the
                // meetings this reminder is for; it stays up until dismissed.
                () => this._notify("Tell participants you're recording", null, null,
                    MessageTray.Urgency.CRITICAL));
        }
    }

    _openHistory(recordingId) {
        this._run(['history', '--recording', recordingId], "Couldn't open the recording");
    }

    _findCommand() {
        // The Shell's PATH often lacks ~/.local/bin, where install.sh puts the command.
        return GLib.find_program_in_path('minutas') ??
            GLib.find_program_in_path(GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'minutas']));
    }

    // Checked each time the menu opens, so installing takes effect without logging out.
    _showInstallState() {
        const installed = this._findCommand() !== null;
        this._installItem.visible = !installed;
        for (const item of this._commandItems)
            item.visible = installed;
    }

    _openInstallPage() {
        Gio.AppInfo.launch_default_for_uri(INSTALL_URL, global.create_app_launch_context(0, -1));
    }

    _run(args, failureTitle, onSuccess) {
        const executable = this._findCommand();
        if (!executable) {
            this._notify("Minutas isn't installed", 'No minutas command on the PATH or in ~/.local/bin; click for how to install it', null)
                .connect('activated', () => this._openInstallPage());
            return;
        }
        let proc;
        try {
            proc = Gio.Subprocess.new([executable, ...args],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE);
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
            if (proc.get_successful())
                onSuccess?.();
            else
                this._notify(failureTitle, lastLine(stderr) || `minutas ${args.join(' ')} exited with an error`, null);
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
        const notification = new MessageTray.Notification({source: this._source, title, body, urgency});
        if (recordingId)
            notification.connect('activated', () => this._openHistory(recordingId));
        this._source.addNotification(notification);
        return notification;
    }
}
