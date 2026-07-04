"""Solana devnet anchoring: commits the ledger's rolling audit-chain head
hash as a Memo Program transaction, and lets anyone independently verify it
later from just a transaction signature.

Design (see ``docs/DESIGN.md`` section 7 for the full write-up):

* We anchor the **chain head** (one SHA-256), not the full ledger. The head
  already transitively commits to every prior record — that is what a
  hash chain *is* — so anchoring it is equivalent to anchoring the whole
  history at a fraction of the size/cost, mirroring how TxLINE itself
  anchors a Merkle *root* rather than every odds tick.
* Devnet only. This module never touches mainnet and never moves anything
  but devnet lamports (worthless test tokens) to pay its own transaction
  fee. It holds no user funds and no production credentials.
* Every network call is wrapped so failures degrade to an
  :class:`~odds_mm.anchor.base.AnchorResult` with ``ok=False`` rather than
  raising — anchoring is best-effort infrastructure, not part of the
  trading-decision critical path.

The wallet itself is never read from source: callers pass an already
-constructed key (``keypair_from_mnemonic``) or use
:meth:`SolanaAnchor.from_env`, which reads a mnemonic *file path* out of an
environment variable (see ``.env.example``). The mnemonic text itself is
never logged, never embedded in an exception message, and never written
anywhere by this module.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from .base import AnchorResult, AnchorSink

MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
MEMO_PREFIX = "odds-mm-audit-head:v1:"
DEFAULT_DEVNET_RPC = "https://api.devnet.solana.com"
PHANTOM_DERIVATION_PATH = "m/44'/501'/0'/0'"  # Phantom's default account 0

ENV_MNEMONIC_FILE = "ODDS_MM_SOLANA_MNEMONIC_FILE"
ENV_RPC_URL = "ODDS_MM_SOLANA_RPC_URL"


class _RpcClient(Protocol):
    """The subset of ``solana.rpc.async_api.AsyncClient`` we depend on.

    Declared as a structural protocol (rather than importing the concrete
    class at module scope) so tests can inject a lightweight async mock
    without installing/patching the real RPC client, and so this module
    stays importable even in environments where ``solana``/``solders``
    aren't installed (only actually *instantiating* :class:`SolanaAnchor`
    or calling :func:`verify_anchor` against a live client requires them).
    """

    async def get_latest_blockhash(self, commitment: Optional[str] = None) -> Any: ...
    async def send_raw_transaction(self, txn: bytes, opts: Any = None) -> Any: ...
    async def confirm_transaction(self, tx_sig: Any, commitment: Optional[str] = None) -> Any: ...
    async def get_transaction(self, tx_sig: Any, encoding: str = "base64", commitment: Optional[str] = None, max_supported_transaction_version: Optional[int] = None) -> Any: ...
    async def request_airdrop(self, pubkey: Any, lamports: int, commitment: Optional[str] = None) -> Any: ...
    async def close(self) -> None: ...


def _load_mnemonic_from_file(path: str) -> str:
    """Read a 12/24-word mnemonic out of a free-form wallet note file.

    Only the mnemonic line is extracted; the rest of the file (labels,
    derivation path notes, address) is ignored. Never returns/logs the
    full file content on error, only the path, to avoid ever leaking the
    seed phrase into logs or exception traces.
    """
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        candidate = line.split(":", 1)[-1].strip()
        words = candidate.split()
        if len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words):
            return " ".join(words)
    raise ValueError(f"no BIP-39 mnemonic line found in {path!r}")


def keypair_from_mnemonic(mnemonic: str, account: int = 0):
    """Derive a Solana ed25519 keypair from a BIP-39 mnemonic via SLIP-0010,
    matching Phantom's default derivation path ``m/44'/501'/0'/0'``.
    """
    from bip_utils import Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins
    from solders.keypair import Keypair

    seed = Bip39SeedGenerator(mnemonic).Generate()
    node = (
        Bip44.FromSeed(seed, Bip44Coins.SOLANA)
        .Purpose()
        .Coin()
        .Account(account)
        .Change(Bip44Changes.CHAIN_EXT)
    )
    return Keypair.from_seed(node.PrivateKey().Raw().ToBytes())


def _new_async_client(rpc_url: str) -> Any:
    from solana.rpc.async_api import AsyncClient

    return AsyncClient(rpc_url)


def _skip_preflight_opts() -> Any:
    """TxOpts skipping preflight simulation (lazy import so the module stays
    importable without ``solana`` installed). Preflight on the public devnet
    endpoint is unreliable because the simulating node is load-balanced and may
    lag the node that issued the blockhash; the cluster still accepts the tx."""
    from solana.rpc.models import TxOpts

    return TxOpts(skip_preflight=True)


def build_memo_instruction(payer_pubkey: Any, memo_bytes: bytes) -> Any:
    """Pure, offline: one Memo Program instruction carrying ``memo_bytes``."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        Pubkey.from_string(MEMO_PROGRAM_ID),
        memo_bytes,
        [AccountMeta(payer_pubkey, is_signer=True, is_writable=False)],
    )


