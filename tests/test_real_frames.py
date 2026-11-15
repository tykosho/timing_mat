"""The parser against traffic captured from the real R200 on the Pi (issue #7).

tests/fixtures/r200_capture_2026-11-15.log was recorded with RAW_CAPTURE_PATH (issue #51):
about 80 s of inventory with two tags passed over the antenna. Between tag reads the module
answers each poll with an error response, AA 01 FF 00 01 15 16 DD (0x15 = no tag found).
"""
import os
import unittest
from collections import Counter

from hardware.frame_capture import iter_capture, load_rx_bytes
from hardware.r200_protocol import (
    READ_MULTI_COMMAND, calculate_checksum, extract_frames, parse_inventory_response
)
from hardware.serial_reader import SerialReader

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "r200_capture_2026-11-15.log")
TAG_A = "E28069150000401D63E5E43D"
TAG_B = "E28069150000501D63E6A83D"
NO_TAG = bytes.fromhex("AA01FF00011516DD")
EXPECTED_READS = [
    (TAG_A, -39.0), (TAG_A, -39.0), (TAG_A, -50.0), (TAG_A, -38.0),
    (TAG_B, -46.0), (TAG_B, -44.0), (TAG_A, -55.0), (TAG_A, -57.0),
]


class RealCaptureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_rx_bytes(FIXTURE)
        cls.frames, cls.rest = extract_frames(bytearray(cls.data))

    def test_every_received_byte_is_part_of_a_frame(self):
        self.assertEqual(sum(map(len, self.frames)), len(self.data))
        self.assertEqual(self.rest, bytearray())

    def test_checksum_rule_matches_the_module(self):
        """8-bit sum of type..payload: confirmed on every captured frame."""
        for frame in self.frames:
            self.assertEqual(calculate_checksum(frame[1:-2]), frame[-2], frame.hex())

    def test_only_tag_reads_and_no_tag_responses(self):
        kinds = Counter(f if f == NO_TAG else f[1:3] for f in self.frames)
        self.assertEqual(kinds[bytes([0x02, 0x22])], len(EXPECTED_READS))
        self.assertEqual(kinds[NO_TAG], len(self.frames) - len(EXPECTED_READS))

    def test_tag_reads_parse_to_epc_and_dbm(self):
        reads = [r for r in map(parse_inventory_response, self.frames) if r]
        self.assertEqual(reads, EXPECTED_READS)

    def test_pc_word_count_matches_the_epc_length(self):
        for frame in self.frames:
            if frame[1] == 0x02:
                epc_len = ((frame[3] << 8) | frame[4]) - 5  # RSSI + PC + EPC + CRC
                self.assertEqual((frame[6] >> 3) * 2, epc_len)

    def test_serial_reader_on_the_captured_chunks(self):
        """Feed the chunks in the sizes the port actually returned them."""
        reads = []
        reader = SerialReader(on_tag_read=reads.append)
        buffer = bytearray()
        with open(FIXTURE) as f:
            for kind, _, text in iter_capture(f):
                if kind == "rx":
                    buffer.extend(bytes.fromhex(text))
                    buffer = reader._process_stream(buffer)
        self.assertEqual([(r.epc, r.rssi) for r in reads], EXPECTED_READS)
        self.assertEqual(buffer, bytearray())

    def test_capture_starts_with_the_inventory_command(self):
        with open(FIXTURE) as f:
            tx = [text for kind, _, text in iter_capture(f) if kind == "tx"]
        self.assertEqual(tx[0], READ_MULTI_COMMAND.hex().upper())


if __name__ == "__main__":
    unittest.main()
