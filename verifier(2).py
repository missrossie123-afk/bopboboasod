from dataclasses import dataclass
from enum import Enum

class VerificationStatus(str, Enum):
    CONFIRMED = "confirmed"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"

@dataclass
class VerificationResult:
    status: VerificationStatus
    message: str

async def verify_gift_transfer(gift_url: str, recipient_username: str | None, deal: dict) -> VerificationResult:
    """
    Fail-closed placeholder. Telegram Bot API does not expose a general endpoint
    for verifying collectible gift ownership in arbitrary user accounts.
    Connect an authorized provider here; confirm only after verifying exact gift ID
    and recipient independently. Never mark a deal complete from a button click.
    """
    return VerificationResult(
        VerificationStatus.UNKNOWN,
        "No authoritative ownership verifier is configured."
    )
