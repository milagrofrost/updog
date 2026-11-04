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
SHUTDOWN = 'Initiating graceful shutdown due to signal terminated ...'


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def line(seconds, message, conn=None):
    entry = {'time': m.to_time_str(at(seconds)), 'message': message}
    if conn is not None:
        entry['connIndex'] = conn
    return json.dumps(entry) + '\n'


def outages(lines):
    found, since = m.find_outages(m.read_events(lines))
    return [(int((s - T0).total_seconds()), None if e is None else int((e - T0).total_seconds())) for s, e, _ in found], since


def state(seconds, link='up', modem=1, gateway=1, internet=1):
    return at(seconds), {'link': link, 'modem': modem, 'gateway': gateway, 'internet': internet}


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
        self.assertEqual(since, at(600))

    def test_restart_while_connected_is_an_outage_until_reconnect(self):
        self.assertEqual(outages(ALL_UP + [line(50, m.STARTING), line(56, m.REGISTERED, 0)])[0], [(50, 56)])

    def test_clean_stop_counts_from_the_shutdown_not_the_restart(self):
        lines = ALL_UP + [line(50, SHUTDOWN), line(50, 'Unregistered tunnel connection', 1),
                          line(96, m.STARTING), line(99, m.REGISTERED, 0)]
        self.assertEqual(outages(lines)[0], [(50, 99)])

    def test_clean_stop_during_outage_keeps_the_original_start(self):
        lines = ALL_UP + [line(10, m.LOST, c) for c in range(4)] + [line(50, SHUTDOWN), line(90, m.STARTING), line(95, m.REGISTERED, 0)]
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

    def test_only_outages_from_a_restart_are_marked_restarted(self):
        lines = ALL_UP + [line(10, m.LOST, c) for c in range(4)] + [line(20, m.REGISTERED, 0),
                                                                    line(30, m.STARTING), line(40, m.REGISTERED, 0),
                                                                    line(50, m.LOST, 0), line(60, m.REGISTERED, 0)]
        found, _ = m.find_outages(m.read_events(lines))
        self.assertEqual([restarted for _, _, restarted in found], [False, True, False])


class Classify(unittest.TestCase):
    def check(self, start, end, states, restarted=False):
        return m.classify(at(start), at(end) if end is not None else None, restarted, states, at(100000))

    def test_before_the_probe_started_is_unclear(self):
        self.assertEqual(self.check(100, 200, [state(500)]), (m.UNCLEAR, m.CAUSE_BEFORE))
        self.assertEqual(self.check(100, 200, []), (m.UNCLEAR, m.CAUSE_BEFORE))

    def test_restart_before_the_probe_started_is_still_not_isp(self):
        self.assertEqual(self.check(100, 200, [], restarted=True), (m.NOT_ISP, m.CAUSE_RESTART))

    def test_modem_fine_and_internet_down_is_the_isp(self):
        states = [state(0), state(95, internet=0, gateway=0), state(300)]
        self.assertEqual(self.check(100, 305, states), (m.ISP, m.CAUSE_ISP))

    def test_internet_going_down_before_the_tunnel_notices_is_the_isp(self):
        states = [state(0), state(60, internet=0), state(110)]
        self.assertEqual(self.check(100, 112, states), (m.ISP, m.CAUSE_ISP))

    def test_internet_fine_for_a_long_outage_is_not_the_isp(self):
        self.assertEqual(self.check(100, 400, [state(0)]), (m.NOT_ISP, m.CAUSE_TUNNEL))

    def test_internet_fine_for_a_short_outage_is_unclear(self):
        self.assertEqual(self.check(100, 120, [state(0)]), (m.UNCLEAR, m.CAUSE_SHORT))

    def test_single_blip_in_a_long_tunnel_outage_is_not_the_isp(self):
        states = [state(0), state(200, internet=0), state(215)]
        self.assertEqual(self.check(100, 1000, states), (m.NOT_ISP, m.CAUSE_TUNNEL))

    def test_usb_adapter_gone_is_not_the_isp(self):
        states = [state(0), state(95, link='gone', modem=0, gateway=0, internet=0), state(300)]
        self.assertEqual(self.check(100, 305, states), (m.NOT_ISP, m.CAUSE_USB))

    def test_link_down_is_unclear(self):
        states = [state(0), state(95, link='down', modem=0, gateway=0, internet=0), state(300)]
        self.assertEqual(self.check(100, 305, states), (m.UNCLEAR, m.CAUSE_LINK))

    def test_modem_not_responding_is_unclear(self):
        states = [state(0), state(95, modem=0, gateway=0, internet=0), state(300)]
        self.assertEqual(self.check(100, 305, states), (m.UNCLEAR, m.CAUSE_MODEM))

    def test_restart_with_internet_fine_is_not_the_isp(self):
        self.assertEqual(self.check(100, 120, [state(0)], restarted=True), (m.NOT_ISP, m.CAUSE_RESTART))

    def test_restart_during_a_real_internet_outage_is_the_isp(self):
        states = [state(0), state(95, internet=0), state(300)]
        self.assertEqual(self.check(100, 305, states, restarted=True), (m.ISP, m.CAUSE_ISP))

    def test_ongoing_outage_uses_the_current_time(self):
        states = [state(0), state(95, internet=0)]
        self.assertEqual(self.check(100, None, states), (m.ISP, m.CAUSE_ISP))


