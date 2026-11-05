"""Every error this SDK raises is an AgentalError."""

from __future__ import annotations


class AgentalError(Exception):
    """Every error this SDK raises is one of these."""


class ApiError(AgentalError):
    """The API refused the request and said why.

    Decide what to do from ``code``, such as ``sandbox_not_running`` or ``name_taken``.
    ``message`` says why in words.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        regions_with_room: list[str] | None = None,
        *,
        fee_micros: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        #: On a create refused with capacity_exceeded: the other regions that have room for its size now, to send it
        #: again to with a new key. Empty when none has, and None on any other error.
        self.regions_with_room = regions_with_room
        #: On a start refused with 429 for its burst (see BURST_REFUSALS): the burst fee it would have cost, in
        #: millionths of a US dollar. None on any other error.
        self.fee_micros = fee_micros
        #: On a start refused with 429 for its burst: seconds until the same start would cost no burst fee. None
        #: where waiting would not help (running_limit_reached, or a start larger than the plan's whole free
        #: burst), and on any other error.
        self.retry_after_seconds = retry_after_seconds


#: The codes a create, fork or start is refused with, with the status 429, for its account's plan.
#: running_limit_reached: the account already runs as many sandboxes as its plan's running_units allows.
#: burst_fee_too_high: the start would cost more in burst fees than max_burst_fee_micros allows.
#: burst_budget_reached: the fee would take this month's burst fees past the account's budget.
BURST_REFUSALS = frozenset({"running_limit_reached", "burst_fee_too_high", "burst_budget_reached"})


class ProxyError(AgentalError):
    """Something in front of the API answered, not the API.

    A 502, a 504, a 52x, or any error with no code in it. The API was not reached or did
    not answer in time, so the request may still be running. Read the sandbox before
    sending anything but a create again.
    """

    def __init__(self, status: int) -> None:
        super().__init__(
            f"HTTP {status} from a proxy in front of the API, not from the API. The request may still be running."
            if status >= 500
            else f"HTTP {status} with no error of the API's in it. Check that base_url is where the API answers."
        )
        self.status = status


class AgentalConnectionError(AgentalError):
    """The API could not be reached at all. Whether the request arrived is not known."""


class AgentalTimeoutError(AgentalError):
    """What was waited for did not happen in the time allowed. It may still happen."""
