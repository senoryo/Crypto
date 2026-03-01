"""Tests for shared/fix_engine.py — FIXWireMessage, FIXSession, signature."""

import base64

from shared.fix_engine import (
    FIXWireMessage,
    FIXSession,
    SOH_CHR,
    build_coinbase_logon_signature,
)


# -----------------------------------------------------------------------
# TestFIXWireMessage
# -----------------------------------------------------------------------

class TestFIXWireMessage:

    def test_set_and_get(self):
        msg = FIXWireMessage()
        msg.set(35, "D")
        assert msg.get(35) == "D"

    def test_get_default(self):
        msg = FIXWireMessage()
        assert msg.get(999) == ""
        assert msg.get(999, "X") == "X"

    def test_get_int(self):
        msg = FIXWireMessage()
        msg.set(38, "100")
        assert msg.get_int(38) == 100
        assert msg.get_int(999) == 0

    def test_get_int_invalid_returns_default(self):
        msg = FIXWireMessage()
        msg.set(38, "abc")
        assert msg.get_int(38) == 0
        assert msg.get_int(38, 42) == 42

    def test_get_float(self):
        msg = FIXWireMessage()
        msg.set(44, "67500.50")
        assert msg.get_float(44) == 67500.50
        assert msg.get_float(999) == 0.0

    def test_get_float_invalid_returns_default(self):
        msg = FIXWireMessage()
        msg.set(44, "bad")
        assert msg.get_float(44) == 0.0
        assert msg.get_float(44, 1.5) == 1.5

    def test_get_all_repeating_tags(self):
        msg = FIXWireMessage()
        msg._fields.append((269, "0"))
        msg._fields.append((269, "1"))
        msg._fields.append((269, "2"))
        assert msg.get_all(269) == ["0", "1", "2"]

    def test_get_all_empty(self):
        msg = FIXWireMessage()
        assert msg.get_all(999) == []

    def test_msg_type_property(self):
        msg = FIXWireMessage()
        msg.set(35, "A")
        assert msg.msg_type == "A"

    def test_set_chaining(self):
        msg = FIXWireMessage().set(35, "D").set(55, "BTC/USD").set(54, "1")
        assert msg.get(35) == "D"
        assert msg.get(55) == "BTC/USD"
        assert msg.get(54) == "1"

    def test_set_replaces_existing(self):
        msg = FIXWireMessage()
        msg.set(44, "100.0")
        msg.set(44, "200.0")
        assert msg.get(44) == "200.0"
        # Should only have one entry for tag 44
        assert len([v for t, v in msg._fields if t == 44]) == 1

    def test_encode_produces_bytes(self):
        msg = FIXWireMessage()
        msg.set(35, "D")
        msg.set(55, "BTC/USD")
        encoded = msg.encode()
        assert isinstance(encoded, bytes)

    def test_encode_has_begin_string(self):
        msg = FIXWireMessage()
        msg.set(35, "D")
        encoded = msg.encode().decode("ascii")
        assert encoded.startswith("8=FIXT.1.1")

    def test_encode_has_body_length_and_checksum(self):
        msg = FIXWireMessage()
        msg.set(35, "0")
        encoded = msg.encode().decode("ascii")
        assert "9=" in encoded
        assert "10=" in encoded

    def test_decode_roundtrip(self):
        original = FIXWireMessage()
        original.set(35, "D")
        original.set(55, "ETH/USD")
        original.set(54, "2")
        original.set(38, "50")
        wire = original.encode()
        decoded = FIXWireMessage.decode(wire)
        assert decoded.get(35) == "D"
        assert decoded.get(55) == "ETH/USD"
        assert decoded.get(54) == "2"
        assert decoded.get(38) == "50"


# -----------------------------------------------------------------------
# TestFIXSession
# -----------------------------------------------------------------------

class TestFIXSession:

    def test_sequence_number_increment(self):
        session = FIXSession("SENDER", "TARGET")
        assert session.next_send_seq() == 1
        assert session.next_send_seq() == 2
        assert session.next_send_seq() == 3

    def test_stamp_message_adds_fields(self):
        session = FIXSession("MY_API_KEY", "Coinbase")
        msg = FIXWireMessage()
        msg.set(35, "D")
        session.stamp_message(msg)
        assert msg.get(49) == "MY_API_KEY"    # SenderCompID
        assert msg.get(56) == "Coinbase"       # TargetCompID
        assert msg.get_int(34) == 1            # MsgSeqNum
        assert msg.get(52) != ""               # SendingTime

    def test_stamp_increments_sequence(self):
        session = FIXSession("S", "T")
        msg1 = FIXWireMessage().set(35, "D")
        msg2 = FIXWireMessage().set(35, "D")
        session.stamp_message(msg1)
        session.stamp_message(msg2)
        assert msg1.get_int(34) == 1
        assert msg2.get_int(34) == 2

    def test_reset(self):
        session = FIXSession("S", "T")
        session.next_send_seq()
        session.next_send_seq()
        session.advance_recv_seq()
        session.reset()
        assert session.next_send_seq() == 1
        assert session.last_recv_seq == 0

    def test_advance_recv_seq(self):
        session = FIXSession("S", "T")
        assert session.last_recv_seq == 0
        session.advance_recv_seq()
        assert session.last_recv_seq == 1
        session.advance_recv_seq()
        assert session.last_recv_seq == 2


# -----------------------------------------------------------------------
# TestCoinbaseLogonSignature
# -----------------------------------------------------------------------

class TestCoinbaseLogonSignature:

    def _make_secret(self) -> str:
        """Generate a valid base64-encoded secret for testing."""
        return base64.b64encode(b"test-secret-key-32-bytes-long!!!" ).decode()

    def test_valid_base64_output(self):
        secret = self._make_secret()
        sig = build_coinbase_logon_signature(
            "20240101-00:00:00.000", "A", 1, "SENDER", "Coinbase", "pass", secret
        )
        decoded = base64.b64decode(sig)
        assert len(decoded) == 32  # SHA-256 produces 32 bytes

    def test_deterministic(self):
        secret = self._make_secret()
        args = ("20240101-00:00:00.000", "A", 1, "SENDER", "Coinbase", "pass", secret)
        sig1 = build_coinbase_logon_signature(*args)
        sig2 = build_coinbase_logon_signature(*args)
        assert sig1 == sig2

    def test_changes_with_different_input(self):
        secret = self._make_secret()
        sig1 = build_coinbase_logon_signature(
            "20240101-00:00:00.000", "A", 1, "SENDER", "Coinbase", "pass", secret
        )
        sig2 = build_coinbase_logon_signature(
            "20240101-00:00:01.000", "A", 1, "SENDER", "Coinbase", "pass", secret
        )
        assert sig1 != sig2
