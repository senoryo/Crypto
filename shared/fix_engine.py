"""
Async FIX 5.0 SP2 TCP+SSL Client Engine for Coinbase Exchange.

Pure stdlib implementation (asyncio, ssl, hmac, hashlib, base64).
Provides wire-level FIX message encoding/decoding, session management,
and an async TCP+SSL client with heartbeat and reconnection logic.
"""

import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import time
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SOH = b"\x01"
SOH_CHR = "\x01"

# Regex to detect end of a FIX message: SOH + tag 10 = 3-digit checksum + SOH
_MSG_BOUNDARY_RE = re.compile(rb"\x0110=\d{3}\x01")


class FIXWireMessage:
    """Ordered list of (tag_int, value_str) pairs for FIX wire protocol."""

    def __init__(self):
        self._fields: List[Tuple[int, str]] = []

    def set(self, tag: int, value) -> "FIXWireMessage":
        """Set a tag value. Replaces the first occurrence if it exists."""
        value_str = str(value)
        for i, (t, _) in enumerate(self._fields):
            if t == tag:
                self._fields[i] = (tag, value_str)
                return self
        self._fields.append((tag, value_str))
        return self

    def get(self, tag: int, default: str = "") -> str:
        """Get the first value for a tag."""
        for t, v in self._fields:
            if t == tag:
                return v
        return default

    def get_all(self, tag: int) -> List[str]:
        """Get all values for a repeating tag."""
        return [v for t, v in self._fields if t == tag]

    def get_int(self, tag: int, default: int = 0) -> int:
        val = self.get(tag)
        if val:
            try:
                return int(val)
            except Exception as e:
                logger.debug(f"Failed to parse int for tag {tag}: {e}")
        return default

    def get_float(self, tag: int, default: float = 0.0) -> float:
        val = self.get(tag)
        if val:
            try:
                return float(val)
            except Exception as e:
                logger.debug(f"Failed to parse float for tag {tag}: {e}")
        return default

    @property
    def msg_type(self) -> str:
        return self.get(35)

    def encode(self) -> bytes:
        """Encode to FIX wire format: 8=FIXT.1.1|9=LEN|...body...|10=CHECKSUM|"""
        # Build body (everything except 8, 9, 10)
        body_parts = []
        for tag, value in self._fields:
            if tag not in (8, 9, 10):
                body_parts.append(f"{tag}={value}")
        body_str = SOH_CHR.join(body_parts) + SOH_CHR if body_parts else ""

        # BeginString and BodyLength
        begin = f"8=FIXT.1.1{SOH_CHR}"
        body_len = len(body_str.encode("ascii"))
        length_field = f"9={body_len}{SOH_CHR}"

        # Message without checksum
        msg_without_checksum = begin + length_field + body_str

        # Checksum: sum of all bytes mod 256
        checksum = sum(msg_without_checksum.encode("ascii")) % 256
        full_msg = msg_without_checksum + f"10={checksum:03d}{SOH_CHR}"

        return full_msg.encode("ascii")

    @classmethod
    def decode(cls, raw_bytes: bytes) -> "FIXWireMessage":
        """Parse SOH-delimited wire format into a FIXWireMessage."""
        msg = cls()
        raw_str = raw_bytes.decode("ascii", errors="replace")
        pairs = raw_str.split(SOH_CHR)
        for pair in pairs:
            if "=" in pair:
                tag_str, value = pair.split("=", 1)
                try:
                    tag = int(tag_str)
                except ValueError:
                    continue
                msg._fields.append((tag, value))
        return msg

    def __repr__(self) -> str:
        fields_str = " ".join(f"{t}={v}" for t, v in self._fields[:10])
        if len(self._fields) > 10:
            fields_str += " ..."
        return f"FIXWireMessage({fields_str})"


