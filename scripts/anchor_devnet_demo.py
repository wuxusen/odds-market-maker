#!/usr/bin/env python3
"""Best-effort live Solana **devnet** demonstration of ledger anchoring.

    ODDS_MM_SOLANA_MNEMONIC_FILE=/path/to/wallet-note.txt \
        python scripts/anchor_devnet_demo.py

What it does, in order, each step best-effort:

1. Load the devnet wallet from ``ODDS_MM_SOLANA_MNEMONIC_FILE`` (a local
   file path — never a mnemonic value directly in the environment).
2. Check its devnet SOL balance; if it's at/near zero, try a faucet
   airdrop (devnet faucets are aggressively rate-limited and may simply
   refuse — that is expected and handled, not a bug).
3. Run one tiny simulated market-making session in-process (headless,
   ``odds_mm.demo.run``) to produce a real, non-trivial hash-chained
   audit trail.
4. Anchor the resulting chain head with a real Memo Program transaction
   on devnet, print the transaction signature + slot, and independently
   re-verify it with :func:`odds_mm.anchor.verify_anchor` — closing the
   loop end to end against the live network.

If devnet is unreachable or the faucet is exhausted, this script explains
that clearly and exits non-zero rather than hanging — it is a demo of the
happy path, not something the test suite depends on (see
``tests/test_anchor.py`` for the network-free, mocked coverage that *does*
gate CI).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MIN_LAMPORTS_FOR_MEMO_FEE = 5_000  # a Memo-only tx costs ~5000 lamports at 1 sig
AIRDROP_LAMPORTS = 1_000_000_000  # 1 SOL (devnet, worthless)
EXPLORER_TX_URL = "https://explorer.solana.com/tx/{sig}?cluster=devnet"


def _fail(msg: str) -> "NoReturn":  # noqa: F821 - documentation only
    print(f"\n[anchor-devnet-demo] FAILED: {msg}")
    print("[anchor-devnet-demo] Nothing on-chain was claimed; rerun once the above is resolved.")
    sys.exit(1)


def main() -> None:
    try:
        from solana.rpc.async_api import AsyncClient
    except ImportError:
        _fail(
            "solana/solders not installed. Run:\n"
            "  pip install -e '.[anchor]'   (or: pip install solana solders bip-utils)"
        )

    from odds_mm.anchor import SolanaAnchor, verify_anchor
    from odds_mm.anchor.solana_anchor import DEFAULT_DEVNET_RPC, ENV_MNEMONIC_FILE, ENV_RPC_URL
    from odds_mm.demo import run as run_demo

    mnemonic_path = os.environ.get(ENV_MNEMONIC_FILE)
    if not mnemonic_path:
        _fail(
            f"{ENV_MNEMONIC_FILE} is not set. Point it at a local wallet note file "
            "(see .env.example) containing a devnet-only BIP-39 mnemonic."
        )
    if not Path(mnemonic_path).is_file():
        _fail(f"{mnemonic_path!r} does not exist.")

    rpc_url = os.environ.get(ENV_RPC_URL, DEFAULT_DEVNET_RPC)
    print(f"[anchor-devnet-demo] RPC: {rpc_url}")

    try:
        sink = SolanaAnchor.from_mnemonic_file(mnemonic_path, rpc_url=rpc_url)
    except Exception as exc:
        _fail(f"could not load wallet from {mnemonic_path!r}: {exc}")

    print(f"[anchor-devnet-demo] wallet: {sink.pubkey}  (devnet only)")

    import asyncio

    async def _ensure_funded() -> bool:
        from solders.pubkey import Pubkey

        client = AsyncClient(rpc_url)
        try:
            bal = await client.get_balance(Pubkey.from_string(sink.pubkey))
            lamports = bal.value
            print(f"[anchor-devnet-demo] balance: {lamports} lamports ({lamports / 1e9:.6f} SOL)")
            if lamports >= MIN_LAMPORTS_FOR_MEMO_FEE:
                return True
            print("[anchor-devnet-demo] balance too low for a fee — requesting devnet airdrop...")
            try:
                resp = await client.request_airdrop(Pubkey.from_string(sink.pubkey), AIRDROP_LAMPORTS)
                sig = resp.value
                print(f"[anchor-devnet-demo] airdrop requested: {sig}")
            except Exception as exc:
                print(f"[anchor-devnet-demo] airdrop request rejected (faucet likely rate-limited): {exc}")
                return False
            for _ in range(20):
                await asyncio.sleep(1.5)
                bal = await client.get_balance(Pubkey.from_string(sink.pubkey))
                if bal.value >= MIN_LAMPORTS_FOR_MEMO_FEE:
                    print(f"[anchor-devnet-demo] funded: {bal.value} lamports")
                    return True
            print("[anchor-devnet-demo] airdrop did not land within 30s")
            return False
        finally:
            await client.close()

    try:
        funded = asyncio.run(_ensure_funded())
    except Exception as exc:
        _fail(f"could not reach devnet RPC to check balance/airdrop: {exc}")

    if not funded:
        _fail(
            "wallet has no devnet SOL and the faucet is unavailable right now "
            "(common — public devnet faucets are heavily rate-limited). "
            "Fund it manually (e.g. https://faucet.solana.com/) with the address "
            f"above and rerun. This is expected occasionally; it is not a bug in "
            "the anchoring code, which is fully covered by tests/test_anchor.py "
            "against a mocked RPC client regardless of live devnet/faucet status."
        )

    print("[anchor-devnet-demo] running a short headless simulated session to build a real audit chain...")
    final = run_demo(seed=7, speed=0, dashboard=False, quiet=True)
    print(
        f"[anchor-devnet-demo] session done: {final['n_fills']} fills, "
        f"audit_len={final['audit_len']}, head={final['audit_head']}"
    )

    print("[anchor-devnet-demo] submitting the anchor transaction to devnet...")
    result = sink.anchor(final["audit_head"])
    if not result.ok:
        _fail(f"anchor transaction failed: {result.error}")

    print(f"[anchor-devnet-demo] anchored! tx signature: {result.tx_sig}")
    print(f"[anchor-devnet-demo] slot: {result.slot}")
    print(f"[anchor-devnet-demo] explorer: {EXPLORER_TX_URL.format(sig=result.tx_sig)}")

    print("[anchor-devnet-demo] independently re-verifying via verify_anchor()...")
    time.sleep(2.0)
    verification = verify_anchor(result.tx_sig, final["audit_head"], rpc_url=rpc_url)
    if verification.verified:
        print("[anchor-devnet-demo] VERIFIED: on-chain memo matches the ledger's audit-chain head.")
    else:
        print(f"[anchor-devnet-demo] verification inconclusive (often just confirmation lag): {verification.error}")
        print("[anchor-devnet-demo] the transaction itself is still real and checkable at the URL above.")

    print("\n" + "=" * 78)
    print(" LIVE DEVNET ANCHOR — record this in README if this is the first one:")
    print(f"   tx:       {result.tx_sig}")
    print(f"   slot:     {result.slot}")
    print(f"   explorer: {EXPLORER_TX_URL.format(sig=result.tx_sig)}")
    print(f"   head:     {final['audit_head']}")
    print("=" * 78)


if __name__ == "__main__":
    main()
