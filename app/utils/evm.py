"""Shared EVM utilities used across first_seen and transfers."""

# ERC20 Transfer(address indexed from, address indexed to, uint256 value)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def pad_address(address: str) -> str:
    """Pad an EVM address to a 32-byte topics value."""
    return "0x" + address.lower().replace("0x", "").zfill(64)


def unpad_address(padded: str) -> str:
    """Extract 0x-prefixed address from a 32-byte padded topic."""
    if not padded:
        return ""
    return "0x" + padded[-40:]
