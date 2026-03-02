"""
Simplified FIX 4.4 Protocol Implementation for the Crypto Trading System.

FIX messages are represented as dictionaries with standard FIX tag numbers.
Messages are serialized to/from "tag=value|" delimited strings for transport.

Key FIX Tags used:
    8   = BeginString (FIX.4.4)
    35  = MsgType
    11  = ClOrdID (client order id)
    37  = OrderID (exchange/OM assigned order id)
    41  = OrigClOrdID (original order id for cancel/replace)
    54  = Side (1=Buy, 2=Sell)
    55  = Symbol
    38  = OrderQty
    44  = Price
    40  = OrdType (1=Market, 2=Limit)
    150 = ExecType (0=New, 4=Canceled, 5=Replaced, 8=Rejected, F=Trade)
    39  = OrdStatus (0=New, 1=PartiallyFilled, 2=Filled, 4=Canceled, 8=Rejected)
    151 = LeavesQty
    14  = CumQty
    6   = AvgPx
    31  = LastPx
    32  = LastQty
    60  = TransactTime
    100 = ExDestination (exchange)
    58  = Text (free text / reject reason)
    10  = CheckSum
"""

import json
import time
from typing import Optional


# FIX Tag Constants
class Tag:
    BeginString = "8"
    MsgType = "35"
    ClOrdID = "11"
    CumQty = "14"
    ExecID = "17"
    AvgPx = "6"
    LastPx = "31"
    LastQty = "32"
    OrderID = "37"
    OrderQty = "38"
    OrdStatus = "39"
    OrdType = "40"
    OrigClOrdID = "41"
    Price = "44"
    Side = "54"
    Symbol = "55"
    Text = "58"
    TransactTime = "60"
    ExDestination = "100"
    ExecType = "150"
    LeavesQty = "151"
    CheckSum = "10"


# FIX MsgType values
class MsgType:
    NewOrderSingle = "D"
    ExecutionReport = "8"
    OrderCancelRequest = "F"
    OrderCancelReplaceRequest = "G"
    OrderStatusRequest = "H"
    Heartbeat = "0"


# FIX ExecType values
class ExecType:
    New = "0"
    Canceled = "4"
    Replaced = "5"
    Rejected = "8"
    Trade = "F"
    PendingNew = "A"


# FIX OrdStatus values
class OrdStatus:
    New = "0"
    PartiallyFilled = "1"
    Filled = "2"
    Canceled = "4"
    Replaced = "5"
    PendingNew = "A"
    Rejected = "8"


# FIX Side values
class Side:
    Buy = "1"
    Sell = "2"


# FIX OrdType values
class OrdType:
    Market = "1"
    Limit = "2"


class FIXMessage:
    """Represents a FIX protocol message."""

    def __init__(self, msg_type: Optional[str] = None, fields: Optional[dict] = None):
        self.fields: dict[str, str] = {}
        self.fields[Tag.BeginString] = "FIX.4.4"
        if msg_type:
            self.fields[Tag.MsgType] = msg_type
        if fields:
            self.fields.update(fields)
        if Tag.TransactTime not in self.fields:
            self.fields[Tag.TransactTime] = time.strftime("%Y%m%d-%H:%M:%S")

    def set(self, tag: str, value) -> "FIXMessage":
        self.fields[tag] = str(value)
        return self

    def get(self, tag: str, default: str = "") -> str:
        return self.fields.get(tag, default)

    @property
    def msg_type(self) -> str:
        return self.fields.get(Tag.MsgType, "")

    def encode(self) -> str:
        """Encode FIX message to tag=value delimited string."""
        parts = []
        for tag, value in self.fields.items():
            if tag != Tag.CheckSum:
                parts.append(f"{tag}={value}")
        body = "|".join(parts)
        checksum = sum(ord(c) for c in body) % 256
        parts.append(f"{Tag.CheckSum}={checksum:03d}")
        return "|".join(parts)

    @classmethod
    def decode(cls, raw: str) -> "FIXMessage":
        """Decode a tag=value delimited string into a FIXMessage."""
        msg = cls()
        msg.fields.clear()
        pairs = raw.split("|")
        for pair in pairs:
            if "=" in pair:
                tag, value = pair.split("=", 1)
                msg.fields[tag] = value
        return msg

    def to_json(self) -> str:
        """Serialize to JSON for WebSocket transport."""
        return json.dumps({"fix": self.fields})

    @classmethod
    def from_json(cls, data: str) -> "FIXMessage":
        """Deserialize from JSON."""
        parsed = json.loads(data)
        msg = cls()
        msg.fields = parsed.get("fix", {})
        return msg

    def __repr__(self) -> str:
        return f"FIXMessage({self.msg_type}: {self.fields})"


