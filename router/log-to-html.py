#!/usr/bin/env python3
"""Turn the cloudflared log and probe log into a public outage history page.

cloudflared keeps 4 connections to Cloudflare, and they drop and reconnect
individually all the time. An outage is only counted when none are left.

updog-probe.sh records whether the modem and the internet were reachable, which
is how each outage gets sorted into ISP, not the ISP, or unclear. The tunnel log
is wiped on reboot, so finished outages are saved to the history file.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytz

LOG_PATH = '/var/log/cloudflared.log'
OUTPUT_PATH = '/www/logs/index.html'
STATE_DIR = '/root/updog'
TIMEZONE = pytz.timezone('US/Eastern')

REGISTERED = 'Registered tunnel connection'
LOST = 'Lost connection with the edge'
STARTING = 'Starting tunnel'
SHUTDOWN_PREFIX = 'Initiating graceful shutdown'

ISP = 'isp'
NOT_ISP = 'not_isp'
UNCLEAR = 'unclear'

CAUSE_ISP = 'ISP: modem was fine, internet was not'
CAUSE_REBOOT = 'Router reboot or power loss'
CAUSE_USB = 'USB adapter disconnected'
CAUSE_RESTART = 'Tunnel restart'
CAUSE_TUNNEL = 'Tunnel or Cloudflare problem: internet was fine'
CAUSE_LINK = 'Link to the modem dropped: modem, cable or ISP'
CAUSE_MODEM = 'Modem stopped responding'
CAUSE_SHORT = 'Too short to tell'
CAUSE_BEFORE = 'Before cause tracking started'

# The tunnel takes a while to notice the internet is gone, and the probe only
# checks every 15 seconds, so look slightly beyond the outage itself.
LOOK_BEFORE = timedelta(seconds=60)
LOOK_AFTER = timedelta(seconds=20)
# Below this, an ISP blip could fall between probe checks.
MIN_SECONDS_TO_CLEAR_ISP = 60
MIN_INTERNET_DOWN_SECONDS = 30

SECTIONS = [
    (ISP, "ISP", "The modem was reachable but the internet wasn't."),
    (NOT_ISP, "Not the ISP", "A restart, power loss, local hardware or the tunnel, while the internet itself was fine."),
    (UNCLEAR, "Unclear", "There isn't enough evidence to say whose fault these were."),
]

HTML_HEADER = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Internet Outages</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 20px;
            padding: 0;
            background-color: #f4f4f4;
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        th, td {
            padding: 10px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        th {
            background-color: #4CAF50;
            color: white;
        }
        tr:nth-child(even) {
            background-color: #f2f2f2;
        }
        .timestamp {
            color: #333;
        }
    </style>
</head>
<body>
    <h1>Matt's Internet Outages</h1>
"""

HTML_FOOTER = """</body>
</html>
"""


def parse_time(time_str):
    return datetime.fromisoformat(time_str.replace('Z', '+00:00'))


