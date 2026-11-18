"""The calendar event a recording overlaps, from Evolution Data Server (EDS).

EDS holds the calendars of GNOME Online Accounts, Nextcloud's among them, and the local ones, and
expands repeating meetings into their occurrences. It is reached through Ubuntu's PyGObject, so
the lookup runs only on the system Python; anywhere else, or with EDS out of reach, it finds
nothing and says why: a calendar problem never fails a recording.
"""

import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

# Stop waits for the lookup, which runs in the recorder unit's ExecStopPost, and systemd kills
# that after the unit's stop timeout, before it queues the transcription. A calendar server that
# takes connections and never answers holds the first open of its calendar far longer than that.
LOOKUP_SECONDS = 1
# (guint32) -1: answer from the calendar's cache at once, never waiting for its server.
NO_WAIT = 0xFFFFFFFF
# EDS picks the events of a span reading a time with no zone as UTC, and such a time can be off
# by a day's worth of offsets; chosen measures the real overlap.
MARGIN_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Event:
    name: str
    attendees: tuple[str, ...]
    # The occurrence's span, in seconds since the epoch.
    start: int
    end: int


def warn(reason: str) -> None:
    sys.stderr.write(f"calendar: {reason}\n")


def attendee_name(cn: str | None, address: str) -> str:
    """The attendee's name, else their address before the @: never the whole address."""
    if cn is not None and cn.strip():
        return cn.strip()
    # The scheme comes as "mailto:" or as "MAILTO:".
    return address.rpartition(":")[2].partition("@")[0]


def chosen(events: Sequence[Event], start: int, end: int) -> Event | None:
    """The event that overlaps the span longest; of two that overlap as long, the earlier one.

    A recording that runs into the next meeting, or started in the last minutes of the one
    before, still gets the meeting it mostly holds.
    """

    def overlap(event: Event) -> int:
        return min(event.end, end) - max(event.start, start)

    overlapping = [event for event in events if overlap(event) > 0]
    return max(overlapping, key=lambda event: (overlap(event), -event.start), default=None)


def lookup(start: datetime, stop: datetime) -> Event | None:
    try:
        import gi  # noqa: PLC0415 - Ubuntu's, which other Pythons lack; recording works without it

        gi.require_version("ECal", "2.0")
        gi.require_version("EDataServer", "1.2")
        gi.require_version("ICalGLib", "3.0")
        from gi.repository import ECal, EDataServer, Gio, GLib, ICalGLib  # noqa: PLC0415
    except (ImportError, ValueError) as error:
        warn(f"no lookup, PyGObject or Evolution Data Server's typelibs are missing: {error}")
        return None
    begin, end = int(start.timestamp()), int(stop.timestamp())
    events: list[Event] = []

    def add(
        component: ICalGLib.Component, starts: ICalGLib.Time, ends: ICalGLib.Time, *_: object
    ) -> bool:
        # A birthday or a holiday lasts all day; a meeting has a time.
        if not starts.is_date():
            # It takes the component it is given, so it gets a copy of EDS's.
            parsed = cast(
                "ECal.Component", ECal.Component.new_from_icalcomponent(component.clone())
            )
            # An invitation from Outlook lists its organizer apart from the attendees.
            people = [parsed.get_organizer(), *(parsed.get_attendees() or [])]
            names = (
                attendee_name(person.get_cn(), person.get_value() or "")
                for person in people
                if person is not None
            )
            events.append(
                Event(
                    name=component.get_summary() or "",
                    attendees=tuple(dict.fromkeys(names)),
                    start=starts.as_timet_with_zone(starts.get_timezone()),
                    end=ends.as_timet_with_zone(ends.get_timezone()),
                )
            )
        return True

    # None for a zone libical doesn't know; UTC is what EDS would read the times in anyway.
    local_zone = ECal.util_get_system_timezone() or ICalGLib.Timezone.get_utc_timezone()
    cancellable = Gio.Cancellable()
    deadline = threading.Timer(LOOKUP_SECONDS, cancellable.cancel)
    deadline.start()
    try:
        registry = EDataServer.SourceRegistry.new_sync(cancellable)
        for source in registry.list_enabled(EDataServer.SOURCE_EXTENSION_CALENDAR):
            try:
                client = ECal.Client.connect_sync(
                    source, ECal.ClientSourceType.EVENTS, NO_WAIT, cancellable
                )
                client = cast("ECal.Client", client)
                # Else EDS reads a time with no zone, which some events have, as UTC.
                client.set_default_timezone(local_zone)
                client.generate_instances_sync(
                    begin - MARGIN_SECONDS, end + MARGIN_SECONDS, cancellable, add
                )
            except GLib.Error as error:
                warn(f"{source.get_display_name()} skipped: {error.message}")
    except GLib.Error as error:
        warn(f"no lookup, Evolution Data Server is out of reach: {error.message}")
        return None
    finally:
        deadline.cancel()
    return chosen(events, begin, end)
