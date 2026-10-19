from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import master_odds_parser as parser
import scheduled_runs


NOW = datetime(2026, 10, 19, 18, 0, tzinfo=parser.PARIS)


def match(event, minutes):
    return {
        "event_pmu": event, "event_wina": event, "url_betclic": "",
        "flashscore_url": f"https://fixture.test/{event}",
        "start": (NOW + timedelta(minutes=minutes)).isoformat(),
    }


def summary():
    return dict(exported=0, ignored=0, failed=0, detail="")


class ScheduledRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        patcher = patch.object(scheduled_runs, "CACHE_DIR", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_matches(self, records, worker, counts=None, arguments=None):
        counts = counts if counts is not None else summary()
        with patch.object(parser, "get_daily_matches", return_value=records), \
                patch.object(parser, "datetime") as clock, \
                patch.object(parser, "process_single_match", side_effect=worker) as process:
            clock.now.return_value = NOW
            parser.run_matches(parser.parse_args(arguments or ["--scheduled"]), counts)
        return counts, process

    def test_mode_implies_official_lineups_and_rejects_incompatible_options(self):
        self.assertFalse(parser.parse_args([]).scheduled)
        self.assertTrue(parser.parse_args(["--scheduled"]).official_lineups_only)
        for flags in (["--flashscore"], ["--date", "demain"], ["--file", "matchs.csv"]):
            with self.subTest(flags=flags), patch("sys.stderr"), self.assertRaises(SystemExit):
                parser.parse_args(["--scheduled"] + flags)

    def test_window_includes_90_minutes_but_excludes_started_and_distant_matches(self):
        records = [match(1, -1), match(2, 0), match(3, 90), match(4, 91)]
        counts, process = self.run_matches(records, ["exported"])
        self.assertEqual(counts["exported"], 1)
        self.assertEqual(counts["ignored"], 3)
        self.assertEqual(process.call_args.args[0], 3)
        self.assertTrue(process.call_args.args[3].official_lineups_only)
        self.assertEqual(process.call_args.kwargs["match_start"], records[2]["start"])

    def test_quiet_run_never_starts_workers(self):
        counts, process = self.run_matches([match(1, 91), match(2, -10)], [])
        process.assert_not_called()
        self.assertEqual(counts["ignored"], 2)
        self.assertFalse((self.cache / "exported.json").exists())

    def test_success_is_persisted_and_next_run_skips_it(self):
        self.run_matches([match(1, 30)], ["exported"])
        counts, process = self.run_matches([match(1, 15)], [])
        process.assert_not_called()
        self.assertEqual(counts["ignored"], 1)
        self.assertTrue(scheduled_runs.ExportedMatches().contains(1))

    def test_missing_lineups_are_retried_on_next_run(self):
        self.run_matches([match(1, 30)], ["ignored"])
        self.assertFalse(scheduled_runs.ExportedMatches().contains(1))
        counts, process = self.run_matches([match(1, 15)], ["exported"])
        process.assert_called_once()
        self.assertEqual(counts["exported"], 1)

    def test_failure_remains_retryable_but_other_success_is_saved(self):
        counts = summary()

        def worker(event, *args, **kwargs):
            if event == 1:
                raise RuntimeError("bookmaker")
            return "exported"

        with self.assertLogs(level="ERROR"), self.assertRaises(SystemExit):
            self.run_matches([match(1, 30), match(2, 30)], worker, counts)
        self.assertEqual((counts["failed"], counts["exported"]), (1, 1))
        state = scheduled_runs.ExportedMatches()
        self.assertFalse(state.contains(1))
        self.assertTrue(state.contains(2))
        self.run_matches([match(1, 15)], ["exported"])
        self.assertTrue(scheduled_runs.ExportedMatches().contains(1))

    def test_standard_mode_preserves_repeat_exports_and_ignores_window(self):
        scheduled_runs.ExportedMatches().mark_exported(1)
        counts, process = self.run_matches([match(1, 180)], ["exported"],
                                          arguments=["--official-lineups-only"])
        process.assert_called_once()
        self.assertEqual(counts["exported"], 1)

    def test_corrupt_state_is_not_silently_discarded(self):
        path = self.cache / "exported.json"
        for payload in ("{", "[]", '{"version": 2, "exported": []}',
                        '{"version": 1, "exported": [true]}'):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="utf-8")
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    scheduled_runs.ExportedMatches()

    def test_state_write_failure_is_explicit_and_keeps_previous_state(self):
        state = scheduled_runs.ExportedMatches()
        state.mark_exported(1)
        with patch.object(Path, "replace", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(OSError, "disk full"):
            state.mark_exported(2)
        self.assertFalse(state.contains(2))
        self.assertTrue(scheduled_runs.ExportedMatches().contains(1))
        self.assertFalse(scheduled_runs.ExportedMatches().contains(2))

    def test_one_state_write_error_does_not_prevent_saving_other_successes(self):
        counts = summary()
        replace = Path.replace

        def fail_first(path, destination):
            if json.loads(path.read_text(encoding="utf-8"))["exported"] == [1]:
                raise OSError("disk full")
            return replace(path, destination)

        with patch.object(Path, "replace", autospec=True, side_effect=fail_first), \
                patch.object(parser, "as_completed", side_effect=lambda futures: list(futures)), \
                self.assertLogs(level="ERROR") as logs, self.assertRaises(SystemExit):
            self.run_matches([match(1, 30), match(2, 30)], ["exported", "exported"], counts)
        self.assertIn("non memorise", counts["detail"])
        self.assertIn("risque de doublon", "\n".join(logs.output))
        self.assertEqual((counts["exported"], counts["failed"]), (2, 1))
        self.assertFalse(scheduled_runs.ExportedMatches().contains(1))
        self.assertTrue(scheduled_runs.ExportedMatches().contains(2))

    def test_lock_blocks_overlaps_and_releases_on_error(self):
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with scheduled_runs.run_lock() as first:
                self.assertTrue(first)
                with scheduled_runs.run_lock() as second:
                    self.assertFalse(second)
                raise RuntimeError("failure")
        with scheduled_runs.run_lock() as acquired:
            self.assertTrue(acquired)

    def test_another_process_cannot_acquire_active_lock(self):
        code = (
            "import fcntl, sys\n"
            "with open(sys.argv[1], 'a') as handle:\n"
            "    try:\n"
            "        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError:\n"
            "        sys.exit(2)\n"
        )
        command = [sys.executable, "-c", code, str(self.cache / "run.lock")]
        with scheduled_runs.run_lock():
            self.assertEqual(subprocess.run(command, check=False, timeout=10).returncode, 2)
        self.assertEqual(subprocess.run(command, check=False, timeout=10).returncode, 0)

    def test_overlapping_main_skips_discovery_and_execution_summary(self):
        with scheduled_runs.run_lock(), patch.object(parser, "configure_logging"), \
                patch.object(parser, "get_daily_matches") as discover, \
                patch.object(parser, "export_execution") as export:
            parser.main(["--scheduled"])
        discover.assert_not_called()
        export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
