"""Sniffer CLI defaults (issue #17)."""
import os
import unittest
from unittest import mock

from hardware import sniffer


class DefaultPortTest(unittest.TestCase):
    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"SERIAL_PORT": "/dev/serial/by-id/x"}):
            self.assertEqual(sniffer.default_port(), "/dev/serial/by-id/x")

    def test_linux_default(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sniffer.sys, "platform", "linux"):
            self.assertEqual(sniffer.default_port(), "/dev/ttyUSB0")

    def test_mac_picks_first_usb_serial(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sniffer.sys, "platform", "darwin"), \
                mock.patch.object(sniffer.glob, "glob", return_value=["/dev/cu.usbserial-2", "/dev/cu.usbserial-130"]):
            self.assertEqual(sniffer.default_port(), "/dev/cu.usbserial-130")

    def test_mac_without_adapter_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sniffer.sys, "platform", "darwin"), \
                mock.patch.object(sniffer.glob, "glob", return_value=[]):
            self.assertEqual(sniffer.default_port(), "/dev/ttyUSB0")

    def test_help_runs(self):
        with mock.patch.object(sniffer.sys, "argv", ["sniffer", "--help"]), self.assertRaises(SystemExit) as cm, \
                mock.patch("sys.stdout"):
            sniffer.main()
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
