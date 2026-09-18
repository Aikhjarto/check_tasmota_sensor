# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""check_tasmota_sensor - Nagios/Icinga plugin for Tasmota devices, via
Tasmota's HTTP command API (GET http://<host>/cm?cmnd=<command>).

If a web admin password is set on the device, pass -u/-P; Tasmota expects
these as "user"/"password" query parameters on every /cm request (not as
HTTP Basic auth), so they are sent that way here too.

Four kinds of checks (--check):
  sensor  Compare a numeric value from "Status 8" (StatusSNS) against
          -w/-c thresholds. Entities are addressed as "<Module>.<Field>",
          e.g. "BME280.Temperature" or "ENERGY.Power" -- Tasmota nests
          sensor readings one level under the module/sensor name. Unlike
          most plugins, -c may be smaller than -w: whichever of the two
          is larger decides whether higher or lower values are worse.
  time    Compare the device's own reported time (the "Time" field,
          present in every Status response) against the local clock
          (default) or an NTP server (--time-server), in seconds of
          offset.
  binary  Evaluate a boolean expression (and/or/not/parentheses) over
          one or more POWER-style relay states from "Status 11"
          (StatusSTS), e.g. --binary-expr "POWER1 and not POWER2" on a
          multi-relay device, or plain "POWER" on a single-relay one.
  text    Match a value from "Status 8" (StatusSNS, same "<Module>.<Field>"
          addressing as "sensor") against a Python regular expression
          (--regex), e.g. to check a string-valued field.

Pass --list instead of --check to print the available sensor fields (as
"<Module>.<Field>") and POWER-style relay states, with their current
value, so you know what to pass to --entity/--text-entity/--binary-expr.

