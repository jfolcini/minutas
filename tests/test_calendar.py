import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas import calendar
from minutas.calendar import Event

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
STOP = START + timedelta(minutes=30)
# 2026-11-18 13:00 UTC, which is 10:00 in Buenos Aires.
ONE_PM = 1795006800
HOUR = 3600
BUENOS_AIRES = "America/Argentina/Buenos_Aires"


@pytest.mark.parametrize(
    ("cn", "address", "name"),
    [
        ("Ana Díaz", "mailto:ana@example.com", "Ana Díaz"),
        (" Ana Díaz ", "mailto:ana@example.com", "Ana Díaz"),
        (None, "mailto:bob.ruiz@example.com", "bob.ruiz"),
        ("", "mailto:bob.ruiz@example.com", "bob.ruiz"),
        (None, "MAILTO:Carla@example.com", "Carla"),
    ],
)
def test_an_attendee_is_named_by_their_name_else_their_address_before_the_at(
    cn: str | None, address: str, name: str
) -> None:
    assert calendar.attendee_name(cn, address) == name


def meeting(name: str, start: int, end: int) -> Event:
    return Event(name=name, attendees=(), start=start, end=end)


def test_the_event_overlapping_the_recording_longest_is_chosen() -> None:
    standup = meeting("Standup", ONE_PM - HOUR, ONE_PM + 600)
    sync = meeting("Weekly sync", ONE_PM, ONE_PM + HOUR)

    assert calendar.chosen([standup, sync], ONE_PM, ONE_PM + 1800) == sync


def test_of_two_events_overlapping_as_long_the_earlier_one_is_chosen() -> None:
    later = meeting("Later", ONE_PM + 600, ONE_PM + HOUR)
    earlier = meeting("Earlier", ONE_PM, ONE_PM + 1200)

    assert calendar.chosen([later, earlier], ONE_PM + 600, ONE_PM + 1200) == earlier


def test_an_event_that_only_touches_the_recording_is_not_chosen() -> None:
    before = meeting("Before", ONE_PM - HOUR, ONE_PM)
    after = meeting("After", ONE_PM + 1800, ONE_PM + HOUR)

    assert calendar.chosen([before, after], ONE_PM, ONE_PM + 1800) is None


spans = st.tuples(st.integers(0, 100), st.integers(1, 100)).map(lambda s: (s[0], s[0] + s[1]))


@given(events=st.lists(spans, max_size=6), recording=spans)
def test_the_chosen_event_overlaps_the_recording_longest(
    events: list[tuple[int, int]], recording: tuple[int, int]
) -> None:
    candidates = [meeting(str(i), start, end) for i, (start, end) in enumerate(events)]
    begin, end = recording

    def overlap(event: Event) -> int:
        return min(event.end, end) - max(event.start, begin)

    found = calendar.chosen(candidates, begin, end)

    longest = max([0, *(overlap(event) for event in candidates)])
    if longest == 0:
        assert found is None
    else:
        assert found is not None
        assert overlap(found) == longest
        tied = [event.start for event in candidates if overlap(event) == longest]
        assert found.start == min(tied)


def test_without_pygobject_the_lookup_finds_nothing_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "gi", None)

    assert calendar.lookup(START, STOP) is None

    assert capsys.readouterr().err == (
        "calendar: no lookup, PyGObject or Evolution Data Server's typelibs are missing: "
        "import of gi halted; None in sys.modules\n"
    )


@pytest.fixture
def gi() -> ModuleType:
    """Ubuntu's PyGObject, which the lookup runs on: under another Python it cannot run."""
    module = pytest.importorskip("gi", exc_type=ImportError)
    assert isinstance(module, ModuleType)
    return module


