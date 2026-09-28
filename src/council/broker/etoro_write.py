"""WRITE client for the Agent Portfolio WRITE token. Operator terminal only.

Rules:
- Importing this module raises RuntimeError unless COUNCIL_ROLE=operator, or COUNCIL_ROLE=dev with
  COUNCIL_MODE=stub (tests against the fake broker). The runner can never load it.
- Opens are v3 market orders sized by UNITS (units × rate = exposure, unambiguous) and carry a
  fixed stopLossRate on EVERY open: a missing/invalid stop raises BEFORE any request is built.
- A stop-loss PATCH always sets a rate; it never sends clearStopLoss.
- Take-profit (swing book, SW-5): an open may carry `takeProfitRate` next to its stop, but only
  when the caller passes one (the planner does so only behind the `tp_on_open` capability gate).
  A PATCH that sets a take-profit ALWAYS sends the current stopLossRate with it, and a PATCH never
  sends clearStopLoss / clearTakeProfit (the route accepts both; we never build them).
- Outcome classification: 2xx → accepted payload (accepted is not executed); 4xx (including a
  clean 401/403 and 429) → DefiniteRejection; timeout / transport error / 5xx → AmbiguousWriteError.
- This client NEVER retries. Retrying a write is the executor's decision, with a new attempt id,
  and only after a definite rejection.
- No order-cancel method yet, on purpose. When an open is held for a closed market (status 11)
  the executor cancels it only if the write client has `cancel_order(request_id=, order_id=)` AND
  its `CANCEL_ROUTE_VERIFIED` attribute is True; both change here after the M5 route check
  verifies the cancel route and its payload (contract test first). Until then a held order becomes
  `waiting_for_market`, which the watch resolves read-only (see ETORO_ROUTES.md, "Order held for a
  closed market"). `resume` never cancels.
"""

from __future__ import annotations

import math
import os
from typing import Any, Literal

import httpx

from council.broker.http import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    AmbiguousWriteError,
    BrokerHTTP,
    BrokerHTTPError,
    DefiniteRejection,
    MissingStopLoss,
    body_excerpt,
    check_request_id,
    decode_json,
    parse_retry_after,
)

__all__ = [
    "AmbiguousWriteError", "DefiniteRejection", "EtoroWriteClient", "MissingStopLoss",
]


def _import_guard() -> None:
    role = os.environ.get("COUNCIL_ROLE")
    mode = os.environ.get("COUNCIL_MODE")
    if role == "operator" or (role == "dev" and mode == "stub"):
        return
    raise RuntimeError(
        "council.broker.etoro_write may only be imported in the operator terminal "
        "(COUNCIL_ROLE=operator) or in stub-mode development"
    )


_import_guard()

CANCEL_ROUTE_VERIFIED = False     # flips, together with cancel_order, after the M5 route check
OPEN_ORDER_PATH = "/api/v3/trading/execution/orders"
CLOSE_POSITION_PATH = "/api/v1/trading/execution/market-close-orders/positions/{position_id}"
POSITION_PATH = "/api/v2/trading/positions/{position_id}"

Transaction = Literal["buy", "sellShort"]
SETTLEMENTS = frozenset({"real", "cfd", "realFutures", "marginTrade"})
STOP_LOSS_TYPES = frozenset({"fixed"})


