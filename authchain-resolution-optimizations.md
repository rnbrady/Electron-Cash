# Proof of Concept: Authchain Resolution for Electron Cash

Electron Cash currently relies on the [Paytaca BCMR indexer](https://bcmr.paytaca.com) to fetch token metadata. This works well but depends on a trusted third-party server. In the absence of the indexer, Electron Cash has limited on-chain resolution: it can locate the genesis transaction and extract a BCMR publication output from it, but it cannot perform authchain resolution as defined by the [BCMR spec](https://github.com/bitjson/chip-bcmr).

The spec defines that metadata is published via an **authchain** (zeroth-descendant transaction chain, or ZDTC): a chain of transactions where each one spends output 0 of the previous. The **authhead** (the final transaction whose output 0 is unspent) carries the authoritative metadata, not necessarily the genesis. Without authchain resolution, if a token issuer updates their metadata by publishing a new BCMR output in a later authchain transaction, Electron Cash will continue showing the stale genesis metadata.

A proof of concept was implemented using Claude Opus 4.6 to test the feasibility and performance of authchain resolution in Electron Cash. An initial naive implementation was created which was painfully slow, and then a series of heuristic-based optimizations were layered on top of it to improve performance:


| Token | Authchain | Paytaca Indexer | On-chain (Naive) | On-chain (Optimized) |
|-------|-----------|-----------------|---------------------|----------------------|
| Moria USD (`b38a33f7...`) | 2 hops | 1.0s | 5.9s | 2.9s |
| Black Box (`6f047eb5...`) | 1 hop | 0.9s | 2.8s | 2.4s |
| Broach (`892cef80...`) | 1 hop, OP_RETURN | 0.6s | **116.6s** | 2.1s |
| PepeCash (`d44bf782...`) | 10 hops | 0.4s | 35.9s | 9.4s |
| Furu Tokens (`d9ab24ed...`) | 4 hops | 0.3s | 2.7s | 3.0s |

The Paytaca indexer is the fastest option but requires a trusted third-party server. The optimized on-chain implementation brings resolution times to within a few seconds of the indexer for most tokens, while remaining trustless.

## Phase 1: Implementing Authchain Resolution

### The Electrum protocol limitation

The core challenge is that the Electrum protocol provides no way to follow an output forward to its spending transaction. Given a txid and output index, there is no query that returns "which transaction spent this output?"

The protocol offers two relevant queries:

- **`blockchain.scripthash.get_history`** -- returns all transactions that have ever touched a given address (scripthash)
- **`blockchain.transaction.get`** -- returns the raw transaction data for a given txid

### The workaround

To find the child transaction that spends `parent_txid:0`, we:

1. Compute the scripthash for the address at the parent's output 0
2. Fetch the **entire history** for that scripthash
3. Download **every transaction** in that history
4. Inspect each transaction's inputs to find the one that spends `parent_txid:0`

This is repeated iteratively at each hop of the authchain: once we find the child, we check whether its output 0 is spent, and if so, repeat the process to find the grandchild, and so on until we reach the authhead.

### Why this is slow

For addresses with long histories, this is extremely expensive. The authbase address for token category `892cef80...` has **1,262 history entries**. To determine that the genesis is the authhead (its output 0 is unspent), every single one of those transactions had to be fetched and checked -- taking **over 100 seconds**.

Multi-hop authchains multiply this cost: a 10-hop chain means repeating the full history-scan process 10 times.

## Phase 2: Heuristics and Optimizations

With the basic resolution working but impractical for real-world use, we layered a series of heuristics and optimizations. Each is a fast path that, if it fails, falls through to the next. The final fallback is always the original history scan, so correctness is preserved.

### 1. OP_RETURN Detection at Genesis Output 0

If the genesis transaction's output 0 is an OP_RETURN (which is the BCMR publication output itself), then output 0 is provably unspendable. The genesis is therefore the authhead by definition -- no further chain walking is needed.

This is the single biggest win. Many tokens publish their BCMR data at genesis output 0 and never update it, making this a common case.

### 2. UTXO-Based Unspent Check

Before scanning history, query `blockchain.scripthash.listunspent` for the parent's output 0 scripthash. If `parent_txid:0` appears in the UTXO set, it is unspent and there is no child -- the parent is the authhead.

This replaces an exhaustive history scan (downloading every tx to confirm none spend output 0) with a single lightweight query.

### 3. UTXO-Based Child Search with Walk-Back

Using the same `listunspent` results from optimization 2, check whether any UTXO transaction is itself the child, or a descendant that connects back to the parent.

For each UTXO at the parent's output 0 address:
1. Fetch the tx and check if any input spends `parent_txid:0` (direct child).
2. If not, walk backwards through the tx's inputs (up to 3 hops), checking if any ancestor spends `parent_txid:0`.

The walk-back traverses *all* inputs at each level, not just `prevout_n == 0`, because descendants may spend any output of the child -- only the authchain itself follows output 0.

**Why this works:** The UTXO set at an address is typically much smaller than the full history. If the child (or a descendant) has any remaining unspent output at the same address, we find it without a history scan.

A cap of 30 total tx fetches prevents the walk-back from becoming more expensive than the history scan it is trying to avoid.

### 4. `from_height` Filtering in History Queries

Fulcrum's `blockchain.scripthash.get_history` supports an optional `from_height` parameter (undocumented in the base Electrum protocol, but supported by Fulcrum). By passing the parent transaction's block height, we exclude all history entries from earlier blocks.

This is applied at every step: the initial authbase-to-genesis lookup and each hop of the forward walk.

For the `892cef80...` test case, this reduced the history from 1,262 entries to 579.

### 5. Deduplication Across UTXO and History Phases

Transaction IDs checked during the UTXO heuristic phase (optimization 3) are tracked in a set. When falling through to the history scan, these txids are excluded from the candidate list, avoiding redundant downloads.

### 6. Parallel UTXO Walk-Back and History Scan

The UTXO walk-back (optimization 3) and the history scan run in parallel using threads. Whichever finds the child first wins; the other is aborted early. The UTXO walk-back checks for a result from the history thread between each network fetch, and vice versa, so neither wastes time once the answer is known.

### 7. UTXO Heuristic in Forward Walk Entry

Before the forward walk begins, query UTXOs at the genesis output 0 address and check if any UTXO with `tx_pos == 0` is connected back to the genesis via `prevout_n == 0` inputs. If found, this is the authhead -- no forward walk needed.

If the authhead has no BCMR publication, the most recent publication found during the walk-back is used as a fallback. This follows the spec: the authhead's publication takes precedence, but earlier publications are still valid if the authhead has none.

## Test Results

All tests resolve the same set of token categories via on-chain resolution (Paytaca indexer disabled). Times are measured from the "Falling-back to slower blockchain method" log entry to the final authhead determination.

### Category `892cef80...`

Authbase address has 1,262 history entries. Genesis is the authhead (1 hop). Genesis output 0 is OP_RETURN.

| Run | Time | Optimizations Active | History Scanned |
|-----|------|----------------------|-----------------|
| 1 | **105s** | None (baseline) | 1,262 entries (exhaustive) |
| 2 | **1.6s** | OP_RETURN detection | 1,262 fetched, short-circuited |
| 5 | **2.1s** | All sequential | 579 entries, 3 skipped |
| 7 | **2.8s** | All + parallel | Network variance |

**50x speedup** from the baseline.

### Category `d44bf782...`

10-hop authchain. Authchain stays on the same address (18-20 history entries per hop).

| Run | Time | Optimizations Active | Notes |
|-----|------|----------------------|-------|
| 1 | N/A | Baseline | Did not complete in time |
| 2 | **14.6s** | OP_RETURN, UTXO unspent check | 20 candidates per hop |
| 5 | **12.7s** | All sequential | from_height shrinks history per hop: 18, 17, 10, 9, 8, 7, 7, 7, 2, 1 |
| 7 | **8.9s** | All + parallel | UTXO walk-back and history race at each hop |

from_height progressively narrows the history at each hop. By the final hops, there are only 1-2 candidates instead of 20.

### Category `d9ab24ed...`

4-hop authchain. Authbase address has 733 history entries.

| Run | Time | Optimizations Active | Notes |
|-----|------|----------------------|-------|
| 2 | **4.2s** | OP_RETURN, UTXO unspent check | History scan at each hop |
| 5 | **4.2s** | All sequential | UTXO walk-back found child at hops 2-4, bypassing history |
| 7 | **2.8s** | All + parallel | Parallel search cuts time further |

In the sequential run, the UTXO walk-back resolved hops 2 through 4 without any history queries:

```
found child 7c54400e... via UTXO walk-back (depth 3 from c2cb8593...)
found child 46cf4038... via UTXO walk-back (depth 2 from c2cb8593...)
found child c2cb8593... via UTXO heuristic
c2cb8593...:0 is unspent (fast path)
```

### Category `6f047eb5...`

1-hop authchain. Authbase address has 87 history entries. UTXO heuristic doesn't find the child (no matching UTXOs).

| Run | Time | Optimizations Active | Notes |
|-----|------|----------------------|-------|
| 2 | **1.8s** | OP_RETURN, UTXO unspent check | 95 entries, 94 candidates |
| 5 | **5.3s** | All sequential | 87 entries, 66 candidates (25 skipped via dedup) |
| 7 | **2.0s** | All + parallel | History found child while UTXO walk-back still running |

In run 7, the history thread found the child in ~0.7s and the UTXO walk-back was aborted after checking only 4 txs instead of 25.

## Summary

| Optimization | Impact | Cost |
|---|---|---|
| OP_RETURN detection | Eliminates chain walk entirely for common case | Negligible (local check) |
| UTXO unspent check | Confirms authhead without history scan | 1 `listunspent` call |
| UTXO child search + walk-back | Finds child without history scan when descendant has UTXOs at same address | Up to 30 tx fetches (capped) |
| `from_height` filtering | Reduces history results, especially effective on multi-hop chains | 1 height lookup per hop |
| Dedup across phases | Avoids re-downloading txs already checked | Negligible (set lookup) |
| Parallel search | Whichever method finds child first wins; the other aborts | 1 extra thread per hop |

The optimizations are layered: each one is a fast path that, if it fails, falls through to the next. The final fallback is the original history scan, so correctness is preserved. The UTXO-based approaches exploit the fact that the UTXO set at an address is typically orders of magnitude smaller than the full history.