class Calendars:
    """Stands in for Evolution Data Server: its source registry and its calendar clients.

    The events are real libical components and times, as EDS hands them to the callback of
    generate_instances_sync, a repeating one once for each of its occurrences in the span.
    """

    def __init__(self, gi: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        gi.require_version("ECal", "2.0")
        gi.require_version("EDataServer", "1.2")
        gi.require_version("ICalGLib", "3.0")
        from gi.repository import ECal, EDataServer, GLib, ICalGLib  # noqa: PLC0415

        self.ical = ICalGLib
        self.glib = GLib
        self.sources: list[SimpleNamespace] = []
        self.extensions: list[str | None] = []
        self.connected: list[tuple[str, object, int]] = []
        self.asked: list[tuple[str, int, int]] = []
        self.registry_error: str | None = None
        monkeypatch.setattr(EDataServer.SourceRegistry, "new_sync", self.registry)
        monkeypatch.setattr(ECal.Client, "connect_sync", self.connect)

    def add(
        self, name: str, *events: tuple[Sequence[str], str, str], error: str | None = None
    ) -> None:
        """A calendar, each event as its VEVENT lines and its occurrence's start and end."""
        self.sources.append(SimpleNamespace(name=name, events=events, error=error))

    def registry(self, cancellable: object = None) -> SimpleNamespace:
        assert cancellable is None
        if self.registry_error is not None:
            raise self.glib.Error(self.registry_error)

        def list_enabled(extension: str | None = None) -> list[SimpleNamespace]:
            self.extensions.append(extension)
            return [
                SimpleNamespace(get_display_name=lambda s=source: s.name, fake=source)
                for source in self.sources
            ]

        return SimpleNamespace(list_enabled=list_enabled)

    def connect(
        self, source: SimpleNamespace, source_type: object, seconds: int, _: object = None
    ) -> SimpleNamespace:
        self.connected.append((source.fake.name, source_type, seconds))
        if source.fake.error is not None:
            raise self.glib.Error(source.fake.error)

        def generate_instances_sync(
            start: int, end: int, _cancellable: object, callback: Callable[..., bool]
        ) -> None:
            self.asked.append((source.fake.name, start, end))
            for lines, begins, ends in source.fake.events:
                component = self.ical.Component.new_from_string(
                    "\r\n".join(["BEGIN:VEVENT", *lines, "END:VEVENT", ""])
                )
                callback(component, self.time(begins), self.time(ends), None, None)

        return SimpleNamespace(generate_instances_sync=generate_instances_sync)

    def time(self, text: str) -> object:
        """A time as libical writes it, "20261118T100000 America/…" for one in a time zone."""
        value, _, zone = text.partition(" ")
        time = self.ical.Time.new_from_string(value)
        if zone:
            time.set_timezone(self.ical.Timezone.get_builtin_timezone(zone))
        return time


@pytest.fixture
def calendars(gi: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Calendars:
    return Calendars(gi, monkeypatch)


WEEKLY_SYNC = (
    "SUMMARY:Weekly sync",
    "ATTENDEE;CN=Ana Díaz:mailto:ana@example.com",
    "ATTENDEE:mailto:bob.ruiz@example.com",
)


def test_lookup_gives_the_event_the_recording_overlaps(
    calendars: Calendars, capsys: pytest.CaptureFixture[str]
) -> None:
    from gi.repository import ECal  # noqa: PLC0415

    at_ten = f"20261118T100000 {BUENOS_AIRES}"
    calendars.add("Work", (WEEKLY_SYNC, at_ten, f"20261118T110000 {BUENOS_AIRES}"))

    assert calendar.lookup(START, STOP) == Event(
        name="Weekly sync",
        attendees=("Ana Díaz", "bob.ruiz"),
        start=ONE_PM,
        end=ONE_PM + HOUR,
    )
    assert calendars.extensions == ["Calendar"]
    assert calendars.connected == [("Work", ECal.ClientSourceType.EVENTS, 1)]
    assert calendars.asked == [("Work", int(START.timestamp()), int(STOP.timestamp()))]
    assert capsys.readouterr().err == ""


def test_a_repeating_meeting_is_found_at_the_occurrence_eds_expanded(
    calendars: Calendars,
) -> None:
    first_of_the_series = ("DTSTART:20261104T130000Z", "DTEND:20261104T140000Z")
    calendars.add(
        "Work",
        (
            (*first_of_the_series, "RRULE:FREQ=WEEKLY", *WEEKLY_SYNC),
            "20261118T130000Z",
            "20261118T140000Z",
        ),
    )

    event = calendar.lookup(START, STOP)

    assert event is not None
    assert (event.name, event.start, event.end) == ("Weekly sync", ONE_PM, ONE_PM + HOUR)


def test_all_day_events_are_not_meetings(calendars: Calendars) -> None:
    calendars.add(
        "Birthdays & Anniversaries", (("SUMMARY:Ana's birthday",), "20261118", "20261119")
    )

    assert calendar.lookup(START, STOP) is None


def test_the_longest_overlap_wins_across_every_calendar(calendars: Calendars) -> None:
    calendars.add("Personal", (("SUMMARY:Dentist",), "20261118T120000Z", "20261118T131000Z"))
    calendars.add("Work", (WEEKLY_SYNC, "20261118T130000Z", "20261118T140000Z"))

    event = calendar.lookup(START, STOP)

    assert event is not None
    assert event.name == "Weekly sync"


def test_an_event_without_a_summary_has_an_empty_name(calendars: Calendars) -> None:
    calendars.add("Work", (WEEKLY_SYNC[1:], "20261118T130000Z", "20261118T140000Z"))

    assert calendar.lookup(START, STOP) == Event(
        name="", attendees=("Ana Díaz", "bob.ruiz"), start=ONE_PM, end=ONE_PM + HOUR
    )


def test_no_event_in_the_span_finds_nothing(
    calendars: Calendars, capsys: pytest.CaptureFixture[str]
) -> None:
    calendars.add("Work")

    assert calendar.lookup(START, STOP) is None

    assert capsys.readouterr().err == ""


def test_a_calendar_that_cannot_be_opened_is_skipped(
    calendars: Calendars, capsys: pytest.CaptureFixture[str]
) -> None:
    calendars.add("Nextcloud", error="Authentication required")
    calendars.add("Work", (WEEKLY_SYNC, "20261118T130000Z", "20261118T140000Z"))

    event = calendar.lookup(START, STOP)

    assert event is not None
    assert event.name == "Weekly sync"
    assert capsys.readouterr().err == "calendar: Nextcloud skipped: Authentication required\n"


def test_evolution_data_server_out_of_reach_finds_nothing_and_says_why(
    calendars: Calendars, capsys: pytest.CaptureFixture[str]
) -> None:
    calendars.registry_error = "The name is not activatable"

    assert calendar.lookup(START, STOP) is None

    assert capsys.readouterr().err == (
        "calendar: no lookup, Evolution Data Server is out of reach: The name is not activatable\n"
    )


def test_without_the_typelibs_the_lookup_finds_nothing_and_says_why(
    gi: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def require_version(namespace: str, version: str) -> None:
        message = f"Namespace {namespace} not available for version {version}"
        raise ValueError(message)

    monkeypatch.setattr(gi, "require_version", require_version)

    assert calendar.lookup(START, STOP) is None

    assert capsys.readouterr().err == (
        "calendar: no lookup, PyGObject or Evolution Data Server's typelibs are missing: "
        "Namespace ECal not available for version 2.0\n"
    )
