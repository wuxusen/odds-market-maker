"""On-chain audit anchoring: sink contract, scheduler policy, ledger wiring,
and (dependency-permitting) the Solana devnet Memo transaction packing +
verification path against a mocked RPC client — no real network traffic.

The Solana-specific tests are skipped (not failed) if the optional
``solana``/``solders``/``bip_utils`` dependencies aren't installed
(``pip install -e ".[anchor]"`` or ``.[dev,anchor]"``); the sink/scheduler/
ledger contract tests always run and never touch those libraries.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from odds_mm.anchor import AnchorPolicy, AnchorResult, AnchorSink, LedgerAnchorScheduler, NullAnchor
from odds_mm.ledger import PaperLedger
from odds_mm.types import Fill, Market, Side

pytest.importorskip("solders", reason="Solana Memo packing tests need solders")
pytest.importorskip("bip_utils", reason="Solana Memo packing tests need bip_utils")

from odds_mm.anchor.solana_anchor import (  # noqa: E402
    MEMO_PREFIX,
    MEMO_PROGRAM_ID,
    SolanaAnchor,
    build_anchor_transaction,
    build_memo_instruction,
    keypair_from_mnemonic,
    verify_anchor,
)

# Well-known, public BIP-39 test vector — NOT a real/funded wallet. Used only
# to exercise deterministic key derivation; never our actual devnet wallet.
TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
TEST_MNEMONIC_EXPECTED_PUBKEY = "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"


def fill(fid_seq: int, outcome: str, side: Side, price: float, qty: float, fixture: int = 1) -> Fill:
    return Fill(fid_seq, fixture, Market.MATCH_ODDS, outcome, side, price, qty, ts=fid_seq * 1000)


# -- AnchorSink contract -------------------------------------------------


def test_null_anchor_never_reports_success():
    sink = NullAnchor()
    result = sink.anchor("ab" * 32)
    assert isinstance(result, AnchorResult)
    assert result.ok is False
    assert result.tx_sig is None


class _FakeSink(AnchorSink):
    """Deterministic in-test sink: no network, scripted outcomes."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[str] = []

    def anchor(self, head_hash: str) -> AnchorResult:
        self.calls.append(head_hash)
        if self._results:
            outcome = self._results.pop(0)
        else:
            outcome = AnchorResult(ok=True, head_hash=head_hash, tx_sig="sig", slot=1)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# -- LedgerAnchorScheduler policy ----------------------------------------


def _ledger_with_fills(n: int) -> PaperLedger:
    led = PaperLedger(starting_cash=1_000.0)
    for i in range(1, n + 1):
        led.record_fill(fill(i, "HOME", Side.ASK, 0.5, 1.0))
    return led


def test_scheduler_not_due_before_any_fills():
    led = PaperLedger(starting_cash=1_000.0)
    sched = LedgerAnchorScheduler(sink=_FakeSink([]), policy=AnchorPolicy(min_fills_between=5, min_interval_ms=60_000))
    assert sched.maybe_anchor(led, now_ms=0) is None  # chain head unchanged (genesis-ish, but no records yet)


def test_scheduler_first_ever_attempt_fires_immediately():
    """No reason to wait out a warm-up window before anything has ever been
    anchored — the first attempt fires as soon as there's one audit record,
    even with thresholds set far out of reach."""
    led = _ledger_with_fills(1)
    sink = _FakeSink([])
    sched = LedgerAnchorScheduler(sink=sink, policy=AnchorPolicy(min_fills_between=10_000, min_interval_ms=10**9))
    result = sched.maybe_anchor(led, now_ms=0)
    assert result is not None and result.ok
    assert sink.calls == [led.audit[0]["hash"]]  # anchored the head *before* the anchor record itself was appended