class ReadProbe(unittest.TestCase):
    def test_reads_states_and_reboots_and_skips_bad_lines(self):
        lines = ['junk\n',
                 '{"time":"2026-11-02T12:00:00Z","link":"up","modem":1,"gateway":1,"internet":1}\n',
                 '{"time":"2026-11-02T13:00:00Z","event":"reboot","down_since":"2026-11-02T12:30:00Z"}\n',
                 '{"time":"nope","internet":1}\n']
        states, reboots = m.read_probe(lines)
        self.assertEqual([t for t, _ in states], [T0])
        self.assertEqual(reboots, [at(1800)])


class UpdateHistory(unittest.TestCase):
    def update(self, history=None, found=(), since=T0, states=(), reboots=(), now=at(100000)):
        return m.update_history(history or {}, list(found), since, list(states), list(reboots), now)

    def test_finished_outages_survive_the_log_being_wiped(self):
        first = self.update(found=[(at(100), at(200), False)])
        second = self.update(first, found=[], since=at(50000))
        self.assertEqual(len(second['rows']), 1)
        self.assertEqual(second['tracking_since'], m.to_time_str(T0))

    def test_finished_outages_keep_their_cause(self):
        first = self.update(found=[(at(100), at(400), False)], states=[state(0)])
        self.assertEqual(first['rows'][0]['section'], m.NOT_ISP)
        second = self.update(first, found=[(at(100), at(400), False)], states=[])
        self.assertEqual(second['rows'][0]['section'], m.NOT_ISP)

    def test_open_outage_gets_updated_when_it_finishes(self):
        first = self.update(found=[(at(100), None, False)])
        self.assertIsNone(first['rows'][0]['end'])
        second = self.update(first, found=[(at(100), at(200), False)])
        self.assertEqual(second['rows'][0]['end'], m.to_time_str(at(200)))

    def test_reboot_becomes_a_not_isp_row_ending_at_first_connection(self):
        history = self.update(reboots=[at(1000)], since=at(1300))
        self.assertEqual(history['rows'], [{'start': m.to_time_str(at(1000)), 'end': m.to_time_str(at(1300)),
                                            'section': m.NOT_ISP, 'cause': m.CAUSE_REBOOT}])

    def test_reboot_is_still_open_until_the_tunnel_connects(self):
        self.assertIsNone(self.update(reboots=[at(1000)], since=None)['rows'][0]['end'])

    def test_reboot_keeps_its_end_after_later_log_data(self):
        first = self.update(reboots=[at(1000)], since=at(1300))
        second = self.update(first, reboots=[at(1000)], since=at(1300), found=[(at(5000), at(5100), False)])
        self.assertEqual([r['end'] for r in second['rows']], [m.to_time_str(at(1300)), m.to_time_str(at(5100))])

    def test_outage_open_when_the_router_went_off_ends_at_the_reboot(self):
        first = self.update(found=[(at(100), None, False)])
        second = self.update(first, reboots=[at(500)], since=at(800))
        self.assertEqual([(r['start'], r['end']) for r in second['rows']],
                         [(m.to_time_str(at(100)), m.to_time_str(at(500))), (m.to_time_str(at(500)), m.to_time_str(at(800)))])

    def test_outage_opened_after_the_last_heartbeat_is_covered_by_the_reboot(self):
        first = self.update(found=[(at(600), None, False)])
        second = self.update(first, reboots=[at(500)], since=at(800))
        self.assertEqual([r['start'] for r in second['rows']], [m.to_time_str(at(500))])