Automatic unit of measurement is only attempted for fields literally
named "Temperature" or "Pressure"/"PressureAtSeaLevel" (from the global
"TempUnit"/"PressureUnit" hints Tasmota provides) -- unlike ESPHome,
Tasmota does not report a unit per sensor field in general, so --uom
should be given explicitly for anything else.
"""
import argparse
import ast
import re
import socket
import struct
import sys
import time
from datetime import datetime

STATE_OK = 0
STATE_WARNING = 1
STATE_CRITICAL = 2
STATE_UNKNOWN = 3
STATE_NAMES = {
    STATE_OK: "OK",
    STATE_WARNING: "WARNING",
    STATE_CRITICAL: "CRITICAL",
    STATE_UNKNOWN: "UNKNOWN",
}
STATE_BY_NAME = {"ok": STATE_OK, "warning": STATE_WARNING, "critical": STATE_CRITICAL}

NTP_EPOCH_OFFSET = 2208988800  # seconds between 1900-01-01 and 1970-01-01


def die(state, message):
    print(f"{STATE_NAMES[state]}: {message}")
    sys.exit(state)


# ---------------------------------------------------------------------------
# Tasmota HTTP command API
# ---------------------------------------------------------------------------

def tasmota_command(args, cmnd):
    """Issue one Tasmota HTTP command (GET /cm?cmnd=...) and return the
    parsed JSON response as a dict."""
    try:
        import requests
    except ImportError:
        die(STATE_UNKNOWN, "python3 module 'requests' is required")

    params = {"cmnd": cmnd}
    if args.username:
        params["user"] = args.username
        params["password"] = args.password or ""

    url = f"http://{args.host}:{args.port}/cm"
    try:
        resp = requests.get(url, params=params, timeout=args.timeout)
    except requests.exceptions.RequestException as exc:
        die(STATE_UNKNOWN, f"HTTP request to {url} failed: {exc}")
    if resp.status_code == 401:
        die(STATE_UNKNOWN, f"HTTP authentication failed for {url}")
    if resp.status_code != 200:
        die(STATE_UNKNOWN, f"HTTP request to {url} returned status {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        die(STATE_UNKNOWN, f"Could not parse JSON response from {url}: {resp.text[:200]!r}")
    if isinstance(data, dict) and data.get("Command") == "Unknown":
        die(STATE_UNKNOWN, f"Tasmota did not recognize command {cmnd!r}")
    if isinstance(data, dict) and "WARNING" in data:
        die(STATE_UNKNOWN, f"Tasmota returned a warning for {cmnd!r}: {data['WARNING']}")
    return data


def fetch_status_sns(args):
    """"Status 8" -- sensor readings, nested one level under a module
    name (e.g. {"BME280": {"Temperature": 26.3, ...}, "Time": "...", ...})."""
    return tasmota_command(args, "Status 8").get("StatusSNS", {})


def fetch_status_sts(args):
    """"Status 11" -- runtime status, including flat POWER/POWER1/...
    relay states and "Time"."""
    return tasmota_command(args, "Status 11").get("StatusSTS", {})


# module/meta keys inside StatusSNS that are not nested sensor modules
STATUS_SNS_META_KEYS = ("Time", "TempUnit", "PressureUnit")

# best-effort unit of measurement, from Tasmota's global hints; Tasmota
# does not report a per-field unit in general, unlike ESPHome.
FIELD_UOM_FROM_META = {
    "temperature": "TempUnit",
    "pressure": "PressureUnit",
    "pressureatsealevel": "PressureUnit",
}


def get_sensor_field(status_sns, entity):
    """entity is "<Module>.<Field>", e.g. "BME280.Temperature"."""
    if "." not in entity:
        die(STATE_UNKNOWN, f"--entity/--text-entity must be \"<Module>.<Field>\", got {entity!r}")
    module, _, field = entity.partition(".")
    module_data = status_sns.get(module)
    if not isinstance(module_data, dict) or field not in module_data:
        die(STATE_UNKNOWN, f"Unknown entity {entity!r} (module {module!r} or field {field!r} not found)")
    uom = None
    meta_key = FIELD_UOM_FROM_META.get(field.lower())
    if meta_key:
        uom = status_sns.get(meta_key)
    return module_data[field], uom


# ---------------------------------------------------------------------------
# entity listing (--list)
# ---------------------------------------------------------------------------

def list_entities(args):
    status_sns = fetch_status_sns(args)
    sensors = []
    for module, fields in status_sns.items():
        if module in STATUS_SNS_META_KEYS or not isinstance(fields, dict):
            continue
        for field, value in fields.items():
            sensors.append((f"{module}.{field}", value))

    status_sts = fetch_status_sts(args)
    relays = [(key, value) for key, value in status_sts.items() if key.startswith("POWER")]

    return sensors, relays


def print_entity_list(sensors, relays, host):
    print(f"Available entities on {host}:")
    if sensors:
        print("  sensor/text (Status 8, use as --entity/--text-entity):")
        for name, value in sorted(sensors):
            print(f"    {name} = {value}")
    if relays:
        print("  binary (Status 11, use in --binary-expr):")
        for name, value in sorted(relays):
            print(f"    {name} = {value}")
    if not sensors and not relays:
        print("  (none found)")


# ---------------------------------------------------------------------------
# threshold evaluation (direction-agnostic: -c may be smaller than -w)
# ---------------------------------------------------------------------------

def evaluate_numeric(value, warning, critical, label, uom=""):
    if warning is not None and critical is not None and warning > critical:
        # lower values are worse (e.g. battery / signal level style checks)
        if value <= critical:
            state = STATE_CRITICAL
        elif value <= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK
    else:
        # higher values are worse (the common case)
        if critical is not None and value >= critical:
            state = STATE_CRITICAL
        elif warning is not None and value >= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK

    perf_warn = "" if warning is None else warning
    perf_crit = "" if critical is None else critical
    message = f"{label} is {value}{uom} | '{label}'={value}{uom};{perf_warn};{perf_crit};;"
    return state, message


# ---------------------------------------------------------------------------
# time-offset check
# ---------------------------------------------------------------------------

def query_ntp(server, timeout):
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (server, 123))
            data, _ = sock.recvfrom(48)
        except OSError as exc:
            die(STATE_UNKNOWN, f"Could not query NTP server {server}: {exc}")
    if len(data) < 48:
        die(STATE_UNKNOWN, f"Short NTP response from {server}")
    transmit_timestamp = struct.unpack("!12I", data)[10]
    return transmit_timestamp - NTP_EPOCH_OFFSET


def get_reference_time(time_server, timeout):
    if not time_server:
        return time.time()
    return query_ntp(time_server, timeout)


def parse_device_time(value):
    """Tasmota's "Time" field has no UTC offset suffix and is local wall-clock
    time (per the device's configured Timezone setting), not UTC -- e.g. it
    reports "16:44" when UTC is "14:44" for a UTC+2 device. Leaving the parsed
    datetime naive and letting datetime.timestamp() interpret it lets Python
    assume it is local time in *this host's* timezone, which is correct as
    long as this plugin runs in the same timezone as the device."""
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        die(STATE_UNKNOWN, f"Could not parse time value: {text!r}")
    return dt.timestamp()


# ---------------------------------------------------------------------------
# binary (POWER) logical expression check
# ---------------------------------------------------------------------------

ALLOWED_EXPR_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def parse_bool_expr(expr):
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        die(STATE_UNKNOWN, f"Invalid --binary-expr: {exc}")

    def check(node):
        if not isinstance(node, ALLOWED_EXPR_NODES):
            die(STATE_UNKNOWN, f"Unsupported element in --binary-expr: {type(node).__name__}")
        for child in ast.iter_child_nodes(node):
            check(child)

    check(tree)
    return tree


def extract_names(tree):
    """Names referenced in the expression, either as bare Python
    identifiers (e.g. POWER1) or as quoted string literals for names
    that aren't valid identifiers."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return sorted(names)


def eval_bool_expr(tree, values):
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            results = [ev(v) for v in node.values]
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.UnaryOp):
            return not ev(node.operand)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise KeyError(node.id)
            return bool(values[node.id])
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                if node.value not in values:
                    raise KeyError(node.value)
                return bool(values[node.value])
            return bool(node.value)
        die(STATE_UNKNOWN, "Unsupported element in --binary-expr")

    return ev(tree)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Check Tasmota devices via the HTTP command API (/cm?cmnd=...).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -H tasmota.local --check sensor --entity BME280.Temperature -w 30 -c 35\n"
            "  %(prog)s -H tasmota.local --check sensor --entity ENERGY.Power -w 2000 -c 2500\n"
            "  %(prog)s -H tasmota.local --check time -w 5 -c 30\n"
            "  %(prog)s -H tasmota.local --check binary --binary-expr 'POWER1 and not POWER2'\n"
            "  %(prog)s -H tasmota.local --check text --text-entity BME280.Temperature --regex '^2'\n"
            "  %(prog)s -H tasmota.local --list\n"
        ),
    )
    parser.add_argument("-H", "--host", required=True, help="Tasmota device hostname or IP address")
    parser.add_argument("-p", "--port", type=int, default=80, help="port to connect to (default: 80)")
    parser.add_argument(
        "-t", "--timeout", type=float, default=15,
        help="timeout in seconds (default: 15; Tasmota devices over WiFi can be slow to respond)",
    )
    parser.add_argument("-u", "--username", help="web admin username (Tasmota expects this as a query parameter)")
    parser.add_argument("-P", "--password", help="web admin password (sent as a query parameter, like Tasmota itself does)")

    parser.add_argument(
        "--list", action="store_true",
        help="list available sensor fields and POWER-style relay states, with their "
             "current value, and exit, instead of running a check",
    )
    parser.add_argument("--check", choices=("sensor", "time", "binary", "text"), help="type of check to perform")

    sensor_group = parser.add_argument_group("--check sensor")
    sensor_group.add_argument("--entity", help='sensor field as "<Module>.<Field>", e.g. "BME280.Temperature"')
    sensor_group.add_argument("-w", "--warning", type=float, help="warning threshold (may be > or < -c)")
    sensor_group.add_argument("-c", "--critical", type=float, help="critical threshold (may be > or < -w)")
    sensor_group.add_argument(
        "--uom", default="",
        help="unit of measurement for performance data (default: retrieved from the "
             "device for fields literally named Temperature/Pressure, empty otherwise)",
    )

    time_group = parser.add_argument_group("--check time")
    time_group.add_argument("--time-server", help="NTP server to compare against (default: local clock)")

    binary_group = parser.add_argument_group("--check binary")
    binary_group.add_argument(
        "--binary-expr",
        help="boolean expression over POWER-style relay states, e.g. 'POWER1 and not POWER2'; "
             "quote names that aren't valid Python identifiers",
    )
    binary_group.add_argument("--true-state", choices=("ok", "warning", "critical"), default="critical")
    binary_group.add_argument("--false-state", choices=("ok", "warning", "critical"), default="ok")

    text_group = parser.add_argument_group("--check text")
    text_group.add_argument("--text-entity", help='sensor field as "<Module>.<Field>" to match')
    text_group.add_argument("--regex", help="Python regular expression to search for in the field's value")
    text_group.add_argument("-i", "--ignore-case", action="store_true", help="match --regex case-insensitively")
    text_group.add_argument("--match-state", choices=("ok", "warning", "critical"), default="ok")
    text_group.add_argument("--no-match-state", choices=("ok", "warning", "critical"), default="critical")

    args = parser.parse_args()

    if args.list:
        return args

    if not args.check:
        parser.error("--check is required (or use --list)")

    if args.check == "sensor":
        if not args.entity or args.warning is None or args.critical is None:
            parser.error("--check sensor requires --entity, -w and -c")
    elif args.check == "time":
        if args.warning is None or args.critical is None:
            parser.error("--check time requires -w and -c")
    elif args.check == "binary":
        if not args.binary_expr:
            parser.error("--check binary requires --binary-expr")
    elif args.check == "text":
        if not args.text_entity or not args.regex:
            parser.error("--check text requires --text-entity and --regex")
        try:
            re.compile(args.regex, re.IGNORECASE if args.ignore_case else 0)
        except re.error as exc:
            parser.error(f"Invalid --regex: {exc}")

    return args


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.list:
        sensors, relays = list_entities(args)
        print_entity_list(sensors, relays, args.host)
        sys.exit(STATE_OK)

    if args.check == "sensor":
        status_sns = fetch_status_sns(args)
        value, auto_uom = get_sensor_field(status_sns, args.entity)
        try:
            value = float(value)
        except (TypeError, ValueError):
            die(STATE_UNKNOWN, f"Value of {args.entity} is not numeric: {value!r}")
        uom = args.uom or auto_uom or ""
        state, message = evaluate_numeric(value, args.warning, args.critical, args.entity, uom)
        die(state, message)

    elif args.check == "time":
        status_sns = fetch_status_sns(args)
        if "Time" not in status_sns:
            die(STATE_UNKNOWN, "Device did not report a 'Time' field")
        device_epoch = parse_device_time(status_sns["Time"])
        reference_epoch = get_reference_time(args.time_server, args.timeout)
        offset = abs(device_epoch - reference_epoch)
        state, message = evaluate_numeric(offset, args.warning, args.critical, "time offset", "s")
        die(state, message)

    elif args.check == "binary":
        tree = parse_bool_expr(args.binary_expr)
        names = extract_names(tree)
        if not names:
            die(STATE_UNKNOWN, "No relay names found in --binary-expr")
        status_sts = fetch_status_sts(args)
        missing = [n for n in names if n not in status_sts]
        if missing:
            die(STATE_UNKNOWN, f"Unknown relay/relays: {', '.join(sorted(missing))}")
        values = {n: status_sts[n] == "ON" for n in names}
        try:
            result = eval_bool_expr(tree, values)
        except KeyError as exc:
            die(STATE_UNKNOWN, f"Unknown relay in expression: {exc}")
        state = STATE_BY_NAME[args.true_state if result else args.false_state]
        details = ", ".join(f"{n}={'ON' if values[n] else 'OFF'}" for n in names)
        die(state, f"'{args.binary_expr}' is {result} ({details})")

    elif args.check == "text":
        status_sns = fetch_status_sns(args)
        value, _auto_uom = get_sensor_field(status_sns, args.text_entity)
        text = str(value)
        flags = re.IGNORECASE if args.ignore_case else 0
        matched = re.search(args.regex, text, flags) is not None
        state = STATE_BY_NAME[args.match_state if matched else args.no_match_state]
        die(state, f"{args.text_entity} = {text!r} {'matches' if matched else 'does not match'} /{args.regex}/")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die(STATE_UNKNOWN, "Interrupted")