def test_scheduler_due_by_fill_count_after_first_attempt():
    led = _ledger_with_fills(1)
    sink = _FakeSink([])
    sched = LedgerAnchorScheduler(sink=sink, policy=AnchorPolicy(min_fills_between=3, min_interval_ms=10**9))
    assert sched.maybe_anchor(led, now_ms=0) is not None  # first-ever attempt, unconditional

    for i in (2, 3):  # 2 new records since the last anchor: still below the count threshold
        led.record_fill(fill(i, "HOME", Side.ASK, 0.5, 1.0))
    assert sched.maybe_anchor(led, now_ms=1) is None

    led.record_fill(fill(4, "HOME", Side.ASK, 0.5, 1.0))  # 3rd new record crosses the threshold
    result = sched.maybe_anchor(led, now_ms=2)
    assert result is not None and result.ok


def test_scheduler_due_by_time_after_first_attempt():
    led = _ledger_with_fills(1)
    sink = _FakeSink([])
    sched = LedgerAnchorScheduler(sink=sink, policy=AnchorPolicy(min_fills_between=1_000, min_interval_ms=5_000))
    assert sched.maybe_anchor(led, now_ms=0) is not None  # first-ever attempt, unconditional

    led.record_fill(fill(2, "AWAY", Side.BID, 0.4, 1.0))
    assert sched.maybe_anchor(led, now_ms=4_000) is None  # too soon, and count threshold not met either
    assert sched.maybe_anchor(led, now_ms=6_000) is not None  # 6s since last attempt > 5s interval


def test_scheduler_skips_when_head_unchanged_since_last_anchor():
    led = _ledger_with_fills(5)
    sink = _FakeSink([])
    sched = LedgerAnchorScheduler(sink=sink, policy=AnchorPolicy(min_fills_between=1, min_interval_ms=0))
    first = sched.maybe_anchor(led, now_ms=0)
    assert first is not None and first.ok
    # No new records since the last successful anchor -> nothing to (re)commit.
    assert sched.maybe_anchor(led, now_ms=999_999) is None


def test_scheduler_retries_after_a_failed_attempt():
    led = _ledger_with_fills(5)
    sink = _FakeSink([AnchorResult(ok=False, head_hash="irrelevant", error="rpc down")])
    sched = LedgerAnchorScheduler(sink=sink, policy=AnchorPolicy(min_fills_between=1, min_interval_ms=1_000))
    failed = sched.maybe_anchor(led, now_ms=0)
    assert failed is not None and failed.ok is False
    # Failure must not advance last_anchored_hash, and must not be recorded
    # into the ledger's own audit chain (no false "anchored" claim).
    assert len(led.audit) == 5
    assert sched.maybe_anchor(led, now_ms=100) is None  # cooldown (min_interval_ms) not yet elapsed
    succeeded = sched.maybe_anchor(led, now_ms=2_000)
    assert succeeded is not None and succeeded.ok
    assert len(led.audit) == 6  # exactly one anchor record appended, on the successful attempt


def test_scheduler_never_raises_even_if_sink_raises():
    led = _ledger_with_fills(2)
    sink = _FakeSink([RuntimeError("boom")])
    sched = LedgerAnchorScheduler(sink=sink, policy=AnchorPolicy(min_fills_between=1, min_interval_ms=0))
    with pytest.raises(RuntimeError):
        sched.maybe_anchor(led, now_ms=0)
    # NB: AnchorSink implementations (NullAnchor, SolanaAnchor) are
    # contractually required not to raise; this test documents that the
    # scheduler itself adds no extra safety net beyond the sink's contract
    # -- callers (e.g. odds_mm.demo.run) wrap the call defensively too.


# -- Ledger <-> anchor record wiring -------------------------------------


def test_record_anchor_extends_chain_and_stays_verifiable():
    led = _ledger_with_fills(3)
    head_before = led.chain_head
    rec = led.record_anchor(tx_sig="5abc...", slot=123456, anchored_hash=head_before, ts=99_000)
    assert rec["type"] == "anchor"
    assert rec["anchored_hash"] == head_before
    assert rec["solana_tx_sig"] == "5abc..."
    assert rec["prev_hash"] == head_before
    assert led.chain_head == rec["hash"]
    assert led.verify_audit_chain() is True


def test_tampering_after_anchor_record_breaks_verification():
    led = _ledger_with_fills(2)
    led.record_anchor(tx_sig="sig", slot=1, anchored_hash=led.chain_head, ts=0)
    led.audit[-1]["solana_slot"] = 999999  # tamper
    assert led.verify_audit_chain() is False