class Render(unittest.TestCase):
    def row(self, start, end, section, cause='x'):
        return {'start': m.to_time_str(at(start)), 'end': m.to_time_str(at(end)) if end is not None else None,
                'section': section, 'cause': cause}

    def history(self, rows):
        return {'tracking_since': m.to_time_str(T0), 'rows': rows}

    def test_duration_format(self):
        self.assertEqual(m.format_duration(11), '11 sec')
        self.assertEqual(m.format_duration(1943), '32 min 23 sec')
        self.assertEqual(m.format_duration(3700), '1 hr 1 min')

    def test_rows_go_in_their_section_newest_first(self):
        html = m.render(self.history([self.row(0, 11, m.UNCLEAR, 'older'), self.row(100, 111, m.UNCLEAR, 'newer'),
                                      self.row(200, 211, m.ISP, 'isp-row'), self.row(300, 311, m.NOT_ISP, 'home-row')]), at(36000))
        order = [html.index(text) for text in ('ISP (1)', 'isp-row', 'Not the ISP (1)', 'home-row', 'Unclear (2)', 'newer', 'older')]
        self.assertEqual(order, sorted(order))

    def test_empty_section_says_none(self):
        html = m.render(self.history([]), at(36000))
        self.assertEqual(html.count('None recorded.'), 3)

    def test_uptime_figures(self):
        html = m.render(self.history([self.row(0, 360, m.UNCLEAR), self.row(1000, 1036, m.ISP)]), at(36000))
        self.assertIn('98.90% uptime', html)
        self.assertIn('with 2 outages', html)
        self.assertIn('99.90% counting only confirmed ISP outages', html)

    def test_uses_local_time_with_dst(self):
        self.assertIn('November 02 2026 : 07:00:00 AM', m.render(self.history([self.row(0, 5, m.ISP)]), at(3600)))

    def test_ongoing_outage_shows_still_down(self):
        self.assertIn('Still down', m.render(self.history([self.row(0, None, m.ISP)]), at(3600)))

    def test_never_connected(self):
        self.assertIn("hasn't connected", m.render({'tracking_since': None, 'rows': []}, T0))


class Main(unittest.TestCase):
    def write_log(self, path, lines):
        with open(path, 'w') as f:
            f.writelines(lines)

    def test_writes_page_and_history_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as d:
            log, out, state_dir = os.path.join(d, 'c.log'), os.path.join(d, 'logs', 'index.html'), os.path.join(d, 'state')
            self.write_log(log, ALL_UP)
            m.main(log, out, state_dir)
            self.assertEqual(sorted(os.listdir(os.path.dirname(out))), ['index.html', 'outages.json'])
            self.assertEqual(os.listdir(state_dir), ['history.json'])

    def test_public_json_has_the_outages(self):
        with tempfile.TemporaryDirectory() as d:
            log, out = os.path.join(d, 'c.log'), os.path.join(d, 'index.html')
            self.write_log(log, ALL_UP + [line(10, m.LOST, c) for c in range(4)] + [line(50, m.REGISTERED, 0)])
            m.main(log, out, d)
            with open(os.path.join(d, 'outages.json')) as f:
                data = json.load(f)
            self.assertEqual(data['tracking_since'], m.to_time_str(T0))
            self.assertEqual(data['outages'][0]['start'], m.to_time_str(at(10)))
            self.assertEqual(data['outages'][0]['end'], m.to_time_str(at(50)))
            self.assertEqual(data['outages'][0]['section'], m.UNCLEAR)
            self.assertIn('updated', data)

    def test_history_survives_the_log_being_wiped(self):
        with tempfile.TemporaryDirectory() as d:
            log, out = os.path.join(d, 'c.log'), os.path.join(d, 'index.html')
            self.write_log(log, ALL_UP + [line(10, m.LOST, c) for c in range(4)] + [line(50, m.REGISTERED, 0)])
            m.main(log, out, d)
            self.write_log(log, [])
            m.main(log, out, d)
            with open(out) as f:
                self.assertIn('with 1 outages', f.read())

    def test_missing_log_leaves_the_existing_page_alone(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'index.html')
            with open(out, 'w') as f:
                f.write('old')
            with self.assertRaises(FileNotFoundError):
                m.main(os.path.join(d, 'missing.log'), out, d)
            with open(out) as f:
                self.assertEqual(f.read(), 'old')

    def test_corrupt_history_stops_rather_than_losing_it(self):
        with tempfile.TemporaryDirectory() as d:
            log, out, saved = os.path.join(d, 'c.log'), os.path.join(d, 'index.html'), os.path.join(d, 'history.json')
            self.write_log(log, ALL_UP)
            with open(saved, 'w') as f:
                f.write('{broken')
            with self.assertRaises(ValueError):
                m.main(log, out, d)
            with open(saved) as f:
                self.assertEqual(f.read(), '{broken')


if __name__ == '__main__':
    unittest.main()