def to_time_str(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def read_events(lines):
    """Yield (time, message, connIndex) for each usable log line."""
    for line in lines:
        try:
            log = json.loads(line)
            yield parse_time(log['time']), log['message'], log.get('connIndex')
        except (ValueError, KeyError, TypeError, AttributeError):
            continue


def find_outages(events):
    """Return (outages, tracking_since).

    outages is a list of (start, end, restarted) tuples, oldest first. end is
    None if the outage is still going. restarted is True if it began with the
    tunnel being stopped or restarted. tracking_since is when the tunnel first
    connected. Time before that is left to the reboot check, because the log
    alone can't tell an internet outage from the router still coming up.
    """
    outages = []
    connections = set()
    tracking_since = None
    outage_start = None
    restarted = False

    for time, message, conn_index in events:
        if message == REGISTERED:
            connections.add(conn_index)
            if tracking_since is None:
                tracking_since = time
            if outage_start is not None:
                outages.append((outage_start, time, restarted))
                outage_start = None
        elif message == LOST:
            if conn_index in connections:
                connections.discard(conn_index)
                if not connections:
                    outage_start = time
                    restarted = False
        elif (message == STARTING or message.startswith(SHUTDOWN_PREFIX)) and connections:
            # A clean stop doesn't log LOST, and a crash logs nothing at all.
            connections.clear()
            outage_start = time
            restarted = True

    if outage_start is not None:
        outages.append((outage_start, None, restarted))
    return outages, tracking_since


def read_probe(lines):
    """Return (states, reboots) from the probe log, oldest first.

    states is a list of (time, state dict). reboots is a list of the times the
    router was last seen alive before going off.
    """
    states = []
    reboots = []
    for line in lines:
        try:
            log = json.loads(line)
            if log.get('event') == 'reboot':
                reboots.append(parse_time(log['down_since']))
            elif 'internet' in log:
                states.append((parse_time(log['time']), log))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return states, reboots


def states_during(states, start, end):
    """Yield (state, seconds) for each state that overlaps the time window."""
    for i, (state_start, state) in enumerate(states):
        state_end = states[i + 1][0] if i + 1 < len(states) else end
        seconds = (min(state_end, end) - max(state_start, start)).total_seconds()
        if seconds > 0:
            yield state, seconds


def classify(start, end, restarted, states, now):
    """Return (section, cause) for an outage."""
    if not states or start < states[0][0]:
        return (NOT_ISP, CAUSE_RESTART) if restarted else (UNCLEAR, CAUSE_BEFORE)

    actual_end = end or now
    seen = list(states_during(states, start - LOOK_BEFORE, actual_end + LOOK_AFTER))
    duration = (actual_end - start).total_seconds()

    if any(state['link'] == 'gone' for state, _ in seen):
        return NOT_ISP, CAUSE_USB
    if any(state['link'] == 'down' for state, _ in seen):
        return UNCLEAR, CAUSE_LINK

    internet_down = [(state, seconds) for state, seconds in seen if not state['internet']]
    if sum(seconds for _, seconds in internet_down) >= min(MIN_INTERNET_DOWN_SECONDS, duration / 2):
        if internet_down:
            modem_fine = all(state['modem'] for state, _ in internet_down)
            return (ISP, CAUSE_ISP) if modem_fine else (UNCLEAR, CAUSE_MODEM)

    if restarted:
        return NOT_ISP, CAUSE_RESTART
    if duration >= MIN_SECONDS_TO_CLEAR_ISP:
        return NOT_ISP, CAUSE_TUNNEL
    return UNCLEAR, CAUSE_SHORT


def update_history(history, outages, tracking_since, states, reboots, now):
    """Merge what the logs say into the saved history and return the new history.

    Finished outages keep the cause they were given when they ended, because
    the logs behind them eventually get wiped or trimmed.
    """
    rows = {row['start']: dict(row) for row in history.get('rows', [])}
    reboot_keys = {to_time_str(down_since) for down_since in reboots}
    current_keys = {to_time_str(start) for start, _, _ in outages}

    for down_since in reboots:
        key = to_time_str(down_since)
        if key in rows and rows[key]['end']:
            continue
        came_back = tracking_since if tracking_since and tracking_since > down_since else None
        rows[key] = {'start': key, 'end': to_time_str(came_back) if came_back else None,
                     'section': NOT_ISP, 'cause': CAUSE_REBOOT}

    # An outage still open when the router went off won't appear in the new log.
    for key, row in list(rows.items()):
        if row['end'] or key in current_keys or key in reboot_keys:
            continue
        start = parse_time(key)
        later_reboots = [down_since for down_since in reboots if down_since > start]
        if later_reboots:
            row['end'] = to_time_str(min(later_reboots))
        else:
            del rows[key]

    for start, end, restarted in outages:
        key = to_time_str(start)
        if key in rows and rows[key]['end']:
            continue
        section, cause = classify(start, end, restarted, states, now)
        rows[key] = {'start': key, 'end': to_time_str(end) if end else None,
                     'section': section, 'cause': cause}

    tracking = history.get('tracking_since')
    if tracking is None and tracking_since is not None:
        tracking = to_time_str(tracking_since)
    return {'tracking_since': tracking, 'rows': sorted(rows.values(), key=lambda row: row['start'])}


def format_time(dt):
    return dt.astimezone(TIMEZONE).strftime("%B %d %Y : %I:%M:%S %p")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} hr {minutes} min"
    if minutes:
        return f"{minutes} min {secs} sec"
    return f"{secs} sec"