class FIXSession:
    """Sequence number and session state tracking."""

    def __init__(self, sender_comp_id: str, target_comp_id: str = "Coinbase",
                 heartbeat_interval: int = 30):
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.heartbeat_interval = heartbeat_interval
        self._send_seq = 0
        self._recv_seq = 0

    def next_send_seq(self) -> int:
        self._send_seq += 1
        return self._send_seq

    def advance_recv_seq(self):
        self._recv_seq += 1

    @property
    def last_recv_seq(self) -> int:
        return self._recv_seq

    def stamp_message(self, msg: FIXWireMessage) -> FIXWireMessage:
        """Add SenderCompID, TargetCompID, MsgSeqNum, and SendingTime."""
        seq = self.next_send_seq()
        msg.set(49, self.sender_comp_id)   # SenderCompID
        msg.set(56, self.target_comp_id)   # TargetCompID
        msg.set(34, seq)                   # MsgSeqNum
        msg.set(52, _utc_timestamp())      # SendingTime
        return msg

    def reset(self):
        self._send_seq = 0
        self._recv_seq = 0


def _utc_timestamp() -> str:
    """UTC timestamp in FIX format: YYYYMMDD-HH:MM:SS.sss"""
    t = time.time()
    gm = time.gmtime(t)
    ms = int((t % 1) * 1000)
    return time.strftime("%Y%m%d-%H:%M:%S", gm) + f".{ms:03d}"


