"""The calendar event a recording overlaps, from Evolution Data Server (EDS).

EDS holds the calendars of GNOME Online Accounts, Nextcloud's among them, and the local ones, and
expands repeating meetings into their occurrences. It is reached through Ubuntu's PyGObject, so
the lookup runs only on the system Python; anywhere else, or with EDS out of reach, it finds
nothing and says why: a calendar problem never fails a recording.
"""

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

# A calendar whose backend is still going online answers from its cache.
CONNECT_SECONDS = 1


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
        from gi.repository import ECal, EDataServer, GLib, ICalGLib  # noqa: PLC0415
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
            parsed = ECal.Component.new_from_icalcomponent(component.clone())
            attendees = cast("ECal.Component", parsed).get_attendees() or []
            events.append(
                Event(
                    name=component.get_summary() or "",
                    attendees=tuple(
                        attendee_name(attendee.get_cn(), attendee.get_value() or "")
                        for attendee in attendees
                    ),
                    start=starts.as_timet_with_zone(starts.get_timezone()),
                    end=ends.as_timet_with_zone(ends.get_timezone()),
                )
            )
        return True

    try:
        registry = EDataServer.SourceRegistry.new_sync(None)
    except GLib.Error as error:
        warn(f"no lookup, Evolution Data Server is out of reach: {error.message}")
        return None
    for source in registry.list_enabled(EDataServer.SOURCE_EXTENSION_CALENDAR):
        try:
            client = ECal.Client.connect_sync(
                source, ECal.ClientSourceType.EVENTS, CONNECT_SECONDS, None
            )
            cast("ECal.Client", client).generate_instances_sync(begin, end, None, add)
        except GLib.Error as error:
            warn(f"{source.get_display_name()} skipped: {error.message}")
    return chosen(events, begin, end)
