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

from typing import Any

import config
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
    """Submit a Gnosis Safe execTransaction to redeem settled CTF tokens.

    Works for a 1-of-1 EOA-owner Polymarket proxy wallet (signature_type=2).
    Returns the tx hash on success.
    """
    import aiohttp
    from eth_abi import encode
    from eth_utils import keccak, to_checksum_address
    from eth_account import Account

    # ALWAYS READ OFFICIAL API SPECS — Polygon JSON-RPC spec applies for all eth_* calls below.
    # Gnosis Safe execTransaction ABI: https://docs.safe.global/advanced/smart-account-signatures
    # CTF redeemPositions ABI: https://github.com/gnosis/conditional-tokens-contracts
    rpc = config.POLYGON_RPC_URL
    ctf = to_checksum_address(ctf_address)
    safe = to_checksum_address(safe_address)
    col = to_checksum_address(collateral)

    # ── 1. Build redeemPositions calldata ────────────────────────────────────
    redeem_data = _build_redeem_calldata(col, condition_id, index_sets)

    # ── 2. Get Safe nonce via eth_call ────────────────────────────────────────
    nonce_selector = keccak(text="nonce()")[:4]
    async with aiohttp.ClientSession() as sess:
        async def rpc_call(method: str, params: list) -> Any:
            body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            async with sess.post(rpc, json=body) as resp:
                data = await resp.json()
                if "error" in data:
                    raise RuntimeError(f"RPC error: {data['error']}")
                return data["result"]

        nonce_hex = await rpc_call("eth_call", [{"to": safe, "data": "0x" + nonce_selector.hex()}, "latest"])
        safe_nonce = int(nonce_hex, 16)

        # ── 3. Build EIP-712 SafeTx hash ──────────────────────────────────────
        # DOMAIN_SEPARATOR_TYPEHASH = keccak("EIP712Domain(uint256 chainId,address verifyingContract)")
        DOMAIN_TYPEHASH = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
        SAFE_TX_TYPEHASH = keccak(text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)")

        domain_sep = keccak(encode(
            ["bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, 137, safe],
        ))
        tx_hash_input = encode(
            ["bytes32", "address", "uint256", "bytes32", "uint8", "uint256", "uint256", "uint256", "address", "address", "uint256"],
            [SAFE_TX_TYPEHASH, ctf, 0, keccak(redeem_data), 0, 0, 0, 0,
             "0x0000000000000000000000000000000000000000",
             "0x0000000000000000000000000000000000000000",
             safe_nonce],
        )
        safe_tx_hash = keccak(b"\x19\x01" + domain_sep + keccak(tx_hash_input))

        # ── 4. Sign SafeTx hash with EOA ──────────────────────────────────────
        acct = Account.from_key(private_key)
        sig_obj = acct.unsafe_sign_hash(safe_tx_hash)
        # Gnosis Safe expects v=27/28; eth_account returns v=0/1 for raw hashes
        v = sig_obj.v + 27 if sig_obj.v < 27 else sig_obj.v
        signature = (
            sig_obj.r.to_bytes(32, "big") + sig_obj.s.to_bytes(32, "big") + bytes([v])
        )

        # ── 5. Build & send execTransaction tx ────────────────────────────────
        exec_selector = keccak(text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)")[:4]
        exec_data = exec_selector + encode(
            ["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "bytes"],
            [ctf, 0, redeem_data, 0, 0, 0, 0,
             "0x0000000000000000000000000000000000000000",
             "0x0000000000000000000000000000000000000000",
             signature],
        )

        # Get nonce for the EOA (for the raw tx, not the Safe nonce)
        eoa_addr = acct.address
        eoa_nonce_hex = await rpc_call("eth_getTransactionCount", [eoa_addr, "latest"])
        eoa_nonce = int(eoa_nonce_hex, 16)

        gas_price_hex = await rpc_call("eth_gasPrice", [])
        gas_price = int(gas_price_hex, 16)
        gas_price = int(gas_price * 1.2)  # 20% priority bump

        gas_est_hex = await rpc_call("eth_estimateGas", [{"from": eoa_addr, "to": safe, "data": "0x" + exec_data.hex()}])
        gas_limit = int(int(gas_est_hex, 16) * 1.3)

        tx = {
            "to": safe,
            "value": 0,
            "data": "0x" + exec_data.hex(),
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": eoa_nonce,
            "chainId": 137,
        }
        signed = acct.sign_transaction(tx)
        tx_hash_hex = await rpc_call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        return tx_hash_hex
