#!/usr/bin/env python3
"""Turn the cloudflared log into a public outage history page.

cloudflared keeps 4 connections to Cloudflare, and they drop and reconnect
individually all the time. An outage is only counted when none are left.
"""

import json
import os
import sys
from datetime import datetime, timezone

import pytz

LOG_PATH = '/var/log/cloudflared.log'
OUTPUT_PATH = '/www/logs/index.html'
TIMEZONE = pytz.timezone('US/Eastern')

REGISTERED = 'Registered tunnel connection'
LOST = 'Lost connection with the edge'
STARTING = 'Starting tunnel'
SHUTDOWN_PREFIX = 'Initiating graceful shutdown'

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

HTML_FOOTER = """        </tbody>
    </table>
</body>
</html>
"""


def parse_time(time_str):
    return datetime.fromisoformat(time_str.replace('Z', '+00:00'))


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

    outages is a list of (start, end) pairs, oldest first. end is None if the
    outage is still going. tracking_since is when the tunnel first connected.
    Time before that is ignored, because at boot we can't tell an internet
    outage from the router still coming up.
    """
    outages = []
    connections = set()
    tracking_since = None
    outage_start = None

    for time, message, conn_index in events:
        if message == REGISTERED:
            connections.add(conn_index)
            if tracking_since is None:
                tracking_since = time
            if outage_start is not None:
                outages.append((outage_start, time))
                outage_start = None
        elif message == LOST:
            if conn_index in connections:
                connections.discard(conn_index)
                if not connections:
                    outage_start = time
        elif (message == STARTING or message.startswith(SHUTDOWN_PREFIX)) and connections:
            # A clean stop doesn't log LOST, and a crash logs nothing at all.
            connections.clear()
            outage_start = time

    if outage_start is not None:
        outages.append((outage_start, None))
    return outages, tracking_since


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


def render(outages, tracking_since, now):
    parts = [HTML_HEADER]
    parts.append(f"    <p>Last updated: {format_time(now)}</p>\n")

    if tracking_since is None:
        parts.append("    <p>The tunnel hasn't connected since the router last started.</p>\n")
    else:
        downtime = sum(((end or now) - start).total_seconds() for start, end in outages)
        tracked = (now - tracking_since).total_seconds()
        uptime = 100 * (1 - downtime / tracked) if tracked > 0 else 100
        parts.append(
            f"    <p><strong>{uptime:.2f}% uptime</strong> with {len(outages)} outages "
            f"since {format_time(tracking_since)}</p>\n"
        )

    parts.append("    <p>Most recent first</p>\n")
    parts.append("    <table>\n        <thead>\n            <tr><th>Started</th><th>Lasted</th></tr>\n")
    parts.append("        </thead>\n        <tbody>\n")
    for start, end in reversed(outages):
        lasted = format_duration((end - start).total_seconds()) if end else "Still down"
        parts.append(f"<tr><td class='timestamp'>{format_time(start)}</td><td>{lasted}</td></tr>\n")
    parts.append(HTML_FOOTER)
    return ''.join(parts)


def main(log_path=LOG_PATH, output_path=OUTPUT_PATH):
    with open(log_path, encoding='utf-8', errors='replace') as f:
        outages, tracking_since = find_outages(read_events(f))

    html = render(outages, tracking_since, datetime.now(timezone.utc))

    # Rename into place so a visitor can never load a half-written page.
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    temp_path = output_path + '.tmp'
    with open(temp_path, 'w', encoding='utf-8') as out_file:
        out_file.write(html)
    os.replace(temp_path, output_path)


if __name__ == "__main__":
    main(*sys.argv[1:3])