def build_anchor_transaction(instruction: Any, keypair: Any, recent_blockhash: Any) -> Any:
    """Pure, offline: sign a single-instruction transaction with ``keypair``
    as fee payer and sole signer. Deterministic given a fixed blockhash.
    """
    from solders.transaction import Transaction

    return Transaction.new_signed_with_payer(
        [instruction], keypair.pubkey(), [keypair], recent_blockhash
    )


def _extract_slot(confirm_resp: Any) -> Optional[int]:
    value = getattr(confirm_resp, "value", None)
    if not value:
        return None
    for status in value:
        if status is not None and getattr(status, "slot", None) is not None:
            return status.slot
    return None


class SolanaAnchor(AnchorSink):
    """Anchors ledger head hashes to Solana devnet via a Memo transaction."""

    def __init__(
        self,
        keypair: Any,
        rpc_url: str = DEFAULT_DEVNET_RPC,
        commitment: str = "confirmed",
        timeout_s: float = 20.0,
        client: Optional[_RpcClient] = None,
        memo_prefix: str = MEMO_PREFIX,
    ) -> None:
        self._keypair = keypair
        self._rpc_url = rpc_url
        self._commitment = commitment
        self._timeout_s = timeout_s
        self._injected_client = client
        self._memo_prefix = memo_prefix

    @classmethod
    def from_mnemonic_file(cls, path: str, rpc_url: str = DEFAULT_DEVNET_RPC, **kwargs: Any) -> "SolanaAnchor":
        keypair = keypair_from_mnemonic(_load_mnemonic_from_file(path))
        return cls(keypair, rpc_url=rpc_url, **kwargs)

    @classmethod
    def from_env(cls, **kwargs: Any) -> "SolanaAnchor":
        """Build from ``ODDS_MM_SOLANA_MNEMONIC_FILE`` (+ optional
        ``ODDS_MM_SOLANA_RPC_URL``). See ``.env.example``. Never reads a
        mnemonic *value* from the environment — only a file path — so the
        secret itself never ends up in process env dumps or shell history.
        """
        path = os.environ.get(ENV_MNEMONIC_FILE)
        if not path:
            raise RuntimeError(
                f"{ENV_MNEMONIC_FILE} is not set — point it at a wallet file "
                "(see .env.example); anchoring stays disabled until it is."
            )
        rpc_url = os.environ.get(ENV_RPC_URL, DEFAULT_DEVNET_RPC)
        return cls.from_mnemonic_file(path, rpc_url=rpc_url, **kwargs)

    @property
    def pubkey(self) -> str:
        return str(self._keypair.pubkey())

    # -- AnchorSink --------------------------------------------------------

    def anchor(self, head_hash: str) -> AnchorResult:
        try:
            return asyncio.run(asyncio.wait_for(self._anchor_async(head_hash), timeout=self._timeout_s))
        except Exception as exc:  # noqa: BLE001 - anchoring must never raise into the caller
            return AnchorResult(ok=False, head_hash=head_hash, error=f"{type(exc).__name__}: {exc}")

    async def _anchor_async(self, head_hash: str) -> AnchorResult:
        owns_client = self._injected_client is None
        client = self._injected_client or _new_async_client(self._rpc_url)
        try:
            memo_bytes = (self._memo_prefix + head_hash).encode("utf-8")
            instruction = build_memo_instruction(self._keypair.pubkey(), memo_bytes)
            # Fetch the blockhash at "finalized" rather than the (fresher) session
            # commitment: on the public devnet endpoint, get_latest_blockhash and the
            # send-preflight simulation can land on different load-balanced nodes, and
            # a too-recent blockhash may not yet exist on the simulating node
            # ("Blockhash not found"). A finalized blockhash is guaranteed present
            # cluster-wide. We also skip preflight so a lagging simulation node can't
            # reject a transaction the cluster will happily accept.
            bh_resp = await client.get_latest_blockhash(commitment="finalized")
            blockhash = bh_resp.value.blockhash
            tx = build_anchor_transaction(instruction, self._keypair, blockhash)
            send_resp = await client.send_raw_transaction(bytes(tx), _skip_preflight_opts())
            sig = send_resp.value
            slot: Optional[int] = None
            try:
                confirm_resp = await client.confirm_transaction(sig, commitment=self._commitment)
                slot = _extract_slot(confirm_resp)
            except Exception:
                pass  # broadcast already succeeded; confirmation is a nice-to-have
            return AnchorResult(ok=True, head_hash=head_hash, tx_sig=str(sig), slot=slot)
        finally:
            if owns_client:
                await client.close()