def build_coinbase_logon_signature(
    sending_time: str,
    msg_type: str,
    seq_num: int,
    sender_comp_id: str,
    target_comp_id: str,
    password: str,
    api_secret_b64: str,
) -> str:
    """Build the HMAC-SHA256 signature for Coinbase FIX Logon.

    Prehash = SOH-joined: SendingTime, MsgType, MsgSeqNum, SenderCompID,
              TargetCompID, Password
    Signature = base64(HMAC-SHA256(base64decode(secret), prehash))
    """
    prehash = SOH_CHR.join([
        sending_time,
        msg_type,
        str(seq_num),
        sender_comp_id,
        target_comp_id,
        password,
    ])
    secret_bytes = base64.b64decode(api_secret_b64)
    sig = hmac.new(secret_bytes, prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(sig).decode("ascii")


class FIXClient:
    """Async TCP+SSL FIX client with heartbeat and reconnection."""

    def __init__(
        self,
        host: str,
        port: int,
        session: FIXSession,
        password: str,
        api_secret_b64: str,
        on_message: Optional[Callable] = None,
        name: str = "FIXClient",
    ):
        self.host = host
        self.port = port
        self.session = session
        self.password = password
        self.api_secret_b64 = api_secret_b64
        self.on_message = on_message
        self.name = name

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._running = False
        self._connected = False
        self._logged_in = False
        self._last_logout_reason = ""
        self._logon_event = asyncio.Event()
        self._read_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_recv_time = 0.0

        # Callback fired once logon is confirmed
        self.on_logon: Optional[Callable] = None

        # Lock for send atomicity (stamp + write must be atomic)
        self._send_lock = asyncio.Lock()

        # Status file for cross-process visibility
        self._status_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
        )

    @property
    def is_connected(self) -> bool:
        """Whether the FIX client has an active connection."""
        return self._connected

    async def connect(self):
        """Open TCP+SSL connection to the FIX endpoint."""
        ssl_ctx = ssl.create_default_context()
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=ssl_ctx,
        )
        self._connected = True
        self._last_recv_time = time.time()
        logger.info(f"[{self.name}] TCP+SSL connected to {self.host}:{self.port}")

    async def logon(self, reset_seq_num: bool = True):
        """Send FIX Logon (MsgType=A) with HMAC-SHA256 authentication."""
        if reset_seq_num:
            self.session.reset()

        self._logon_event.clear()

        msg = FIXWireMessage()
        msg.set(35, "A")  # MsgType = Logon

        # Stamp with session info (sets seq, sender/target, sending time)
        self.session.stamp_message(msg)

        sending_time = msg.get(52)
        seq_num = msg.get_int(34)

        # Coinbase Logon fields
        msg.set(98, 0)                               # EncryptMethod = None
        msg.set(108, self.session.heartbeat_interval) # HeartBtInt
        msg.set(553, self.session.sender_comp_id)    # Username = API key
        msg.set(554, self.password)                   # Password (passphrase)

        signature = build_coinbase_logon_signature(
            sending_time=sending_time,
            msg_type="A",
            seq_num=seq_num,
            sender_comp_id=self.session.sender_comp_id,
            target_comp_id=self.session.target_comp_id,
            password=self.password,
            api_secret_b64=self.api_secret_b64,
        )
        msg.set(95, len(signature))                  # RawDataLength
        msg.set(96, signature)                       # RawData (signature)
        msg.set(141, "Y")                            # ResetSeqNumFlag
        msg.set(1137, "9")                           # DefaultApplVerID = FIX50SP2
        msg.set(8013, "S")                           # CancelOrdersOnDisconnect = Session

        await self._send_raw(msg)
        # Log the wire message for debugging
        wire_debug = msg.encode().replace(b"\x01", b"|")
        logger.info(f"[{self.name}] Logon sent (seq={seq_num})")
        logger.debug(f"[{self.name}] Logon wire: {wire_debug.decode('ascii', errors='replace')}")

        # Wait for Logon response
        try:
            await asyncio.wait_for(self._logon_event.wait(), timeout=10.0)
            logger.info(f"[{self.name}] FIX Logon confirmed")
        except asyncio.TimeoutError:
            logger.error(f"[{self.name}] Logon response timeout")
            raise ConnectionError("FIX Logon timeout")

    async def send(self, msg: FIXWireMessage):
        """Stamp and send a FIX message. Lock ensures atomic stamp+write."""
        async with self._send_lock:
            self.session.stamp_message(msg)
            await self._send_raw(msg)

    async def _send_raw(self, msg: FIXWireMessage):
        """Encode and write a FIX message to the socket."""
        if not self._writer:
            raise ConnectionError("Not connected")
        data = msg.encode()
        self._writer.write(data)
        await self._writer.drain()

    async def _read_loop(self):
        """Read TCP stream, frame messages, dispatch."""
        buf = b""
        while self._running and self._connected:
            try:
                chunk = await self._reader.read(65536)
                if not chunk:
                    logger.warning(f"[{self.name}] Connection closed by remote")
                    self._connected = False
                    break
                buf += chunk
                self._last_recv_time = time.time()
                logger.debug(f"[{self.name}] Received {len(chunk)} bytes: {chunk[:200]}")

                # Extract complete messages from buffer
                while True:
                    match = _MSG_BOUNDARY_RE.search(buf)
                    if not match:
                        break
                    end = match.end()
                    raw_msg = buf[:end]
                    buf = buf[end:]

                    try:
                        wire_msg = FIXWireMessage.decode(raw_msg)
                        self.session.advance_recv_seq()
                        await self._dispatch(wire_msg)
                    except Exception as e:
                        logger.error(f"[{self.name}] Error parsing FIX message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.error(f"[{self.name}] Read error: {e}")
                    self._connected = False
                break

    async def _dispatch(self, msg: FIXWireMessage):
        """Route message to session handler or application callback."""
        msg_type = msg.msg_type

        if msg_type == "A":  # Logon
            logger.info(f"[{self.name}] Received Logon response")
            self._logged_in = True
            self._last_logout_reason = ""
            self._write_status()
            self._logon_event.set()
            if self.on_logon:
                await self.on_logon()

        elif msg_type == "0":  # Heartbeat
            pass  # Expected, no action needed

        elif msg_type == "1":  # TestRequest
            test_req_id = msg.get(112)
            hb = FIXWireMessage()
            hb.set(35, "0")  # Heartbeat
            hb.set(112, test_req_id)  # TestReqID
            await self.send(hb)

        elif msg_type == "5":  # Logout
            text = msg.get(58)
            logger.info(f"[{self.name}] Received Logout: {text}")
            self._logged_in = False
            self._last_logout_reason = text
            self._connected = False
            self._write_status()

        elif msg_type == "4":  # SequenceReset
            new_seq = msg.get_int(36)
            if new_seq > 0:
                self.session._recv_seq = new_seq - 1
            logger.info(f"[{self.name}] SequenceReset to {new_seq}")

        elif msg_type == "3":  # Reject
            text = msg.get(58)
            ref_seq = msg.get(45)
            logger.warning(f"[{self.name}] Session Reject: seq={ref_seq} text={text}")

        else:
            # Application message — forward to callback
            if self.on_message:
                try:
                    await self.on_message(msg)
                except Exception as e:
                    logger.error(f"[{self.name}] on_message error: {e}", exc_info=True)

    async def _heartbeat_loop(self):
        """Send Heartbeat every interval, detect timeout."""
        interval = self.session.heartbeat_interval
        while self._running and self._connected:
            try:
                await asyncio.sleep(interval)
                if not self._running or not self._connected:
                    break

                # Send heartbeat
                hb = FIXWireMessage()
                hb.set(35, "0")
                await self.send(hb)

                # Check for remote timeout (no data in 2x heartbeat interval)
                elapsed = time.time() - self._last_recv_time
                if elapsed > interval * 2.5:
                    logger.warning(
                        f"[{self.name}] No data received for {elapsed:.0f}s, "
                        f"sending TestRequest"
                    )
                    tr = FIXWireMessage()
                    tr.set(35, "1")  # TestRequest
                    tr.set(112, f"TEST-{int(time.time())}")
                    await self.send(tr)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.error(f"[{self.name}] Heartbeat error: {e}")
                break

    async def run(self, auto_reconnect: bool = True):
        """Connect, logon, and run read/heartbeat loops.

        With auto_reconnect, retries with exponential backoff on disconnect.
        """
        self._running = True
        backoff = 2.0
        max_backoff = 60.0

        while self._running:
            try:
                await self.connect()

                # Start read loop BEFORE logon so we can receive the response
                self._read_task = asyncio.create_task(self._read_loop())

                await self.logon(reset_seq_num=True)

                # Start heartbeat loop after successful logon
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                # Wait for either loop to finish (disconnect)
                done, _ = await asyncio.wait(
                    [self._read_task, self._heartbeat_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel the other task
                for task in [self._read_task, self._heartbeat_task]:
                    if task and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                self._read_task = None
                self._heartbeat_task = None
                await self._close_socket()

                backoff = 2.0  # reset on clean disconnect

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    f"[{self.name}] Connection failed: {e}. "
                    f"Reconnecting in {backoff:.0f}s..."
                )

            if not self._running or not auto_reconnect:
                break

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def stop(self):
        """Send Logout and close socket."""
        self._running = False

        # Try to send Logout gracefully
        if self._connected and self._writer:
            try:
                logout = FIXWireMessage()
                logout.set(35, "5")  # Logout
                await self.send(logout)
                logger.info(f"[{self.name}] Logout sent")
            except Exception as e:
                logger.debug(f"[{self.name}] Failed to send Logout: {e}")

        # Cancel tasks
        for task in [self._read_task, self._heartbeat_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._read_task = None
        self._heartbeat_task = None
        await self._close_socket()
        logger.info(f"[{self.name}] Stopped")

    async def _close_socket(self):
        """Close the TCP socket."""
        self._connected = False
        self._logged_in = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.debug(f"[{self.name}] Error closing socket: {e}")
            self._writer = None
        self._reader = None
        self._write_status()

    def _write_status(self):
        """Write connection status to a JSON file for cross-process visibility.

        Uses file locking to prevent TOCTOU races when multiple FIX clients
        write concurrently.
        """
        try:
            os.makedirs(self._status_dir, exist_ok=True)
            status_file = os.path.join(self._status_dir, "fix_status.json")

            # Use exclusive file lock for atomic read-modify-write
            with open(status_file, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.seek(0)
                    content = f.read()
                    existing = json.loads(content) if content.strip() else {}
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(f"[{self.name}] Failed to read status file: {e}")
                    existing = {}

                existing[self.name] = {
                    "connected": self._connected,
                    "logged_in": self._logged_in,
                    "host": self.host,
                    "last_logout_reason": self._last_logout_reason,
                }

                f.seek(0)
                f.truncate()
                json.dump(existing, f)
                # Lock released when file handle closes
        except Exception as e:
            logger.debug(f"[{self.name}] Failed to write status file: {e}")
