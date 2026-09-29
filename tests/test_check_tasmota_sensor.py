"""Tests for check_tasmota_sensor against a fake Tasmota HTTP command API."""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import check_tasmota_sensor as plugin

SRC = os.path.dirname(os.path.dirname(os.path.abspath(plugin.__file__)))

STATUS_SNS = {
    "Time": "2026-09-29T10:00:00",
    "DS18B20": {"Id": "01144A2B", "Temperature": 15.1},
    "SHT3X": {"Temperature": 24.0, "Humidity": 45.0, "DewPoint": 11.9},
    "ANALOG": {"Temperature": 30.0},
    "TempUnit": "C",
}


class FakeTasmota(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        command = query.get("cmnd", [""])[0]
        payload = {"StatusSNS": STATUS_SNS} if command == "Status 8" else {"Command": "Unknown"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestCompare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeTasmota)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_plugin(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "check_tasmota_sensor", "-H", "127.0.0.1", "-p", str(self.port)] + list(argv),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            env=dict(os.environ, PYTHONPATH=SRC), timeout=60)

    def expect(self, state, pattern, *argv):
        result = self.run_plugin(*argv)
        self.assertEqual(state, result.returncode, result)
        self.assertRegex(result.stdout, pattern)
        self.assertEqual(1, len(result.stdout.splitlines()), result)

    def compare(self, entity, other, must_be, warning, critical, *extra):
        return ["--check", "compare", "--entity", entity, "--compare-entity", other, "--must-be", must_be,
                "-w", str(warning), "-c", str(critical)] + list(extra)

    def test_sensor(self):
        self.expect(plugin.STATE_OK, r"^OK: DS18B20.Temperature is 15.1C \| ", "--check", "sensor",
                    "--entity", "DS18B20.Temperature", "-w", "30", "-c", "35")

    def test_dew_point_ok(self):
        self.expect(plugin.STATE_OK, r"^OK: DS18B20.Temperature is 3.2C above SHT3X.DewPoint \(15.1C vs 11.9C\) \| "
                                     r"'margin'=3.2C;3;1 'DS18B20.Temperature'=15.1C 'SHT3X.DewPoint'=11.9C$",
                    *self.compare("DS18B20.Temperature", "SHT3X.DewPoint", "above", 3, 1))

    def test_close(self):
        self.expect(plugin.STATE_WARNING, r"^WARNING: DS18B20.Temperature is 3.2C above",
                    *self.compare("DS18B20.Temperature", "SHT3X.DewPoint", "above", 4, 1))
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: DS18B20.Temperature is 3.2C above",
                    *self.compare("DS18B20.Temperature", "SHT3X.DewPoint", "above", 5, 3.2))

    def test_wrong_side(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: SHT3X.DewPoint is 3.2C below, not above, "
                                           r"DS18B20.Temperature \(11.9C vs 15.1C\) \| 'margin'=-3.2C;3;1 ",
                    *self.compare("SHT3X.DewPoint", "DS18B20.Temperature", "above", 3, 1))

    def test_below(self):
        self.expect(plugin.STATE_OK, r"^OK: SHT3X.Temperature is 6C below ANALOG.Temperature \(24C vs 30C\) "
                                     r"\| 'margin'=6C;2;0 ",
                    *self.compare("SHT3X.Temperature", "ANALOG.Temperature", "below", 2, 0))
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: ANALOG.Temperature is 6C above, not below",
                    *self.compare("ANALOG.Temperature", "SHT3X.Temperature", "below", 2, 0))

    def test_uom_override(self):
        # Tasmota's TempUnit is "C"; --uom replaces it in the text and the perfdata
        self.expect(plugin.STATE_OK, r"^OK: ANALOG.Temperature is 18.1K above SHT3X.DewPoint .* "
                                     r"'margin'=18.1K;3;1 'ANALOG.Temperature'=30K 'SHT3X.DewPoint'=11.9K$",
                    *self.compare("ANALOG.Temperature", "SHT3X.DewPoint", "above", 3, 1, "--uom", "K"))

    def test_not_numeric(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Value of DS18B20.Id is not numeric: '01144A2B'$",
                    *self.compare("DS18B20.Id", "SHT3X.DewPoint", "above", 3, 1))

    def test_unknown_field(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Unknown entity 'SHT3X.Nothing'",
                    *self.compare("DS18B20.Temperature", "SHT3X.Nothing", "above", 3, 1))

    def test_usage(self):
        result = self.run_plugin(*self.compare("DS18B20.Temperature", "SHT3X.DewPoint", "above", 1, 3))
        self.assertEqual(plugin.STATE_UNKNOWN, result.returncode, result)
        self.assertIn("-w must not be smaller than -c", result.stderr)

    def test_usage_errors_are_unknown(self):
        # argparse would exit 2, which Nagios reads as CRITICAL
        for argv in (["-Z"], [], ["--check", "sensor"], ["--check", "nope"], ["-p", "abc", "--list"],
                     ["--check", "sensor", "--entity", "x", "-w", "abc", "-c", "1"],
                     ["--check", "text", "--text-entity", "x", "--regex", "("]):
            result = self.run_plugin(*argv)
            self.assertEqual(plugin.STATE_UNKNOWN, result.returncode, (argv, result))
            self.assertIn("check_tasmota_sensor: error: ", result.stderr)


if __name__ == "__main__":
    unittest.main()