def _positive_finite(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def build_open_body(
    *,
    instrument_id: int,
    transaction: str,
    settlement: str,
    leverage: int,
    units: float,
    stop_loss_rate: float | None,
    stop_loss_type: str = "fixed",
    take_profit_rate: float | None = None,
) -> dict[str, Any]:
    """The exact v3 open body. Validates every field; the stop-loss check comes first. A
    `take_profit_rate` (optional) must sit on the profit side of the stop."""
    if not _positive_finite(stop_loss_rate):
        raise MissingStopLoss("every open carries a positive, finite stopLossRate")
    if stop_loss_type not in STOP_LOSS_TYPES:
        raise ValueError("stopLossType must be 'fixed'")
    if transaction not in ("buy", "sellShort"):
        raise ValueError("transaction must be 'buy' or 'sellShort'")
    if settlement not in SETTLEMENTS:
        raise ValueError(f"unknown settlement {settlement!r}")
    if not _positive_int(instrument_id):
        raise ValueError("instrument_id must be a positive int")
    if not _positive_int(leverage):
        raise ValueError("leverage must be a positive int")
    if not _positive_finite(units):
        raise ValueError("units must be positive and finite")
    if settlement == "real" and (leverage != 1 or transaction != "buy"):
        raise ValueError("real settlement is long-only at 1x")
    if take_profit_rate is not None:
        _check_take_profit(take_profit_rate, float(stop_loss_rate),  # type: ignore[arg-type]
                           is_buy=transaction == "buy")
    body = {
        "action": "open",
        "transaction": transaction,
        "instrumentId": instrument_id,
        "settlementType": settlement,
        "orderType": "mkt",
        "leverage": leverage,
        "units": float(units),
        "stopLossRate": float(stop_loss_rate),  # type: ignore[arg-type]
        "stopLossType": stop_loss_type,
    }
    if take_profit_rate is not None:
        body["takeProfitRate"] = float(take_profit_rate)
    return body


def _check_take_profit(take_profit_rate: Any, stop_loss_rate: float, *, is_buy: bool | None) -> None:
    if not _positive_finite(take_profit_rate):
        raise ValueError("takeProfitRate must be positive and finite")
    if is_buy is True and not take_profit_rate > stop_loss_rate:
        raise ValueError("a long's takeProfitRate must be above its stopLossRate")
    if is_buy is False and not take_profit_rate < stop_loss_rate:
        raise ValueError("a short's takeProfitRate must be below its stopLossRate")


def build_close_body(*, instrument_id: int, units_to_deduct: float | None) -> dict[str, Any]:
    """Market-close body. UnitsToDeduct null = full close."""
    if not _positive_int(instrument_id):
        raise ValueError("instrument_id must be a positive int")
    if units_to_deduct is not None and not _positive_finite(units_to_deduct):
        raise ValueError("units_to_deduct must be positive and finite, or None for a full close")
    return {
        "InstrumentId": instrument_id,
        "UnitsToDeduct": None if units_to_deduct is None else float(units_to_deduct),
    }


def build_patch_body(*, stop_loss_rate: float, take_profit_rate: float | None = None) -> dict[str, Any]:
    """Position PATCH body: the stop-loss rate ALWAYS (a take-profit is never sent without the
    current stop), plus `takeProfitRate` when given. Never clears a stop or a take-profit."""
    if not _positive_finite(stop_loss_rate):
        raise MissingStopLoss("a position PATCH must set a positive, finite stop-loss rate")
    body: dict[str, Any] = {"stopLossRate": float(stop_loss_rate), "stopLossType": "fixed"}
    if take_profit_rate is not None:
        _check_take_profit(take_profit_rate, float(stop_loss_rate), is_buy=None)
        body["takeProfitRate"] = float(take_profit_rate)
    return body


class EtoroWriteClient:
    """open / close / patch-SL(+TP). Construct only with the WRITE token in the operator terminal."""

    # The executor cancels a held order only when this is True AND `cancel_order` exists.
    CANCEL_ROUTE_VERIFIED = CANCEL_ROUTE_VERIFIED

    def __init__(
        self,
        api_key: str,
        user_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    ) -> None:
        self._http = BrokerHTTP(
            api_key, user_key, base_url=base_url, transport=transport, timeout=timeout,
            read_backoff=(), max_429_retries=0,
        )

    def __repr__(self) -> str:
        return f"EtoroWriteClient({self._http!r})"

    def open_order(
        self,
        *,
        request_id: str,
        instrument_id: int,
        transaction: Transaction,
        settlement: str,
        leverage: int,
        units: float,
        stop_loss_rate: float | None,
        stop_loss_type: str = "fixed",
        take_profit_rate: float | None = None,
    ) -> dict[str, Any]:
        """POST v3 market open by units. 202 = accepted (not executed): poll orders:lookup."""
        body = build_open_body(
            instrument_id=instrument_id, transaction=transaction, settlement=settlement,
            leverage=leverage, units=units, stop_loss_rate=stop_loss_rate,
            stop_loss_type=stop_loss_type, take_profit_rate=take_profit_rate,
        )
        return self._send("POST", OPEN_ORDER_PATH, body, request_id)

    def close_position(
        self,
        *,
        request_id: str,
        position_id: int,
        instrument_id: int,
        units_to_deduct: float | None = None,
    ) -> dict[str, Any]:
        """Market close (full, or partial by UnitsToDeduct). 200 = submitted, not yet closed."""
        if not _positive_int(position_id):
            raise ValueError("position_id must be a positive int")
        body = build_close_body(instrument_id=instrument_id, units_to_deduct=units_to_deduct)
        return self._send(
            "POST", CLOSE_POSITION_PATH.format(position_id=position_id), body, request_id
        )

    def patch_stop_loss(
        self, *, request_id: str, position_id: int, stop_loss_rate: float,
        take_profit_rate: float | None = None,
    ) -> dict[str, Any]:
        """Set a fixed stop-loss rate (and, when given, the take-profit rate) on an open position.
        409 = position already closed."""
        if not _positive_int(position_id):
            raise ValueError("position_id must be a positive int")
        body = build_patch_body(stop_loss_rate=stop_loss_rate, take_profit_rate=take_profit_rate)
        return self._send("PATCH", POSITION_PATH.format(position_id=position_id), body, request_id)

    def _send(self, method: str, path: str, body: dict[str, Any], request_id: str) -> dict[str, Any]:
        request_id = check_request_id(request_id)
        try:
            response = self._http.send_once(method, path, json_body=body, request_id=request_id)
        except httpx.TransportError as exc:  # includes every timeout
            raise AmbiguousWriteError(
                f"write outcome unknown ({type(exc).__name__})", request_id
            ) from exc
        status = response.status_code
        if 200 <= status < 300:
            try:
                payload = decode_json(response)
            except BrokerHTTPError as exc:
                raise AmbiguousWriteError("accepted write with an unreadable body", request_id, status) from exc
            return payload if isinstance(payload, dict) else {"result": payload}
        if 400 <= status < 500:
            retry_after = (
                parse_retry_after(response.headers.get("retry-after")) if status == 429 else None
            )
            raise DefiniteRejection(status, body_excerpt(response), retry_after)
        raise AmbiguousWriteError(f"write outcome unknown (HTTP {status})", request_id, status)