@dataclass(frozen=True)
class AnchorVerification:
    """Result of independently re-checking a claimed anchor transaction."""

    verified: bool
    tx_sig: str
    slot: Optional[int] = None
    memo: Optional[str] = None
    error: Optional[str] = None


def _extract_memo(versioned_tx: Any, memo_prefix: str) -> Optional[str]:
    """Pull the Memo Program instruction's decoded text out of a
    ``VersionedTransaction`` (as returned by ``encoding="base64"``).
    """
    message = versioned_tx.message
    keys = message.account_keys
    for ix in message.instructions:
        program_id = keys[ix.program_id_index]
        if str(program_id) != MEMO_PROGRAM_ID:
            continue
        try:
            text = bytes(ix.data).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.startswith(memo_prefix):
            return text
    return None


def verify_anchor(
    tx_sig: str,
    expected_hash: str,
    rpc_url: str = DEFAULT_DEVNET_RPC,
    client: Optional[_RpcClient] = None,
    memo_prefix: str = MEMO_PREFIX,
    commitment: str = "confirmed",
    timeout_s: float = 20.0,
) -> AnchorVerification:
    """Independently confirm that ``tx_sig`` is a real Solana transaction
    whose memo commits to ``expected_hash`` (a ledger audit-chain head).

    Never raises: RPC/network failures come back as
    ``AnchorVerification(verified=False, error=...)``.
    """
    try:
        return asyncio.run(
            asyncio.wait_for(
                _verify_anchor_async(tx_sig, expected_hash, rpc_url, client, memo_prefix, commitment),
                timeout=timeout_s,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return AnchorVerification(verified=False, tx_sig=tx_sig, error=f"{type(exc).__name__}: {exc}")


async def _verify_anchor_async(
    tx_sig: str,
    expected_hash: str,
    rpc_url: str,
    client: Optional[_RpcClient],
    memo_prefix: str,
    commitment: str,
) -> AnchorVerification:
    owns_client = client is None
    client = client or _new_async_client(rpc_url)
    try:
        from solders.signature import Signature

        sig_obj = Signature.from_string(tx_sig) if isinstance(tx_sig, str) else tx_sig
        resp = await client.get_transaction(
            sig_obj, encoding="base64", commitment=commitment, max_supported_transaction_version=0
        )
        value = getattr(resp, "value", None)
        if value is None:
            return AnchorVerification(verified=False, tx_sig=tx_sig, error="transaction not found")
        versioned_tx = value.transaction.transaction
        slot = getattr(value, "slot", None)
        memo = _extract_memo(versioned_tx, memo_prefix)
        if memo is None:
            return AnchorVerification(verified=False, tx_sig=tx_sig, slot=slot, error="no matching memo instruction")
        expected_memo = memo_prefix + expected_hash
        return AnchorVerification(verified=memo == expected_memo, tx_sig=tx_sig, slot=slot, memo=memo)
    finally:
        if owns_client:
            await client.close()
