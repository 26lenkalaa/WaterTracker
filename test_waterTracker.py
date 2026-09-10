"""Tests for waterTracker.

	python -m unittest discover claudeCode

Nothing here touches Messages or the real state file: sends are captured in a
list and STATE_PATH is redirected into a temp directory.
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("WATER_PHONE", "+15551234567")
_spec = importlib.util.spec_from_file_location("waterTracker", Path(__file__).with_name("waterTracker.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


class ExtractOuncesTest(unittest.TestCase):
	def test_reads_amounts_out_of_sentences(self):
		cases = {
			"16": 16.0,
			"16oz": 16.0,
			"2 cups": 16.0,
			"500ml": 16.9,
			"+1L": 33.8,
			"just had a couple glasses": 16.0,
			"drank 500ml and a bottle at the gym": 33.8,
			"half a liter": 16.9,
			"i just finished a glass of water": 8.0,
			"had 20 oz, you can stop for today": 20.0,
			"i had three bottles today": 50.7,
			"12 ounces down": 12.0,
			"a bottle": 16.9,
			"just drank 16": 16.0,
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertAlmostEqual(wt.extract_ounces(text), expected, places=1)

	def test_reads_containers_word_numbers_and_fractions(self):
		cases = {
			"a can": 12.0,
			"my nalgene": 32.0,
			"another glass": 8.0,
			"a few sips": 4.5,
			"a couple gulps": 4.0,
			"twenty five ounces": 25.0,
			"fifteen oz": 15.0,
			"two hundred ml": 6.8,
			"3/4 of a bottle": 12.7,
			"half my water bottle": 8.4,
			"a glass and a half": 12.0,
			"a pint": 16.0,
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertAlmostEqual(wt.extract_ounces(text), expected, places=1)

	def test_a_negation_before_the_amount_is_not_a_log(self):
		for text in ("i havent had 16 oz yet", "didn't drink my 2 cups", "no water yet, not even a glass"):
			with self.subTest(text=text):
				self.assertTrue(wt.amount_is_negated(text))
		# A negation *after* the amount is a different sentence.
		self.assertFalse(wt.amount_is_negated("had 16 oz but not the second bottle"))

	def test_a_container_without_a_quantity_needs_a_possessive(self):
		# "the bottle is empty" is a statement, not a drink.
		self.assertIsNone(wt.extract_ounces("the bottle is empty"))
		self.assertEqual(wt.extract_ounces("my bottle"), 16.9)

	def test_ignores_text_without_a_usable_amount(self):
		# The bare-number guard matters most here: a time or a count in a long
		# sentence must not be logged as ounces.
		for text in (
			"i'll drink some at 16:00 after my 3 meetings",
			"lol",
			"ok you can stop for today",
			"how much have i had today?",
			"9999999 oz",
			"0 oz",
			"",
			"maybe later today when i get back from the gym around 16",
		):
			with self.subTest(text=text):
				self.assertIsNone(wt.extract_ounces(text))


class DetectIntentTest(unittest.TestCase):
	def test_finds_commands_in_longer_replies(self):
		cases = {
			"undo": "undo",
			"scratch that": "undo",
			"remove the last one": "undo",
			"nevermind": "undo",
			"status": "status",
			"?": "status",
			"how much so far": "status",
			"progress": "status",
			"pause": "pause",
			"quiet please": "pause",
			"ok you can stop for today": "pause",
			"resume": "resume",
			"go": "resume",
			"turn it back on": "resume",
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertEqual(wt.detect_intent(text), expected)

	def test_finds_casual_phrasings(self):
		cases = {
			"done": "drank",
			"yep": "drank",
			"just finished one": "drank",
			"👍": "drank",
			"not yet": "later",
			"in a bit": "later",
			"nah": "later",
			"i'm going to bed": "pause",
			"done for the day": "pause",  # a pause, not a glass
			"how am i doing": "status",
			"am i on track": "status",
			"oops": "undo",
			"my bad": "undo",
			"what's the trend": "week",
			"i'm back": "resume",
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertEqual(wt.detect_intent(text), expected)

	def test_plain_amounts_are_not_commands(self):
		for text in ("16", "2 cups", "just had a glass"):
			with self.subTest(text=text):
				self.assertIsNone(wt.detect_intent(text))


class MessageTimeTest(unittest.TestCase):
	def test_reads_both_second_and_nanosecond_timestamps(self):
		when = datetime(2026, 9, 9, 12, 0).timestamp()
		apple = when - wt.APPLE_EPOCH
		self.assertAlmostEqual(wt.message_time(int(apple)), when, places=0)
		self.assertAlmostEqual(wt.message_time(int(apple * 1_000_000_000)), when, places=0)

	def test_no_timestamp_is_not_a_time(self):
		self.assertIsNone(wt.message_time(None))
		self.assertIsNone(wt.message_time(0))


class HandleTest(unittest.TestCase):
	def test_accepts_phone_numbers_and_emails(self):
		for handle in ("+15551234567", "5551234567", "(555) 123-4567", "me@example.com"):
			with self.subTest(handle=handle):
				self.assertTrue(wt.looks_like_handle(handle))

	def test_rejects_placeholders_and_fragments(self):
		# "+1..." is what got pasted out of a setup example, sending every
		# reminder into a handle Messages could not deliver to.
		for handle in ("+1...", "", None, "your number here", "+1", "555-1234", "me@localhost", "@example.com"):
			with self.subTest(handle=handle):
				self.assertFalse(wt.looks_like_handle(handle))


class TrackerTestCase(unittest.TestCase):
	"""Base class: a tracker on a temp state file with sends captured."""

	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.state_path = Path(folder.name) / "state.json"
		self.patch(wt, "STATE_PATH", self.state_path)
		self.tracker = wt.WaterTracker()
		self.sent = []
		self.tracker.send = self.sent.append

	def patch(self, module, name, value):
		"""Swap a module-level setting for the duration of one test."""
		previous = getattr(module, name)
		setattr(module, name, value)
		self.addCleanup(setattr, module, name, previous)

	def set_day(self, day, ounces):
		self.tracker.state["days"][day.isoformat()] = [{"at": f"{day}T09:00:00", "oz": ounces, "via": "test"}]


class StateTest(TrackerTestCase):
	def test_write_is_atomic_and_leaves_no_scratch_file(self):
		self.tracker.add(16, "cli")
		self.assertEqual(json.loads(self.state_path.read_text())["days"][self.tracker.today()][0]["oz"], 16)
		self.assertFalse(self.state_path.with_name(self.state_path.name + ".tmp").exists())

	def test_unreadable_state_is_kept_not_overwritten(self):
		self.tracker.add(16, "cli")
		original = self.state_path.read_text()
		self.state_path.write_text(original[:20])  # a write cut short

		fresh = wt.WaterTracker()
		self.assertEqual(fresh.total(), 0)
		spoiled = self.state_path.with_name(self.state_path.name + ".corrupt")
		self.assertEqual(spoiled.read_text(), original[:20])

	def test_inspecting_a_bad_file_does_not_move_it(self):
		self.state_path.write_text("{not json")
		self.assertIsNone(wt.load_state(quarantine=False))
		self.assertTrue(self.state_path.exists())

	def test_missing_file_starts_empty(self):
		self.assertEqual(wt.load_state(), {})

	def test_reading_the_log_does_not_need_a_phone_number(self):
		self.patch(self.tracker, "phone", None)
		self.tracker.add(16, "cli")
		self.assertIn("16", self.tracker.progress_line())
		self.assertEqual(len(self.tracker.week_lines(7)), 8)

	def test_sending_without_a_phone_number_stops_with_a_clear_error(self):
		tracker = wt.WaterTracker()
		tracker.phone = None
		with self.assertRaises(SystemExit) as caught:
			tracker.send("hello")
		self.assertIn("WATER_PHONE", str(caught.exception))

	def test_sending_to_a_placeholder_handle_stops_before_messages_sees_it(self):
		tracker = wt.WaterTracker()
		tracker.phone = "+1..."
		with self.assertRaises(SystemExit) as caught:
			tracker.send("hello")
		self.assertIn("not a phone number", str(caught.exception))


class MarkerTest(TrackerTestCase):
	"""Outgoing messages have to be self-identifying, late echoes included."""

	def setUp(self):
		super().setUp()
		self.osascript = []

		class Result:
			returncode = 0
			stderr = ""

		def fake_run(command, **kwargs):
			self.osascript.append(command[-1])
			return Result()

		self.patch(wt.subprocess, "run", fake_run)
		# The real send, not the capturing stub the base class installs.
		self.tracker.send = lambda message: wt.WaterTracker.send(self.tracker, message)

	def test_every_outgoing_message_carries_the_marker(self):
		self.tracker.handle_reply("16 oz")
		self.tracker.handle_reply("status")
		self.tracker.handle_reply("lol")
		self.assertEqual(len(self.osascript), 3)
		for message in self.osascript:
			self.assertTrue(message.startswith(wt.MARKER), f"unmarked: {message!r}")

	def test_the_marker_is_not_doubled(self):
		self.tracker.send(f"{wt.MARKER} already marked")
		self.assertEqual(self.osascript[-1].count(wt.MARKER), 1)


class EchoTest(TrackerTestCase):
	"""Texting your own number means reading your own sends back as replies."""

	def test_our_own_message_is_recognised_once(self):
		self.tracker.state["sent_echoes"].append([time.time(), "💧 Hydrate."])
		self.assertTrue(self.tracker.is_echo("💧 Hydrate."))
		self.assertFalse(self.tracker.is_echo("💧 Hydrate."), "one send matched two incoming rows")

	def test_a_real_reply_is_not_mistaken_for_an_echo(self):
		self.tracker.state["sent_echoes"].append([time.time(), "💧 Hydrate."])
		self.assertFalse(self.tracker.is_echo("16 oz"))

	def test_records_expire_instead_of_accumulating(self):
		self.tracker.state["sent_echoes"] = [
			[time.time() - wt.ECHO_WINDOW_SEC - 1, "old"],
			[time.time(), "new"],
		]
		self.tracker.forget_stale_echoes()
		self.assertEqual([message for _, message in self.tracker.state["sent_echoes"]], ["new"])

	def test_a_burst_of_sends_does_not_evict_a_pending_record(self):
		# The old 20-message cap dropped the oldest record regardless of age.
		self.tracker.state["sent_echoes"].append([time.time(), "first"])
		for number in range(30):
			self.tracker.state["sent_echoes"].append([time.time(), f"nudge {number}"])
		self.assertTrue(self.tracker.is_echo("first"))

	def test_state_written_in_the_old_format_is_discarded(self):
		self.tracker.state["sent_echoes"] = ["a bare string from an older version"]
		self.tracker.save()
		self.assertEqual(wt.WaterTracker().state["sent_echoes"], [])


class HandleReplyTest(TrackerTestCase):
	def test_logs_an_amount_and_reports_what_is_left(self):
		self.tracker.handle_reply("just had a couple glasses")
		self.assertEqual(self.tracker.total(), 16)
		self.assertIn("84 oz to go", self.sent[-1])

	def test_sets_the_goal_without_logging_it(self):
		self.tracker.handle_reply("set my goal to 64 oz")
		self.assertEqual(self.tracker.goal, 64)
		self.assertEqual(self.tracker.total(), 0)

	def test_goal_word_next_to_an_amount_still_logs(self):
		# "my goal, i drank 16 oz" is a log, not a goal change: the comma ends
		# the goal phrase.
		self.tracker.handle_reply("my goal, i drank 16 oz")
		self.assertEqual(self.tracker.goal, 100)
		self.assertEqual(self.tracker.total(), 16)

	def test_one_reply_can_log_and_pause(self):
		self.tracker.handle_reply("had 20 oz, you can stop for today")
		self.assertEqual(self.tracker.total(), 20)
		self.assertEqual(self.tracker.state["paused_on"], self.tracker.today())
		self.assertIn("Paused", self.sent[-1])

	def test_undo_drops_the_last_entry_only(self):
		self.tracker.handle_reply("16 oz")
		self.tracker.handle_reply("8 oz")
		self.tracker.handle_reply("scratch that")
		self.assertEqual(self.tracker.total(), 16)

	def test_undo_on_an_empty_day_is_harmless(self):
		self.tracker.handle_reply("undo")
		self.assertIn("Nothing logged", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0)

	def test_resume_clears_a_pause(self):
		self.tracker.handle_reply("pause")
		self.tracker.handle_reply("resume")
		self.assertIsNone(self.tracker.state["paused_on"])

	def test_goal_is_celebrated_once_a_day(self):
		self.tracker.handle_reply("100 oz")
		self.assertIn("Goal hit", self.sent[-1])
		self.tracker.handle_reply("8 oz")
		self.assertNotIn("Goal hit", self.sent[-1])
		self.assertIn("past your goal", self.sent[-1])

	def test_raising_the_goal_allows_a_fresh_celebration(self):
		self.tracker.handle_reply("100 oz")
		self.tracker.handle_reply("goal 120")
		self.tracker.handle_reply("20 oz")
		self.assertIn("Goal hit", self.sent[-1])

	def test_confirming_without_an_amount_logs_a_default_serving(self):
		self.tracker.handle_reply("done")
		self.assertEqual(self.tracker.total(), wt.DEFAULT_SERVING_OZ)
		self.assertIn("text an amount to be exact", self.sent[-1])

	def test_an_amount_beats_a_bare_confirmation(self):
		self.tracker.handle_reply("yep, drank 20 oz")
		self.assertEqual(self.tracker.total(), 20)

	def test_a_negated_amount_is_not_logged(self):
		self.tracker.handle_reply("i havent had 16 oz yet")
		self.assertEqual(self.tracker.total(), 0)
		self.assertIn("check back later", self.sent[-1])

	def test_not_yet_pushes_the_next_nudge_out_without_pausing(self):
		before = self.tracker.state["last_nudge_at"]
		self.tracker.handle_reply("not yet")
		self.assertGreater(self.tracker.state["last_nudge_at"], before)
		self.assertIsNone(self.tracker.state["paused_on"], "a snooze is not a pause")
		self.assertEqual(self.tracker.total(), 0)

	def test_unparseable_reply_asks_again_without_logging(self):
		self.tracker.handle_reply("lol")
		self.assertIn("Didn't catch", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0)


class StreakTest(TrackerTestCase):
	def test_counts_back_from_the_last_day_at_goal(self):
		today = date.today()
		for offset in (1, 2, 3):
			self.set_day(today - timedelta(days=offset), 100)
		self.assertEqual(self.tracker.streak(), 3)

	def test_today_counts_once_the_goal_is_met(self):
		self.set_day(date.today() - timedelta(days=1), 100)
		self.tracker.add(100, "test")
		self.assertEqual(self.tracker.streak(), 2)

	def test_a_short_day_breaks_the_streak(self):
		today = date.today()
		self.set_day(today - timedelta(days=1), 100)
		self.set_day(today - timedelta(days=2), 20)
		self.set_day(today - timedelta(days=3), 100)
		self.assertEqual(self.tracker.streak(), 1)


class WeekTest(TrackerTestCase):
	def test_lists_recent_days_oldest_first_with_an_average(self):
		today = date.today()
		self.set_day(today - timedelta(days=1), 100)
		self.set_day(today - timedelta(days=2), 50)
		lines = self.tracker.week_lines(3)

		self.assertEqual(len(lines), 4, "3 days plus a summary")
		self.assertIn("50 oz", lines[0])
		self.assertIn("100 oz", lines[1])
		self.assertIn("*", lines[1], "a day at goal is marked")
		self.assertNotIn("*", lines[0])
		self.assertIn("50 oz/day average, 1/3 days at goal", lines[-1])

	def test_days_with_nothing_logged_count_as_zero(self):
		lines = self.tracker.week_lines(7)
		self.assertIn("0 oz/day average, 0/7 days at goal", lines[-1])
		self.assertEqual(len(lines), 8)

	def test_asking_by_text_answers_with_the_week(self):
		self.set_day(date.today() - timedelta(days=1), 64)
		self.tracker.handle_reply("what was my weekly average")
		self.assertIn("oz/day average", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0, "a question logged an amount")


class ReadRepliesTest(TrackerTestCase):
	"""The reply path against a stand-in for the Messages database."""

	def setUp(self):
		super().setUp()
		self.db_path = self.state_path.with_name("chat.db")
		self.patch(wt, "CHAT_DB", self.db_path)
		# WAL mode with the connection held open, as Messages runs it: commits
		# land in the -wal sidecar and are not checkpointed into chat.db, which
		# is what makes copying the sidecars necessary.
		self.db = sqlite3.connect(self.db_path)
		self.addCleanup(self.db.close)
		self.db.execute("PRAGMA journal_mode=WAL")
		self.db.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
		self.db.execute(
			"CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, "
			"attributedBody BLOB, handle_id INTEGER, is_from_me INTEGER, date INTEGER)"
		)
		self.db.execute("INSERT INTO handle VALUES (1, ?)", (self.tracker.phone,))
		self.db.execute("INSERT INTO handle VALUES (2, '+15559998888')")
		self.db.commit()

	def add_message(self, text, handle=1, from_me=0, blob=None, minutes_ago=0):
		apple_time = int((time.time() - minutes_ago * 60 - wt.APPLE_EPOCH) * 1_000_000_000)
		self.db.execute(
			"INSERT INTO message (text, attributedBody, handle_id, is_from_me, date) "
			"VALUES (?, ?, ?, ?, ?)",
			(text, blob, handle, from_me, apple_time),
		)
		self.db.commit()

	def bodies(self):
		return [body for _, body in self.tracker.read_replies()]

	def count_copies(self):
		"""Start counting database copies; returns the growing list."""
		copies = []
		original = shutil.copy2

		class CountingShutil:
			def copy2(self, source, target):
				copies.append(source)
				return original(source, target)

		self.patch(wt, "shutil", CountingShutil())
		return copies

	def test_reads_each_new_message_once(self):
		self.add_message("16 oz")
		self.assertEqual(self.bodies(), ["16 oz"])
		self.add_message("8 oz")
		self.assertEqual(self.bodies(), ["8 oz"], "an already-handled message came back")

	def test_ignores_other_senders_and_our_own_outgoing_rows(self):
		self.add_message("16 oz", handle=2)
		self.add_message("32 oz", from_me=1)
		self.add_message("64 oz")
		self.assertEqual(self.bodies(), ["64 oz"])

	def test_matches_a_handle_written_in_another_format(self):
		with closing(sqlite3.connect(self.db_path)) as db:
			db.execute("UPDATE handle SET id = '(555) 123-4567' WHERE ROWID = 1")
			db.commit()
		self.add_message("16 oz")
		self.assertEqual(self.bodies(), ["16 oz"])

	def test_our_own_nudge_read_back_is_not_a_reply(self):
		# Texting your own number: the send shows up again as an incoming row.
		nudge = "💧 Hydrate. ░░░░░░░░░░ 0/100 oz (0%)"
		self.tracker.state["sent_echoes"].append([time.time(), nudge])
		self.add_message(nudge)
		self.assertEqual(self.bodies(), [])

	def test_our_own_message_read_back_late_is_still_not_a_reply(self):
		# The echo record expires but the copy stays in the database forever,
		# and our own text is full of amounts.
		own = f"{wt.MARKER} Logged 16 oz. ████░░░░░░ 48/100 oz (48%) — 52 oz to go."
		self.assertIsNotNone(wt.extract_ounces(own), "premise: this text parses as an amount")
		self.add_message(own)
		self.assertEqual(self.bodies(), [])

	def test_a_reply_from_hours_ago_is_not_logged_today(self):
		self.add_message("16 oz", minutes_ago=wt.STALE_REPLY_MIN + 1)
		self.add_message("8 oz", minutes_ago=1)
		self.assertEqual(self.bodies(), ["8 oz"])

	def test_a_message_without_a_timestamp_is_still_read(self):
		self.db.execute("INSERT INTO message (text, handle_id, is_from_me) VALUES ('16 oz', 1, 0)")
		self.db.commit()
		self.assertEqual(self.bodies(), ["16 oz"])

	def test_falls_back_to_the_attributed_body(self):
		text = "24 oz"
		blob = b"streamtyped\x81\xe8\x03NSString\x01\x94\x84\x01+" + bytes([len(text)]) + text.encode()
		self.add_message(None, blob=blob)
		self.assertEqual(self.bodies(), [text])

	def test_history_is_skipped_on_first_run(self):
		self.add_message("16 oz from last week")
		fresh = wt.WaterTracker()
		fresh.prime_replies()
		self.assertEqual(fresh.read_replies(), [])

	def test_unchanged_database_is_not_copied_again(self):
		copies = self.count_copies()
		self.add_message("16 oz")
		self.assertEqual(self.bodies(), ["16 oz"])
		after_read = len(copies)
		self.assertGreater(after_read, 0)

		self.assertEqual(self.bodies(), [])
		self.assertEqual(len(copies), after_read, "copied a database that had not changed")

		# The insert above sits in the -wal sidecar, so this only works if the
		# stamp watches the sidecars and not chat.db alone.
		self.add_message("8 oz")
		self.assertEqual(self.bodies(), ["8 oz"], "missed a message after skipping a copy")
		self.assertGreater(len(copies), after_read)

	def test_an_unreadable_stamp_does_not_skip_the_read(self):
		self.add_message("16 oz")
		self.patch(self.tracker, "db_stamp", lambda: None)
		self.assertEqual(self.bodies(), ["16 oz"])
		self.assertEqual(self.bodies(), [], "the cursor did not advance")


class PruneTest(TrackerTestCase):
	def test_drops_days_past_the_window_and_keeps_the_rest(self):
		today = date.today()
		self.set_day(today - timedelta(days=wt.KEEP_DAYS + 5), 100)
		self.set_day(today - timedelta(days=wt.KEEP_DAYS - 5), 100)
		self.set_day(today, 20)

		self.assertEqual(self.tracker.prune_days(), 1)
		remaining = sorted(self.tracker.state["days"])
		self.assertEqual(remaining, sorted([
			(today - timedelta(days=wt.KEEP_DAYS - 5)).isoformat(),
			today.isoformat(),
		]))

	def test_nothing_to_prune_is_a_no_op(self):
		self.set_day(date.today(), 20)
		self.assertEqual(self.tracker.prune_days(), 0)
		self.assertEqual(self.tracker.total(), 20)


class RemindTest(TrackerTestCase):
	def setUp(self):
		super().setUp()
		self.patch(wt, "WAKE_HOUR", 0)
		self.patch(wt, "SLEEP_HOUR", 24)

	def test_nudges_then_waits_out_the_interval(self):
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), 1)
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), 1, "nudged twice inside one interval")

		# Well past any pace-adjusted gap.
		self.tracker.state["last_nudge_at"] = time.time() - wt.INTERVAL_MIN * 60 * 10
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), 2)

	def test_pace_target_grows_across_the_day(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.patch(wt, "SLEEP_HOUR", 20)
		expected = {6: 0, 8: 0, 14: 50, 20: 100, 23: 100}
		for hour, ounces in expected.items():
			with self.subTest(hour=hour):
				at = datetime(2026, 9, 9, hour)
				self.assertAlmostEqual(self.tracker.expected_by_now(at), ounces, places=1)

	def test_being_behind_shortens_the_wait_and_being_ahead_lengthens_it(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.patch(wt, "SLEEP_HOUR", 20)
		midday = datetime(2026, 9, 9, 14)  # 50 oz of a 100 oz goal due by now
		interval = wt.INTERVAL_MIN * 60

		self.assertEqual(self.tracker.nudge_gap(midday), interval * 0.5)  # 50 oz behind, floored
		self.tracker.add(25, "test")
		self.assertAlmostEqual(self.tracker.nudge_gap(midday), interval * 0.5)  # 25 oz behind
		self.tracker.add(15, "test")
		self.assertAlmostEqual(self.tracker.nudge_gap(midday), interval * 0.8)  # 10 oz behind
		self.tracker.add(20, "test")
		self.assertAlmostEqual(self.tracker.nudge_gap(midday), interval * 1.5)  # ahead of pace

	def test_nudge_says_how_far_behind_you_are(self):
		# maybe_remind reads the real clock, so the deficit is whatever the
		# time of day makes it; the point is that the text agrees with it.
		deficit = self.tracker.expected_by_now()
		self.tracker.maybe_remind()
		if deficit >= 1:
			self.assertIn(f"{deficit:.0f} oz behind pace", self.sent[-1])
		else:
			self.assertNotIn("behind pace", self.sent[-1])

	def test_interval_survives_a_restart(self):
		self.tracker.maybe_remind()
		restarted = wt.WaterTracker()
		restarted.send = self.sent.append
		restarted.maybe_remind()
		self.assertEqual(len(self.sent), 1, "a restart re-nudged immediately")

	def test_quiet_when_paused_asleep_or_done(self):
		self.tracker.state["paused_on"] = self.tracker.today()
		self.tracker.maybe_remind()
		self.assertEqual(self.sent, [])

		self.tracker.state["paused_on"] = None
		self.patch(wt, "WAKE_HOUR", 23)
		self.patch(wt, "SLEEP_HOUR", 24)
		self.tracker.state["last_nudge_at"] = 0
		if not self.tracker.awake():
			self.tracker.maybe_remind()
			self.assertEqual(self.sent, [], "nudged outside the waking window")

		self.patch(wt, "WAKE_HOUR", 0)
		self.patch(wt, "SLEEP_HOUR", 24)
		self.tracker.add(self.tracker.goal, "test")
		self.tracker.maybe_remind()
		self.assertEqual(self.sent, [], "nudged after the goal was met")


if __name__ == "__main__":
	unittest.main()