def seconds_down(row, now):
    end = parse_time(row['end']) if row['end'] else now
    return (end - parse_time(row['start'])).total_seconds()


def uptime_percent(rows, tracked_seconds, now):
    if tracked_seconds <= 0:
        return 100.0
    return 100 * (1 - sum(seconds_down(row, now) for row in rows) / tracked_seconds)


def render_section(title, description, rows, now):
    parts = [f"    <h2>{title} ({len(rows)})</h2>\n    <p>{description}</p>\n"]
    if not rows:
        parts.append("    <p><em>None recorded.</em></p>\n")
        return parts
    parts.append("    <table>\n        <thead>\n            <tr><th>Started</th><th>Lasted</th><th>Cause</th></tr>\n")
    parts.append("        </thead>\n        <tbody>\n")
    for row in sorted(rows, key=lambda r: r['start'], reverse=True):
        lasted = format_duration(seconds_down(row, now)) if row['end'] else "Still down"
        parts.append(f"<tr><td class='timestamp'>{format_time(parse_time(row['start']))}</td>"
                     f"<td>{lasted}</td><td>{row['cause']}</td></tr>\n")
    parts.append("        </tbody>\n    </table>\n")
    return parts


def render(history, now):
    parts = [HTML_HEADER, f"    <p>Last updated: {format_time(now)}</p>\n"]
    rows = history['rows']

    if history['tracking_since'] is None:
        parts.append("    <p>The tunnel hasn't connected since the router last started.</p>\n")
    else:
        since = parse_time(history['tracking_since'])
        tracked = (now - since).total_seconds()
        overall = uptime_percent(rows, tracked, now)
        isp_only = uptime_percent([row for row in rows if row['section'] == ISP], tracked, now)
        parts.append(f"    <p><strong>{overall:.2f}% uptime</strong> with {len(rows)} outages "
                     f"since {format_time(since)}</p>\n")
        parts.append(f"    <p>{isp_only:.2f}% counting only confirmed ISP outages</p>\n")

    for section, title, description in SECTIONS:
        parts.extend(render_section(title, description, [r for r in rows if r['section'] == section], now))
    parts.append(HTML_FOOTER)
    return ''.join(parts)


def public_json(history, now):
    """The data behind the status page at updog.frostbit.in."""
    return json.dumps({'updated': to_time_str(now), 'tracking_since': history['tracking_since'],
                       'outages': history['rows']})


def write_atomic(path, text):
    # Rename into place so nothing can ever read a half-written file.
    temp_path = path + '.tmp'
    with open(temp_path, 'w', encoding='utf-8') as out_file:
        out_file.write(text)
    os.replace(temp_path, path)


def read_lines(path):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def main(log_path=LOG_PATH, output_path=OUTPUT_PATH, state_dir=STATE_DIR):
    with open(log_path, encoding='utf-8', errors='replace') as f:
        outages, tracking_since = find_outages(read_events(f))
    states, reboots = read_probe(read_lines(os.path.join(state_dir, 'probe.log')))

    # A corrupt history file stops the script here, rather than losing the history.
    history_path = os.path.join(state_dir, 'history.json')
    try:
        with open(history_path, encoding='utf-8') as f:
            saved_text = f.read()
        history = json.loads(saved_text)
    except FileNotFoundError:
        saved_text = None
        history = {}

    now = datetime.now(timezone.utc)
    history = update_history(history, outages, tracking_since, states, reboots, now)

    os.makedirs(state_dir, exist_ok=True)
    history_text = json.dumps(history, indent=1)
    if history_text != saved_text:
        write_atomic(history_path, history_text)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    write_atomic(output_path, render(history, now))
    write_atomic(os.path.join(os.path.dirname(output_path), 'outages.json'), public_json(history, now))


if __name__ == "__main__":
    main(*sys.argv[1:4])