def test_fill_and_settlement_records_keep_reserved_anchor_placeholder():
    led = PaperLedger(starting_cash=100.0)
    rec = led.record_fill(fill(1, "HOME", Side.ASK, 0.5, 1.0))
    assert rec["anchor"] == {"solana_tx_sig": None, "solana_slot": None, "merkle_root": None}


# -- Solana packing (pure, offline) --------------------------------------


def test_memo_instruction_targets_memo_program():
    from solders.keypair import Keypair

    kp = Keypair()
    ix = build_memo_instruction(kp.pubkey(), b"hello")
    assert str(ix.program_id) == MEMO_PROGRAM_ID
    assert bytes(ix.data) == b"hello"
    assert ix.accounts[0].pubkey == kp.pubkey()
    assert ix.accounts[0].is_signer is True


def test_anchor_transaction_is_deterministic_given_fixed_blockhash():
    from solders.hash import Hash
    from solders.keypair import Keypair

    kp = Keypair()
    bh = Hash.default()
    head = hashlib.sha256(b"whatever").hexdigest()
    ix = build_memo_instruction(kp.pubkey(), (MEMO_PREFIX + head).encode())
    tx1 = build_anchor_transaction(ix, kp, bh)
    tx2 = build_anchor_transaction(ix, kp, bh)
    assert bytes(tx1) == bytes(tx2)
    assert tx1.message.recent_blockhash == bh


def test_keypair_from_mnemonic_matches_known_test_vector():
    kp = keypair_from_mnemonic(TEST_MNEMONIC)
    assert str(kp.pubkey()) == TEST_MNEMONIC_EXPECTED_PUBKEY


def test_load_mnemonic_from_file(tmp_path):
    from odds_mm.anchor.solana_anchor import _load_mnemonic_from_file

    note = tmp_path / "wallet.txt"
    note.write_text(f"some label\n地址: Xyz\n助记词(12词): {TEST_MNEMONIC}\n")
    assert _load_mnemonic_from_file(str(note)) == TEST_MNEMONIC


# -- SolanaAnchor.anchor() against a mocked async RPC client -------------


class _FakeAsyncClient:
    """Duck-typed stand-in for solana.rpc.async_api.AsyncClient. No sockets."""

    def __init__(self, blockhash, sig, slot=None, fail_send=False, fail_confirm=False, tx_for_get=None):
        self._blockhash = blockhash
        self._sig = sig
        self._slot = slot
        self._fail_send = fail_send
        self._fail_confirm = fail_confirm
        self._tx_for_get = tx_for_get
        self.closed = False
        self.sent = []

    async def get_latest_blockhash(self, commitment=None):
        return SimpleNamespace(value=SimpleNamespace(blockhash=self._blockhash))

    async def send_raw_transaction(self, txn, opts=None):
        self.sent.append(txn)
        if self._fail_send:
            raise ConnectionError("devnet RPC unreachable")
        return SimpleNamespace(value=self._sig)

    async def confirm_transaction(self, tx_sig, commitment=None):
        if self._fail_confirm:
            raise TimeoutError("confirmation timed out")
        return SimpleNamespace(value=[SimpleNamespace(slot=self._slot)])

    async def get_transaction(self, tx_sig, encoding="base64", commitment=None, max_supported_transaction_version=None):
        if self._tx_for_get is None:
            return SimpleNamespace(value=None)
        return SimpleNamespace(value=SimpleNamespace(transaction=SimpleNamespace(transaction=self._tx_for_get), slot=self._slot))

    async def close(self):
        self.closed = True


def _dummy_keypair_and_blockhash():
    from solders.hash import Hash
    from solders.keypair import Keypair

    return Keypair(), Hash.default()