# Convenience factory functions

def new_order_single(
    cl_ord_id: str,
    symbol: str,
    side: str,
    qty: float,
    ord_type: str,
    price: float = 0.0,
    exchange: str = "",
) -> FIXMessage:
    """Create a NewOrderSingle FIX message."""
    msg = FIXMessage(MsgType.NewOrderSingle)
    msg.set(Tag.ClOrdID, cl_ord_id)
    msg.set(Tag.Symbol, symbol)
    msg.set(Tag.Side, side)
    msg.set(Tag.OrderQty, qty)
    msg.set(Tag.OrdType, ord_type)
    if ord_type == OrdType.Limit:
        msg.set(Tag.Price, price)
    if exchange:
        msg.set(Tag.ExDestination, exchange)
    return msg


def execution_report(
    cl_ord_id: str,
    order_id: str,
    exec_type: str,
    ord_status: str,
    symbol: str,
    side: str,
    leaves_qty: float,
    cum_qty: float,
    avg_px: float,
    last_px: float = 0.0,
    last_qty: float = 0.0,
    text: str = "",
    order_qty: float = 0.0,
    price: float = 0.0,
) -> FIXMessage:
    """Create an ExecutionReport FIX message."""
    msg = FIXMessage(MsgType.ExecutionReport)
    msg.set(Tag.ClOrdID, cl_ord_id)
    msg.set(Tag.OrderID, order_id)
    msg.set(Tag.ExecType, exec_type)
    msg.set(Tag.OrdStatus, ord_status)
    msg.set(Tag.Symbol, symbol)
    msg.set(Tag.Side, side)
    msg.set(Tag.LeavesQty, leaves_qty)
    msg.set(Tag.CumQty, cum_qty)
    msg.set(Tag.AvgPx, avg_px)
    if last_px:
        msg.set(Tag.LastPx, last_px)
    if last_qty:
        msg.set(Tag.LastQty, last_qty)
    if text:
        msg.set(Tag.Text, text)
    if order_qty:
        msg.set(Tag.OrderQty, order_qty)
    if price:
        msg.set(Tag.Price, price)
    return msg


def cancel_request(
    cl_ord_id: str,
    orig_cl_ord_id: str,
    symbol: str,
    side: str,
) -> FIXMessage:
    """Create an OrderCancelRequest FIX message."""
    msg = FIXMessage(MsgType.OrderCancelRequest)
    msg.set(Tag.ClOrdID, cl_ord_id)
    msg.set(Tag.OrigClOrdID, orig_cl_ord_id)
    msg.set(Tag.Symbol, symbol)
    msg.set(Tag.Side, side)
    return msg


def cancel_replace_request(
    cl_ord_id: str,
    orig_cl_ord_id: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
) -> FIXMessage:
    """Create an OrderCancelReplaceRequest FIX message."""
    msg = FIXMessage(MsgType.OrderCancelReplaceRequest)
    msg.set(Tag.ClOrdID, cl_ord_id)
    msg.set(Tag.OrigClOrdID, orig_cl_ord_id)
    msg.set(Tag.Symbol, symbol)
    msg.set(Tag.Side, side)
    msg.set(Tag.OrderQty, qty)
    msg.set(Tag.Price, price)
    return msg
