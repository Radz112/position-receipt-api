import os
from dotenv import load_dotenv

load_dotenv()

BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
PORT = int(os.getenv("PORT", "8000"))
PAY_TO_ADDRESS = {
    "base": os.getenv("PAY_TO_ADDRESS_BASE", ""),
    "solana": os.getenv("PAY_TO_ADDRESS_SOLANA", ""),
}

VALID_CHAINS = {"base", "solana"}

DEPTH_CONFIG = {
    "fast": {
        "base_days": 30,
        "sol_sigs": 200,
        "max_rpc_calls": 8,
        "max_time_s": 1.0,
    },
    "standard": {
        "base_days": 90,
        "sol_sigs": 500,
        "max_rpc_calls": 12,
        "max_time_s": 2.0,
    },
    "deep": {
        "base_days": 180,
        "sol_sigs": 1000,
        "max_rpc_calls": 16,
        "max_time_s": 3.0,
    },
}

TRANSFER_BUDGET = {
    "base": {
        "max_rpc_calls": 10,
        "max_time_s": 1.0,
        "chunk_size": 25_000,
        "target_inbound": 5,
        "target_outbound": 5,
    },
    "solana": {
        "max_tx_parsed": 20,
        "max_time_s": 1.0,
        "sig_fetch_limit": 30,
        "target_inbound": 5,
        "target_outbound": 5,
        "parallel_batch_size": 5,
    },
}

# Native token identifiers per chain (used to skip log-based scans)
NATIVE_TOKENS = {
    "base": {"eth", "0x0000000000000000000000000000000000000000"},
    "solana": {"sol", "So11111111111111111111111111111111111111112"},
}

# Pre-lowercased address sets — avoids rebuilding on every request
KNOWN_DEX_ROUTERS = {
    "base": {
        "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap Universal Router
        "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router v2
        "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # Kyberswap
        "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
        "0x6352a56caadc4f1e25cd6c75970fa768a3304e64",  # OpenOcean
    },
    "solana": {
        "jup6lkbzbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4".lower(),
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc".lower(),
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8".lower(),
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK".lower(),
    },
}

KNOWN_DISTRIBUTOR_CONTRACTS = {
    "0x777777c338d5487fdecc5b15949cc8e9f69a7899",
    "0x000000000000cd17345801aa8147b8d3950260ff",
}

WRAPPED_TOKENS = {
    "base": {"0x4200000000000000000000000000000000000006"},
    "solana": {"so11111111111111111111111111111111111111112"},
}

LP_SYMBOLS = {"UNI-V2", "SLP", "CAKE-LP", "JLP", "ORCA-LP"}

RATE_LIMITS = {
    "per_ip": {
        "max_requests": int(os.getenv("RATE_LIMIT_PER_IP", "60")),
        "window_s": 60,
    },
    "per_wallet_token": {
        "max_requests": int(os.getenv("RATE_LIMIT_PER_WT", "10")),
        "window_s": 60,
    },
}
