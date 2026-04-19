"""
ctf_utils.py — On-chain CTF redemption helpers.

Shared between api_server.py (user-triggered redeem endpoint) and
monitor.py (auto-redeem loop) to avoid a circular import.

Public surface:
    _build_redeem_calldata(collateral, condition_id_hex, index_sets) -> bytes
    _redeem_ctf_via_safe(ctf_address, collateral, condition_id,
                         index_sets, private_key, safe_address) -> str
"""
from __future__ import annotations

from logger import get_bot_logger

log = get_bot_logger(__name__)


def _build_redeem_calldata(collateral: str, condition_id_hex: str, index_sets: list[int]) -> bytes:
    """ABI-encode a ConditionalTokens.redeemPositions() call.

    redeemPositions(address collateralToken, bytes32 parentCollectionId,
                    bytes32 conditionId, uint256[] indexSets)
    """
    from eth_abi import encode
    from eth_utils import keccak

    selector = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    cid_bytes = bytes.fromhex(condition_id_hex.removeprefix("0x").zfill(64))
    parent = b"\x00" * 32
    payload = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [collateral, parent, cid_bytes, index_sets],
    )
    return selector + payload


async def _redeem_ctf_via_safe(
    ctf_address: str,
    collateral: str,
    condition_id: str,
    index_sets: list[int],
    private_key: str,
    safe_address: str,
) -> str:
    """Submit a redeemPositions tx through Polymarket's gasless relayer.

    Uses the Polymarket relayer (https://relayer-v2.polymarket.com) so no POL
    is required in the signing key.  The relayer pays gas on behalf of
    GNOSIS_SAFE (signature_type=2) proxy wallets.

    API reference:
      GET  /nonce?address={signer}&type=SAFE  → {"nonce": "N"}
      POST /submit  → {"transactionID": "...", "state": "STATE_NEW", ...}
    """
    import aiohttp
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address
    from eth_account import Account
    from eth_account.messages import encode_defunct
    import config as _cfg

    # ALWAYS READ OFFICIAL API SPECS:
    # Polymarket relayer: https://docs.polymarket.com/market-makers/inventory
    # Gnosis Safe EIP-712: https://docs.safe.global/advanced/smart-account-signatures
    # CTF redeemPositions: https://github.com/gnosis/conditional-tokens-contracts
    RELAYER_URL = "https://relayer-v2.polymarket.com"
    _relayer_headers = {
        "RELAYER_API_KEY":         _cfg.RELAYER_API_KEY,
        "RELAYER_API_KEY_ADDRESS": _cfg.RELAYER_API_KEY_ADDRESS,
    }

    ctf  = to_checksum_address(ctf_address)
    safe = to_checksum_address(safe_address)
    col  = to_checksum_address(collateral)

    # ── 1. Build redeemPositions calldata ────────────────────────────────────
    redeem_data = _build_redeem_calldata(col, condition_id, index_sets)

    acct = Account.from_key(private_key)
    signer_address = acct.address  # EOA that owns the Safe

    async with aiohttp.ClientSession() as sess:
        # ── 2. Get Safe nonce directly from the on-chain contract ─────────────
        # NOTE: The relayer's /nonce endpoint can go stale when prior
        # submissions revert (it queues ahead of reality).  A stale nonce
        # causes the Safe to compute a different EIP-712 hash → GS026.
        # Reading from the chain is authoritative and always correct.
        _nonce_selector = keccak(text="nonce()")[:4]
        async with sess.post(
            _cfg.POLYGON_RPC_URL,
            json={
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": safe, "data": "0x" + _nonce_selector.hex()}, "latest"],
                "id": 1,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            rpc_resp = await resp.json()
        if "error" in rpc_resp:
            raise RuntimeError(f"RPC eth_call /nonce error: {rpc_resp['error']}")
        safe_nonce = int(rpc_resp["result"], 16)
        safe_nonce_str: str = str(safe_nonce)

        # ── 3. Build EIP-712 SafeTx hash ──────────────────────────────────────
        DOMAIN_TYPEHASH  = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
        SAFE_TX_TYPEHASH = keccak(text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)")
        ZERO_ADDR = "0x0000000000000000000000000000000000000000"

        domain_sep = keccak(encode(
            ["bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, 137, safe],
        ))
        tx_hash_input = encode(
            ["bytes32", "address", "uint256", "bytes32", "uint8",
             "uint256", "uint256", "uint256", "address", "address", "uint256"],
            [SAFE_TX_TYPEHASH, ctf, 0, keccak(redeem_data), 0,
             0, 0, 0, ZERO_ADDR, ZERO_ADDR, safe_nonce],
        )
        safe_tx_hash = keccak(b"\x19\x01" + domain_sep + keccak(tx_hash_input))

        # ── 4. Sign using eth_sign (personal_sign) style ──────────────────────
        # The relayer expects v=31/32 (Gnosis Safe "eth_sign" format):
        #   Gnosis Safe verifies v=31 as: ecrecover(eth_sign(safe_tx_hash), v-4, r, s)
        # eth_account.sign_message prepends the Ethereum signed-message prefix,
        # returning v=27/28.  Adding 4 yields 31/32.
        msg     = encode_defunct(primitive=safe_tx_hash)
        sig_obj = acct.sign_message(msg)
        r, s, v = sig_obj.r, sig_obj.s, sig_obj.v
        gnosis_v = v + 4  # 27→31, 28→32

        # Pack as: r (uint256 = 32 bytes) + s (uint256 = 32 bytes) + v (uint8 = 1 byte)
        packed_sig = (
            "0x"
            + r.to_bytes(32, "big").hex()
            + s.to_bytes(32, "big").hex()
            + bytes([gnosis_v]).hex()
        )

        # ── 5. POST to relayer /submit ─────────────────────────────────────────
        payload = {
            "type": "SAFE",
            "from": signer_address,
            "to": ctf,
            "proxyWallet": safe,
            "data": "0x" + redeem_data.hex(),
            "nonce": safe_nonce_str,
            "signature": packed_sig,
            "signatureParams": {
                "gasPrice":       "0",
                "operation":      "0",   # Call
                "safeTxnGas":     "0",
                "baseGas":        "0",
                "gasToken":       ZERO_ADDR,
                "refundReceiver": ZERO_ADDR,
            },
            "metadata": f"redeem {condition_id[:20]}",
        }

        async with sess.post(
            f"{RELAYER_URL}/submit",
            json=payload,
            headers=_relayer_headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Relayer /submit error (HTTP {resp.status}): {body}")
            # Return the Ethereum tx hash when available; fall back to the
            # relayer's internal transactionID (UUID) for logging.
            return body.get("transactionHash") or body.get("transactionID") or str(body)