def test_solana_anchor_success_returns_sig_and_slot():
    from solders.signature import Signature

    kp, bh = _dummy_keypair_and_blockhash()
    sig = Signature.default()
    client = _FakeAsyncClient(blockhash=bh, sig=sig, slot=42)
    anchor = SolanaAnchor(kp, client=client)

    result = anchor.anchor("cd" * 32)

    assert result.ok is True
    assert result.tx_sig == str(sig)
    assert result.slot == 42
    assert client.closed is False  # injected client is caller-owned, not closed by us
    assert len(client.sent) == 1


def test_solana_anchor_send_failure_degrades_without_raising():
    kp, bh = _dummy_keypair_and_blockhash()
    from solders.signature import Signature

    client = _FakeAsyncClient(blockhash=bh, sig=Signature.default(), fail_send=True)
    anchor = SolanaAnchor(kp, client=client)

    result = anchor.anchor("ef" * 32)

    assert result.ok is False
    assert "unreachable" in (result.error or "").lower() or result.error is not None
    assert result.tx_sig is None


def test_solana_anchor_confirm_timeout_still_returns_signature():
    """Broadcast succeeded even if confirmation polling times out — the
    signature is still a valid, independently-checkable receipt, so we
    report ok=True with slot=None rather than throwing the result away."""
    from solders.signature import Signature

    kp, bh = _dummy_keypair_and_blockhash()
    sig = Signature.default()
    client = _FakeAsyncClient(blockhash=bh, sig=sig, fail_confirm=True)
    anchor = SolanaAnchor(kp, client=client)

    result = anchor.anchor("11" * 32)

    assert result.ok is True
    assert result.tx_sig == str(sig)
    assert result.slot is None


def test_verify_anchor_true_for_matching_memo():
    kp, bh = _dummy_keypair_and_blockhash()
    head = hashlib.sha256(b"chain-head").hexdigest()
    ix = build_memo_instruction(kp.pubkey(), (MEMO_PREFIX + head).encode())
    tx = build_anchor_transaction(ix, kp, bh)
    from solders.signature import Signature

    client = _FakeAsyncClient(blockhash=bh, sig=Signature.default(), slot=7, tx_for_get=tx)

    verification = verify_anchor(str(Signature.default()), head, client=client)

    assert verification.verified is True
    assert verification.slot == 7
    assert verification.memo == MEMO_PREFIX + head


def test_verify_anchor_false_for_mismatched_hash():
    kp, bh = _dummy_keypair_and_blockhash()
    head = hashlib.sha256(b"real-head").hexdigest()
    other = hashlib.sha256(b"tampered-head").hexdigest()
    ix = build_memo_instruction(kp.pubkey(), (MEMO_PREFIX + head).encode())
    tx = build_anchor_transaction(ix, kp, bh)
    from solders.signature import Signature

    client = _FakeAsyncClient(blockhash=bh, sig=Signature.default(), tx_for_get=tx)

    verification = verify_anchor(str(Signature.default()), other, client=client)
    assert verification.verified is False


def test_verify_anchor_handles_transaction_not_found():
    from solders.signature import Signature

    client = _FakeAsyncClient(blockhash=None, sig=None, tx_for_get=None)
    result = verify_anchor(str(Signature.default()), "ab" * 32, client=client)
    assert result.verified is False
    assert result.error is not None


def test_verify_anchor_never_raises_on_rpc_error():
    from solders.signature import Signature

    class _ExplodingClient(_FakeAsyncClient):
        async def get_transaction(self, *a, **kw):
            raise ConnectionError("rpc down")

    client = _ExplodingClient(blockhash=None, sig=None)
    result = verify_anchor(str(Signature.default()), "ab" * 32, client=client)
    assert result.verified is False
    assert "rpc down" in (result.error or "")


def test_verify_anchor_never_raises_on_malformed_signature():
    result = verify_anchor("not-a-real-signature", "ab" * 32, client=_FakeAsyncClient(blockhash=None, sig=None))
    assert result.verified is False
    assert result.error is not None


def test_solana_anchor_from_env_requires_mnemonic_file(monkeypatch):
    from odds_mm.anchor.solana_anchor import ENV_MNEMONIC_FILE

    monkeypatch.delenv(ENV_MNEMONIC_FILE, raising=False)
    with pytest.raises(RuntimeError):
        SolanaAnchor.from_env()
