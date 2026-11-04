"""Run with: python -m unittest test_log_to_html (needs pytz)."""

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('log_to_html', os.path.join(HERE, 'log-to-html.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

T0 = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def line(seconds, message, conn=None):
    entry = {'time': (T0 + timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ'), 'message': message}
    if conn is not None:
        entry['connIndex'] = conn
    return json.dumps(entry) + '\n'


def outages(lines):
    found, since = m.find_outages(m.read_events(lines))
    return [(int((s - T0).total_seconds()), None if e is None else int((e - T0).total_seconds())) for s, e in found], since


ALL_UP = [line(0, m.REGISTERED, c) for c in range(4)]


class FindOutages(unittest.TestCase):
    def test_one_connection_dropping_is_not_an_outage(self):
        self.assertEqual(outages(ALL_UP + [line(10, m.LOST, 2), line(21, m.REGISTERED, 2)])[0], [])

    def test_rolling_drops_with_one_always_up_is_not_an_outage(self):
        lines = ALL_UP + [line(10, m.LOST, 0), line(11, m.LOST, 1), line(12, m.LOST, 2),
                          line(13, m.REGISTERED, 0), line(14, m.LOST, 3)]
        self.assertEqual(outages(lines)[0], [])

    def test_all_connections_down_is_an_outage_ending_on_first_reconnect(self):
        lines = ALL_UP + [line(10 + c, m.LOST, c) for c in range(4)] + [line(100, m.REGISTERED, 1), line(101, m.REGISTERED, 0)]
        self.assertEqual(outages(lines)[0], [(13, 100)])

    def test_outage_still_going_has_no_end(self):
        lines = ALL_UP + [line(10, m.LOST, c) for c in range(4)]
        self.assertEqual(outages(lines)[0], [(10, None)])

    def test_boot_without_internet_is_not_counted(self):
        lines = [line(0, m.STARTING), line(5, 'Serve tunnel error'), line(5, m.STARTING), line(600, m.REGISTERED, 0)]
        found, since = outages(lines)
        self.assertEqual(found, [])
        self.assertEqual(since, T0 + timedelta(seconds=600))

    def test_restart_while_connected_is_an_outage_until_reconnect(self):
        self.assertEqual(outages(ALL_UP + [line(50, m.STARTING), line(56, m.REGISTERED, 0)])[0], [(50, 56)])

    def test_clean_stop_counts_from_the_shutdown_not_the_restart(self):
        lines = ALL_UP + [line(50, 'Initiating graceful shutdown due to signal terminated ...'),
                          line(50, 'Unregistered tunnel connection', 1),
                          line(96, m.STARTING), line(99, m.REGISTERED, 0)]
        self.assertEqual(outages(lines)[0], [(50, 99)])

    def test_clean_stop_during_outage_keeps_the_original_start(self):
        lines = ALL_UP + [line(10, m.LOST, c) for c in range(4)] + [
            line(50, 'Initiating graceful shutdown due to signal terminated ...'),
            line(90, m.STARTING), line(95, m.REGISTERED, 0)]
        self.assertEqual(outages(lines)[0], [(10, 95)])

    def test_restart_during_outage_keeps_the_original_start(self):
        lines = ALL_UP + [line(10, m.LOST, c) for c in range(4)] + [line(50, m.STARTING), line(90, m.REGISTERED, 0)]
        self.assertEqual(outages(lines)[0], [(10, 90)])

    def test_duplicate_lost_for_a_down_connection_is_ignored(self):
        lines = ALL_UP + [line(10, m.LOST, c) for c in range(4)] + [line(20, m.LOST, 0), line(30, m.REGISTERED, 0)]
        self.assertEqual(outages(lines)[0], [(10, 30)])

    def test_bad_lines_are_skipped(self):
        lines = ['not json\n', '[1, 2]\n', '{"message": "no time"}\n', '{"time": "nope", "message": "x"}\n'] + ALL_UP
        self.assertEqual(outages(lines), ([], T0))


class Render(unittest.TestCase):
    def test_duration_format(self):
        self.assertEqual(m.format_duration(11), '11 sec')
        self.assertEqual(m.format_duration(1943), '32 min 23 sec')
        self.assertEqual(m.format_duration(3700), '1 hr 1 min')

    def test_uptime_and_newest_first(self):
        outs = [(T0, T0 + timedelta(seconds=360)), (T0 + timedelta(hours=2), T0 + timedelta(hours=2, seconds=11))]
        html = m.render(outs, T0, T0 + timedelta(hours=10))
        self.assertIn('98.97% uptime', html)
        self.assertLess(html.index('11 sec'), html.index('6 min 0 sec'))

    def test_uses_local_time_with_dst(self):
        html = m.render([(T0, T0 + timedelta(seconds=5))], T0, T0 + timedelta(hours=1))
        self.assertIn('November 02 2026 : 07:00:00 AM', html)

    def test_ongoing_outage_shows_still_down(self):
        self.assertIn('Still down', m.render([(T0, None)], T0, T0 + timedelta(hours=1)))

    def test_never_connected(self):
        self.assertIn("hasn't connected", m.render([], None, T0))


class Main(unittest.TestCase):
    def test_writes_page_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            log, out = os.path.join(d, 'c.log'), os.path.join(d, 'logs', 'index.html')
            with open(log, 'w') as f:
                f.writelines(ALL_UP)
            m.main(log, out)
            self.assertEqual(os.listdir(os.path.dirname(out)), ['index.html'])

    def test_missing_log_leaves_the_existing_page_alone(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'index.html')
            with open(out, 'w') as f:
                f.write('old')
            with self.assertRaises(FileNotFoundError):
                m.main(os.path.join(d, 'missing.log'), out)
            with open(out) as f:
                self.assertEqual(f.read(), 'old')


if __name__ == '__main__':
    unittest.main()
