#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# -*- mode: python3 -*-
# This file (c) 2023-2025 Calin Culianu <calin.culianu@gmail.com>
# Part of the Electron Cash SPV Wallet
# License: MIT
""" Encapsulation and handling of token metadata """

import hashlib
import json
import os
import queue
import requests
import threading

from abc import ABCMeta, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union

from electroncash import address, networks, token, util
from electroncash.simple_config import SimpleConfig
from electroncash.transaction import Transaction


class TokenMeta(util.PrintError, metaclass=ABCMeta):

    def __init__(self, config: SimpleConfig):
        util.PrintError.__init__(self)
        self.config = config
        self.lock = threading.RLock()
        self.path = os.path.join(config.electrum_path(), "cashtoken_meta")
        self.make_dir(self.path)
        self.icons_path = os.path.join(self.path, "icons")
        self.make_dir(self.icons_path)
        self._icon_cache: Dict[str, Any] = dict()
        self.d: Dict[str, Any] = dict()
        self.dirty = False  # True if we wrote some keys to self.d, but they are not yet saved to disk
        self.load()

    def load(self):
        with self.lock:
            metafile = os.path.join(self.path, "metadata.json")
            if os.path.exists(metafile):
                try:
                    with open(metafile, "rt", encoding='utf-8') as f:
                        jdata = f.read()
                        self.d = json.loads(jdata)
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
                    self.print_error(f"Error loading {metafile}: {e!r}")

    def save(self, force=False):
        if not force and not self.dirty:
            return
        with self.lock:
            metafile = os.path.join(self.path, "metadata.json")
            metafile_tmp = metafile + ".tmp"
            try:
                jdata = json.dumps(self.d)
                with open(metafile_tmp, "wt", encoding='utf-8') as f:
                    f.write(jdata)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(metafile_tmp, metafile)
            except (TypeError, ValueError, json.JSONDecodeError, OSError) as e:
                self.print_error(f"Unable to save data to {metafile}: {e!r}")
            self.dirty = False

    @staticmethod
    def make_dir(path):
        util.make_dir(path)
        assert os.path.exists(path) and os.path.isdir(path)

    @staticmethod
    def _mk_icon_key(token_id_hex, nft_hex):
        return token_id_hex if not nft_hex else f"{token_id_hex}_{nft_hex}"

    def get_icon(self, token_id_hex: str, *, nft_hex: Optional[str] = None, autogen_if_missing=True) -> Optional[Any]:
        """Gets the token-specific (or NFT-specific) icon object. On Qt for example this will return a QIcon.
        If no real icon is known for this token and/or NFT, returns the default "generated" icon, unless
        autogen_if_missing=False in which case it returns None."""
        key = self._mk_icon_key(token_id_hex, nft_hex)
        icon = self._icon_cache.get(key)
        if icon:
            return icon
        buf = self._read_icon_file(self._icon_filepath(key))
        if buf:
            icon = self._bytes_to_icon(buf)
        if not icon:
            if not autogen_if_missing:
                # Special case: Return None to indicate there is no icon known
                return None
            icon = self.gen_default_icon(token_id_hex)
        assert icon is not None
        self._icon_cache[key] = icon
        return icon

    def _icon_filepath(self, token_id_hex: str, *, nft_hex: Optional[str] = None) -> str:
        key = self._mk_icon_key(token_id_hex, nft_hex)
        return os.path.join(self.icons_path, key) + "." + self._icon_ext

    def set_icon(self, token_id_hex: str, icon: Any, *, nft_hex: Optional[str] = None):
        fname = self._icon_filepath(token_id_hex, nft_hex=nft_hex)
        buf = (icon is not None and self._icon_to_bytes(icon)) or None
        self._write_icon_file(fname, buf)
        if icon is not None:
            self._icon_cache[self._mk_icon_key(token_id_hex, nft_hex)] = icon

    @property
    def _icon_ext(self) -> str:
        """Reimplement in subclasses to define the icon file extension. Default is "png" """
        return "png"

    def _read_icon_file(self, filepath: str) -> Optional[bytes]:
        with self.lock:
            if not os.path.exists(filepath):
                return None
            with open(filepath, "rb") as f:
                return f.read(1_000_000)  # Read up to 1MB

    @abstractmethod
    def _icon_to_bytes(self, icon: Any) -> bytes:
        """Reimplement in subclasses to take whatever icon format the platform expects and spit out bytes"""
        pass

    @abstractmethod
    def _bytes_to_icon(self, buf: bytes) -> Any:
        """Reimplement in subclasses to take bytes and spit out whatever icon format the platform expects"""
        pass

    @abstractmethod
    def gen_default_icon(self, token_id_hex: str) -> Any:
        """Reimplement in subclasses to generate a default icon for a token_id if the icon file is missing"""
        pass

    def convert_downloaded_icon(self, icon_data: bytes, icon_ext: str) -> Any:
        """Reimplement in subclasses to convert the downloaded icon byte buffer into a platform-specific format"""
        return self._bytes_to_icon(icon_data)

    def _write_icon_file(self, filepath: str, buf: Optional[bytes]):
        with self.lock:
            try:
                os.remove(filepath)
            except OSError:
                pass
            if buf is None:
                return
            with open(filepath, "wb") as f:
                f.write(buf)

    def _get_nft_meta(self, token_id_hex: str, nft_hex: str, create_if_missing=False) -> dict:
        empty = {}
        if nft_hex and isinstance(nft_hex, str):
            ret = self.d.get("nfts", empty).get(token_id_hex, empty).get(nft_hex, empty)
            if ret is empty and create_if_missing:
                dict_by_token_id = self.d.get("nfts")
                if dict_by_token_id is None:
                    self.d["nfts"] = dict_by_token_id = {}
                dict_by_nft_id = dict_by_token_id.get(token_id_hex)
                if dict_by_nft_id is None:
                    dict_by_nft_id = dict_by_token_id[token_id_hex] = {}
                ret = dict_by_nft_id[nft_hex] = {}
            if isinstance(ret, dict):
                return ret
        return empty

    def get_token_display_name(self, token_id_hex: str) -> Optional[str]:
        """Returns None if not found or if empty, otherwise returns the display name if found and not empty."""
        ret = self.d.get("display_names", {}).get(token_id_hex)
        if isinstance(ret, str):
            return ret

    def get_nft_display_name(self, token_id_hex: str, nft_hex: str):
        ret = self._get_nft_meta(token_id_hex, nft_hex).get("display_name")
        if ret and isinstance(ret, str):
            return ret

    def get_token_ticker_symbol(self, token_id_hex: str) -> Optional[str]:
        ret = self.d.get("tickers", {}).get(token_id_hex)
        if isinstance(ret, str):
            return ret

    def get_token_decimals(self, token_id_hex: str) -> Optional[int]:
        """Returns None if unknown or undefined decimals for token"""
        ret = self.d.get("decimals", {}).get(token_id_hex)
        if isinstance(ret, int):
            return ret

    def has_any_metadata_for(self, token_id_hex: str, nft_hex: Optional[str] = None) -> bool:
        if nft_hex:
            return (self.get_nft_display_name(token_id_hex, nft_hex) is not None
                    or self.get_icon(token_id_hex, nft_hex=nft_hex, autogen_if_missing=False) is not None)
        return (self.get_token_display_name(token_id_hex) is not None
                or self.get_token_ticker_symbol(token_id_hex) is not None
                or self.get_token_decimals(token_id_hex) is not None
                or self.get_icon(token_id_hex, autogen_if_missing=False) is not None)

    def set_token_display_name(self, token_id_hex: str, name: Optional[str]):
        dd = self.d.get("display_names", {})
        if name is None:
            dd.pop(token_id_hex, None)
        elif isinstance(name, str):
            was_empty = not dd
            dd[token_id_hex] = str(name)
            if was_empty:
                self.d["display_names"] = dd
        self.dirty = True

    def set_nft_display_name(self, token_id_hex: str, nft_hex: str, name: Optional[str]):
        dd = self._get_nft_meta(token_id_hex, nft_hex, create_if_missing=True)
        if name is None:
            dd.pop("display_name", None)
        elif isinstance(name, str):
            dd["display_name"] = str(name)
        self.dirty = True

    def set_token_ticker_symbol(self, token_id_hex: str, ticker: Optional[str]):
        dd = self.d.get("tickers", {})
        if ticker is None:
            dd.pop(token_id_hex, None)
        elif isinstance(ticker, str):
            was_empty = not dd
            dd[token_id_hex] = str(ticker)
            if was_empty:
                self.d["tickers"] = dd
        self.dirty = True

    def set_token_decimals(self, token_id_hex: str, decimals: Optional[int]):
        dd = self.d.get("decimals", {})
        if decimals is None:
            dd.pop(token_id_hex, None)
        elif isinstance(decimals, int):
            was_empty = not dd
            dd[token_id_hex] = int(decimals)
            if was_empty:
                self.d["decimals"] = dd
        self.dirty = True

    @staticmethod
    def _normalize_to_token_id_hex(token_or_id: Union[str, token.OutputData, bytes, bytearray]) -> str:
        assert isinstance(token_or_id, (str, bytes, bytearray, token.OutputData))
        if isinstance(token_or_id, str):
            return token_or_id
        elif isinstance(token_or_id, (bytes, bytearray)):
            return token_or_id[::-1].hex()  # reverse
        else:
            return token_or_id.id_hex

    @staticmethod
    def _normalize_to_nft_hex(nft: Union[str, token.OutputData, bytes, bytearray]) -> str:
        assert isinstance(nft, (str, token.OutputData, bytes, bytearray))
        if isinstance(nft, str):
            return nft
        elif isinstance(nft, (bytes, bytearray)):
            return nft.hex()  # don't reverse
        else:
            return nft.commitment.hex()  # don't reverse

    def format_amount(self, token_or_id: Union[str, token.OutputData, bytes, bytearray], fungible_amount: int,
                      num_zeros=0, is_diff=False, whitespace=False, precision=None,
                      append_tokentoshis=False, decimals=None) -> str:
        """Formats a particular token's amount string, according to that token's metadata spec for decimals.
        If the token is unknown we tread the 'decimals' for that token as '0'."""
        if decimals is None:
            token_id_hex = self._normalize_to_token_id_hex(token_or_id)
            decimals = self.get_token_decimals(token_id_hex)
        if not isinstance(decimals, int):
            decimals = 0
        return token.format_fungible_amount(fungible_amount, decimal_point=decimals, num_zeros=num_zeros,
                                            precision=precision, is_diff=is_diff, whitespaces=whitespace,
                                            append_tokentoshis=append_tokentoshis)

    def parse_amount(self, token_or_id: Union[str, token.OutputData, bytes, bytearray], val: str) -> int:
        """Inverse of above"""
        token_id_hex = self._normalize_to_token_id_hex(token_or_id)
        decimals = self.get_token_decimals(token_id_hex)
        if not isinstance(decimals, int):
            decimals = 0
        return token.parse_fungible_amount(val, decimal_point=decimals)

    def format_token_display_name(self, token_or_id: Union[str, token.OutputData, bytes, bytearray],
                                  format_str="{token_name} ({token_symbol})",
                                  *, nft: Optional[Union[str, token.OutputData, bytes, bytearray]] = None) -> str:
        token_id_hex = self._normalize_to_token_id_hex(token_or_id)
        nft_hex: Optional[str] = self._normalize_to_nft_hex(nft) if nft else None
        tn = None
        if nft_hex:
            tn = self.get_nft_display_name(token_id_hex, nft_hex)
        if not tn:
            tn = self.get_token_display_name(token_id_hex)
        if tn:
            tn = tn.strip()
        tn = tn or token_id_hex
        tsym = self.get_token_ticker_symbol(token_id_hex)
        if tsym:
            tsym = tsym.strip()
        if not tsym:
            return tn
        return format_str.format(token_name=tn, token_symbol=tsym)


def _get_tx_height(wallet, tx_hash, timeout=30) -> int:
    """Get the confirmed block height for a tx. Tries wallet cache first,
    then falls back to network lookup. Returns 0 if unknown."""
    height, _, _ = wallet.get_tx_height(tx_hash)
    if height > 0:
        return height
    if not wallet.network:
        return 0
    try:
        height = wallet.network.synchronous_get(
            ("blockchain.transaction.get_height", [tx_hash]), timeout=timeout)
        if isinstance(height, int) and height > 0:
            return height
    except Exception:
        pass
    return 0


def try_to_find_genesis_tx(wallet, token_id_hex, timeout=30) -> Optional[Transaction]:
    """Find the genesis tx for a token. The genesis tx is the one that spends
    output 0 of the authbase (token_id_hex)."""
    assert isinstance(token_id_hex, str) and len(token_id_hex) == 64
    try:
        authbase_tx = wallet.try_to_get_tx(token_id_hex, allow_network_lookup=True, timeout=timeout)
    except util.TimeoutException as e:
        util.print_error(f"Failed to get authbase tx {token_id_hex}: {e!r}")
        return None
    if not authbase_tx:
        util.print_error(f"Authbase tx {token_id_hex} not found")
        return None
    from_height = _get_tx_height(wallet, token_id_hex, timeout)
    genesis_tx = _find_child_spending_output0(wallet, authbase_tx, token_id_hex, timeout,
                                              from_height=from_height)
    if not genesis_tx:
        util.print_error(f"Genesis tx not found for authbase {token_id_hex}")
    return genesis_tx


def _scripthash_for_output0(tx) -> Optional[str]:
    """Compute the scripthash for output 0 of a tx directly from its raw scriptPubKey,
    bypassing Address objects to avoid any address-format complications."""
    from electroncash.transaction import deserialize as tx_deserialize
    if not tx.raw:
        return None
    try:
        d = tx_deserialize(tx.raw)
    except Exception:
        return None
    outputs = d.get('outputs', [])
    if not outputs:
        return None
    spk_hex = outputs[0].get('scriptPubKey')
    if not spk_hex:
        return None
    spk_bytes = bytes.fromhex(spk_hex)
    return hashlib.sha256(spk_bytes).digest()[::-1].hex()


def _find_child_spending_output0(wallet, parent_tx, parent_txid, timeout=30,
                                 from_height=0) -> Optional[Transaction]:
    """Given a transaction, find the tx that spends its output 0. Returns None if output 0 is unspent.
    Runs a UTXO walk-back and history scan in parallel — first to find the child wins.
    from_height is passed to get_history to limit results to txs at or above that block height."""
    if not wallet.network:
        return None
    scripthash = _scripthash_for_output0(parent_tx)
    if not scripthash:
        return None
    # Fast path: check UTXOs at the same address as parent:0
    try:
        utxos = wallet.network.synchronous_get(
            ("blockchain.scripthash.listunspent", [scripthash]), timeout=timeout)
        util.print_error(f"_find_child: {parent_txid} listunspent returned {len(utxos)} UTXOs")
    except Exception as e:
        util.print_error(f"_find_child: listunspent failed for {scripthash}: {e!r}, trying history")
        return _find_child_via_history(wallet, scripthash, parent_txid, timeout, from_height)
    # If parent:0 is itself unspent, there is no child
    for u in utxos:
        if u['tx_hash'] == parent_txid and u['tx_pos'] == 0:
            util.print_error(f"_find_child: {parent_txid}:0 is unspent (fast path)")
            return None
    # Parent:0 is spent — run UTXO walk-back and history scan in parallel.
    # History scan runs in a daemon thread; UTXO walk-back runs in current thread.
    # First to find the child wins.
    result_q = queue.Queue()

    def history_worker():
        try:
            result = _find_child_via_history(wallet, scripthash, parent_txid, timeout,
                                             from_height, stop_check=result_q)
            result_q.put(result)
        except Exception:
            result_q.put(None)

    hist_thread = threading.Thread(target=history_worker, daemon=True)
    hist_thread.start()
    # UTXO walk-back in current thread, checking result_q between fetches
    # so we bail out early if the history thread finds the child first.
    checked = {parent_txid}
    max_walk_depth = 3
    max_utxo_checks = 30
    utxo_found = None
    for u in utxos:
        if len(checked) >= max_utxo_checks:
            break
        if not result_q.empty():
            util.print_error("_find_child: history thread found result, aborting UTXO walk-back")
            break
        tx_hash = u['tx_hash']
        if tx_hash in checked:
            continue
        checked.add(tx_hash)
        try:
            tx2 = wallet.try_to_get_tx(tx_hash, allow_network_lookup=True, timeout=timeout)
        except util.TimeoutException:
            continue
        if not tx2:
            continue
        # Direct child check
        for inp in tx2.inputs():
            if inp['prevout_n'] == 0 and inp['prevout_hash'] == parent_txid:
                util.print_error(f"_find_child: found child {tx_hash} via UTXO heuristic")
                utxo_found = tx2
                break
        if utxo_found:
            break
        # Walk back up to max_walk_depth hops through parent txs
        frontier = []
        for inp in tx2.inputs():
            ph = inp['prevout_hash']
            if ph not in checked:
                frontier.append(ph)
        for depth in range(max_walk_depth - 1):
            next_frontier = []
            for prev_hash in frontier:
                if len(checked) >= max_utxo_checks:
                    break
                if not result_q.empty():
                    break
                if prev_hash in checked:
                    continue
                checked.add(prev_hash)
                try:
                    prev_tx = wallet.try_to_get_tx(prev_hash, allow_network_lookup=True,
                                                   timeout=timeout)
                except util.TimeoutException:
                    continue
                if not prev_tx:
                    continue
                for inp in prev_tx.inputs():
                    if inp['prevout_n'] == 0 and inp['prevout_hash'] == parent_txid:
                        util.print_error(f"_find_child: found child {prev_hash} via UTXO"
                                         f" walk-back (depth {depth + 2} from {tx_hash})")
                        utxo_found = prev_tx
                        break
                if utxo_found:
                    break
                for inp in prev_tx.inputs():
                    ph = inp['prevout_hash']
                    if ph not in checked:
                        next_frontier.append(ph)
            if utxo_found or not result_q.empty():
                break
            frontier = next_frontier
    if utxo_found:
        return utxo_found
    util.print_error(f"_find_child: UTXO heuristic checked {len(checked) - 1} txs, no child found")
    # Walk-back failed or aborted — get history result
    try:
        result = result_q.get(True, timeout)
    except queue.Empty:
        result = None
    return result


def _find_child_via_history(wallet, scripthash, parent_txid, timeout=30,
                            from_height=0, stop_check=None) -> Optional[Transaction]:
    """Search address history for the tx that spends parent_txid:0.
    If stop_check (a queue) is provided and non-empty, bail out early (other search found it)."""
    try:
        params = [scripthash, from_height] if from_height > 0 else [scripthash]
        util.print_error(f"_find_child_hist: parent={parent_txid}, scripthash={scripthash},"
                         f" from_height={from_height}")
        h2 = wallet.network.synchronous_get(
            ("blockchain.scripthash.get_history", params), timeout=timeout)
    except Exception as e:
        util.print_error(f"_find_child_hist: get_history failed for {scripthash}: {e!r}")
        return None
    candidates = [item for item in h2 if item.get('tx_hash', '') != parent_txid]
    util.print_error(f"_find_child_hist: history has {len(h2)} entries, {len(candidates)} candidates")
    for item in candidates:
        if stop_check is not None and not stop_check.empty():
            util.print_error("_find_child_hist: stopping early, other search found result")
            return None
        tx_hash = item.get('tx_hash', '')
        try:
            tx2 = wallet.try_to_get_tx(tx_hash, allow_network_lookup=True, timeout=timeout)
        except util.TimeoutException:
            return None
        if not tx2:
            continue
        for inp in tx2.inputs():
            if inp['prevout_n'] == 0 and inp['prevout_hash'] == parent_txid:
                util.print_error(f"_find_child_hist: found child {tx_hash}")
                return tx2
    return None


def _get_bcmr_pushes_from_tx(tx) -> Optional[List[bytes]]:
    """Check a transaction for a BCMR publication output. Returns the OP_RETURN
    pushes (hash + urls) if found, or None."""
    for i, (_, script, _) in enumerate(tx.outputs()):
        if isinstance(script, address.ScriptOutput) and script.is_opreturn():
            try:
                pushes = address.Script.get_ops(script.to_script()[1:])
            except address.ScriptError:
                continue
            if (all(isinstance(t, tuple) and len(t) == 2 and isinstance(t[0], int)
                    and isinstance(t[1], (bytes, bytearray)) for t in pushes)
                    and len(pushes) >= 2 and pushes[0] == (4, b'BCMR') and pushes[1][0] == 32):
                return [p[1] for p in pushes[1:]]
    return None


def _walk_back_to_genesis(wallet, tx, genesis_txid, timeout=30,
                          max_depth=100) -> Tuple[bool, Optional[List[bytes]]]:
    """Walk backwards from tx following inputs that spend output 0, looking for
    a connection to genesis_txid. Returns (connected, best_pushes) where
    best_pushes is the most recent BCMR publication found during the walk
    (closest to the starting tx)."""
    best_pushes = None
    current_tx = tx
    for depth in range(max_depth):
        # Find inputs spending output 0 of some parent
        for inp in current_tx.inputs():
            if inp['prevout_n'] != 0:
                continue
            prev_hash = inp['prevout_hash']
            if prev_hash == genesis_txid:
                util.print_error(f"_walk_back: reached genesis {genesis_txid} at depth {depth}")
                return True, best_pushes
            try:
                prev_tx = wallet.try_to_get_tx(prev_hash, allow_network_lookup=True, timeout=timeout)
            except util.TimeoutException:
                continue
            if not prev_tx:
                continue
            # Check for BCMR publication in this intermediate tx
            pushes = _get_bcmr_pushes_from_tx(prev_tx)
            if pushes is not None and best_pushes is None:
                # Keep the most recent (closest to authhead)
                util.print_error(f"_walk_back: found BCMR in {prev_hash} at depth {depth}")
                best_pushes = pushes
            current_tx = prev_tx
            break  # Follow the first prevout_n==0 input found
        else:
            # No input with prevout_n==0 found
            util.print_error(f"_walk_back: no prevout_n==0 input at depth {depth}")
            return False, best_pushes
    return False, best_pushes


def try_to_get_bcmr_op_return_pushes(wallet, token_id_hex, timeout=30) -> Optional[List[bytes]]:
    """Find the authhead and return its BCMR publication pushes. Uses a UTXO-based
    heuristic (authhead likely shares address with genesis output 0) with walk-back
    verification, falling back to forward walk if needed."""
    genesis_tx = try_to_find_genesis_tx(wallet, token_id_hex, timeout)
    if not genesis_tx:
        return None
    genesis_txid = genesis_tx.txid()
    genesis_pushes = _get_bcmr_pushes_from_tx(genesis_tx)

    # If genesis output 0 is OP_RETURN, it's unspendable — genesis is the authhead.
    # Per BCMR spec this is a "burned" identity, but it may still contain the publication.
    outputs = genesis_tx.outputs()
    if outputs and isinstance(outputs[0][1], address.ScriptOutput) and outputs[0][1].is_opreturn():
        util.print_error(f"Genesis {genesis_txid} output 0 is OP_RETURN — genesis is authhead")
        return genesis_pushes

    # Get UTXOs at the genesis output 0 address
    scripthash = _scripthash_for_output0(genesis_tx)
    if not scripthash or not wallet.network:
        # Can't do UTXO heuristic, check genesis only
        return genesis_pushes

    try:
        utxos = wallet.network.synchronous_get(
            ("blockchain.scripthash.listunspent", [scripthash]), timeout=timeout)
    except Exception as e:
        util.print_error(f"listunspent failed for genesis output 0: {e!r}")
        return genesis_pushes

    # Check if genesis:0 is unspent — genesis is the authhead
    for u in utxos:
        if u['tx_hash'] == genesis_txid and u['tx_pos'] == 0:
            util.print_error(f"Genesis {genesis_txid} is authhead (output 0 unspent)")
            return genesis_pushes

    # Genesis:0 is spent — authhead is further down the chain.
    # Heuristic: check UTXOs at this address, walk back to verify connection.
    for u in utxos:
        tx_hash = u['tx_hash']
        try:
            utxo_tx = wallet.try_to_get_tx(tx_hash, allow_network_lookup=True, timeout=timeout)
        except util.TimeoutException:
            continue
        if not utxo_tx:
            continue
        # Only consider UTXOs at output 0 — potential authhead identity outputs
        if u['tx_pos'] != 0:
            continue
        connected, walkback_pushes = _walk_back_to_genesis(wallet, utxo_tx, genesis_txid, timeout)
        if connected:
            util.print_error(f"Found authhead via walk-back: {tx_hash}")
            # Per spec, authhead's publication takes precedence
            authhead_pushes = _get_bcmr_pushes_from_tx(utxo_tx)
            if authhead_pushes:
                return authhead_pushes
            # Fallback: most recent publication found during walk-back
            if walkback_pushes:
                return walkback_pushes
            # Last resort: genesis publication
            return genesis_pushes

    # Heuristic failed — authchain left the address. Fall back to forward walk.
    util.print_error(f"UTXO heuristic failed for {token_id_hex}, falling back to forward walk")
    best_pushes = genesis_pushes
    current_tx = genesis_tx
    current_txid = genesis_txid
    for depth in range(100):
        current_height = _get_tx_height(wallet, current_txid, timeout)
        child = _find_child_spending_output0(wallet, current_tx, current_txid, timeout,
                                              from_height=current_height)
        if child is None:
            util.print_error(f"Forward walk: authhead at depth {depth}: {current_txid}")
            break
        current_tx = child
        current_txid = current_tx.txid()
        pushes = _get_bcmr_pushes_from_tx(current_tx)
        if pushes is not None:
            util.print_error(f"Forward walk: BCMR in {current_txid} at depth {depth}")
            best_pushes = pushes
    return best_pushes


class DownloadedMetaData:
    """Encapsulates downloaded metadata"""
    __slots__ = ('name', 'description', 'decimals', 'symbol', 'icon', 'icon_ext')

    name: str
    description: str
    decimals: int
    symbol: str
    icon: Optional[bytes]
    icon_ext: Optional[str]

    def __init__(self):
        self.name = self.description = self.symbol = ''
        self.decimals = 0
        self.icon = self.icon_ext = None

    def __repr__(self):
        icon_thing = len(self.icon) if self.icon is not None else None
        return f"<DownloadedMetaData name={self.name} description={self.description} decimals={self.decimals}" \
               f" symbol={self.symbol}, icon_ext={self.icon_ext} icon={icon_thing} bytes>"

    def sanitize(self):
        """Cleans up some fields for this object to enforce invariants"""
        try:
            self.decimals = int(self.decimals)
        except (ValueError, TypeError):
            pass
        # BCMR spec: "An integer between 0 and 18 (inclusive)"
        self.decimals = min(max(0, self.decimals), 18) if isinstance(self.decimals, int) else 0
        # BCMR schema: "names should be hidden beyond ... 20 characters until revealed by the user"
        # We allow up to 2 x 20 = 40 characters
        self.name = self.name[:40] if isinstance(self.name, str) else ""
        # BCMR spec: "descriptions should be hidden beyond ... 140 characters until revealed by the user"
        # We allow up to 2 x 140 = 280 characters
        self.description = self.description[:280] if isinstance(self.description, str) else ""
        # BCMR spec: "this standard recommends that... clients accommodate up to 26 characters for full symbols"
        self.symbol = self.symbol[:26] if isinstance(self.symbol, str) else ""


def _rewrite_if_ipfs(u: str) -> str:
    """Rewrites any ipfs-style URLs to https using a proxy site that serves such things"""
    if u.lower().startswith("ipfs://"):
        parts = u[7:].split('/', 1)
        last_part = '/' + '/'.join(parts[1:]) if len(parts) >= 2 else ''
        cid = parts[0]
        ret = f"https://dweb.link/ipfs/{cid}{last_part}"
        util.print_error(f"Rewrote \"{u}\" -> \"{ret}\"")
        return ret
    else:
        return u


def _try_to_dl_icon(icon_url: str, *, timeout=30) -> Optional[Tuple[bytes, str]]:
    """Given an icon url, download the icon and return a tuple of (icon_bytes, filename_extension) or None on error"""
    if not icon_url or not isinstance(icon_url, str):
        return
    icon_url = _rewrite_if_ipfs(icon_url)
    r2 = requests.get(icon_url, timeout=timeout, allow_redirects=True)
    if not r2.ok:
        util.print_error(f"Got error downloading icon from {icon_url}: {r2.status_code} {r2.reason}")
        return
    util.print_error(f"Downloaded {len(r2.content)} bytes from {icon_url}")
    icon = r2.content
    # Figure out the icon extension from the content-type header if present, otherwise fall-back to filename-based ext.
    if r2.headers.get("Content-Type", "").startswith("image/"):
        icon_ext = "." + r2.headers["Content-Type"][6:]
        util.print_error(f"Image type from header: {icon_ext}")
    else:
        icon_ext = os.path.splitext(icon_url)[-1]
        util.print_error(f"Image type from filename: {icon_ext}")
    return icon, icon_ext



def _try_to_dl_from_paytaca_indexer(token_id_hex, timeout=30, *, skip_icon=False,
                                    nft_hex=None) -> Optional[DownloadedMetaData]:
    """Download metadata from the paytaca indexer"""
    host = networks.net.PAYTACA_HOST
    if host:
        return None
    if not nft_hex:
        url = f"https://{host}/api/tokens/{token_id_hex}/"
    else:
        url = f"https://{host}/api/tokens/{token_id_hex}/{nft_hex}"
    r = requests.get(url, timeout=timeout, allow_redirects=True)
    if not r.ok:
        util.print_error(f"Got error requesting url {url}: {r.status_code} {r.reason}")
        return
    try:
        jdoc = json.loads(r.content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError) as e:
        util.print_error(f"Got exception decoding from {url}: {e!r}")
        return
    if not isinstance(jdoc, dict):
        util.print_error(f"Invalid format for JSON content downloaded from {url}, not a dict: {jdoc}")
        return
    if "error" in jdoc:
        util.print_error(f"Got error reply from {url}: {jdoc.get('error')}")
        return
    md = DownloadedMetaData()
    md.name = jdoc.get("name")
    if not md.name:
        util.print_error(f'Required key "name" missing or empty in JSON downloaded from {url}')
        return
    md.description = jdoc.get("description", "")

    # Handle NFT-specific metadata
    nft_icon_override = None
    if nft_hex and "type_metadata" in jdoc:
        ndict = jdoc["type_metadata"]
        if isinstance(ndict, dict):
            nft_name = ndict.get("name")
            if isinstance(nft_name, str):
                md.name = nft_name
            nft_desc = ndict.get("description")
            if isinstance(nft_desc, str):
                md.description = nft_desc
            nft_uris = ndict.get("uris")
            if isinstance(nft_uris, dict):
                nft_icon = nft_uris.get("icon")
                if nft_icon and isinstance(nft_icon, str):
                    nft_icon_override = nft_icon

    tdict = jdoc.get("token")
    if not isinstance(tdict, dict) or tdict.get("category") != token_id_hex:
        util.print_error(f'Invalid "token" dict downloaded from {url}: {tdict}')
        return
    md.symbol = tdict.get("symbol", "")
    md.decimals = tdict.get("decimals", 0)
    if nft_hex and "decimals" not in tdict:
        # Hack to get the "decimals" from the NFT parent if missing in child NFT results
        util.print_error(f'Missing "decimals" for NFT, downloading parent info for: {token_id_hex} ...')
        md2 = _try_to_dl_from_paytaca_indexer(token_id_hex, timeout=timeout, skip_icon=True, nft_hex=None)
        if md2:
            md.decimals = md2.decimals

    # Next, try and download the icon
    if not skip_icon:
        icon_url = nft_icon_override or None
        if not icon_url:
            udict = jdoc.get("uris")
            if isinstance(udict, dict) and "icon" in udict:
                icon_url = udict["icon"]
        res = _try_to_dl_icon(icon_url, timeout=timeout)
        if res:
            md.icon, md.icon_ext = res
    md.sanitize()
    return md


def _try_to_dl_from_blockchain(wallet, token_id_hex, *, timeout=30, skip_icon=False) -> Optional[DownloadedMetaData]:
    """Synchronously find the genesis tx, download metadata if it has properly formed BCMR, and return
    an object describing what was found. May return None on timeout or other error."""
    pushes = try_to_get_bcmr_op_return_pushes(wallet, token_id_hex, timeout=timeout)
    if not pushes or len(pushes) < 2:
        return None

    shasum = pushes[0]
    for url in pushes[1:]:
        try:
            url = url.decode("utf-8")
        except UnicodeError as e:
            util.print_error(f"Failed to decode url: {url!r} as utf-8, skipping...")

        url = _rewrite_if_ipfs(url)
        if not url.lower().startswith("https://"):
            url = "https://" + url
        r = requests.get(url, timeout=timeout)
        if r.ok:
            util.print_error(f"Downloaded {len(r.content)} bytes from {url}")
            sha = hashlib.sha256()
            sha.update(bytes(r.content))
            digest = sha.digest()
            if digest != shasum and digest[::-1] != shasum:
                util.print_error(f"Warning: hash mismatch for json document at {url}, proceeding anyway...")
            try:
                jdoc = json.loads(r.content.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeError) as e:
                util.print_error(f"Got exception decoding from {url}: {e!r}")
                continue
            identities = jdoc.get("identities", {})
            if not identities and not isinstance(identities, dict):
                util.print_error(f"Bad identity found from {url}")
                continue
            for identity, d in identities.items():
                if isinstance(d, list):
                    # Support broken spec
                    d = {-i:val for i, val in enumerate(d)}
                if not isinstance(d, dict) or not d:
                    util.print_error(f"Expected dict in identity {identity} from {url}")
                    break
                times = sorted(d.keys(), reverse=True)
                for t in times:
                    dd = d[t]
                    tok = dd.get("token", {})
                    if not tok or not isinstance(tok, dict):
                        util.print_error(f'Expected a "token" dict in identity {identity}:{t} from {url}')
                        continue
                    cat = tok.get("category", "")
                    if cat != token_id_hex:
                        util.print_error(f"Skipping category {cat}")
                        continue
                    decimals = tok.get("decimals", 0)
                    name = dd.get("name", "")
                    description = dd.get("description", "")
                    symbol = tok.get("symbol", "")

                    md = DownloadedMetaData()
                    md.decimals = decimals
                    md.symbol = symbol
                    md.name = name
                    md.description = description

                    uris = dd.get("uris", {})
                    if not skip_icon and uris and isinstance(uris, dict):
                        icon_url = uris.get("icon")
                        res = _try_to_dl_icon(icon_url, timeout=timeout)
                        if res:
                            md.icon, md.icon_ext = res
                    md.sanitize()
                    return md
        else:
            util.print_error(f"Got error requesting url {url}: {r.status_code} {r.reason}")


def try_to_download_metadata(wallet, token_id_hex, timeout=30, *, skip_icon=False,
                             use_indexers=True, use_blockchain=True,
                             nft_hex: Optional[str] = None) -> Optional[DownloadedMetaData]:
    """Tries to get BCMR metadata given a token_id_hex (category id). First, tries the paytaca indexer (faster),
    then if that fails, falls-back to blockchain-based BCMR metadata resolution. Will return None on failure.
    Be sure to catch exceptions as this may raise an exception from the `requests` module."""
    assert use_indexers or use_blockchain, "Must specify at least one of: use_indexers, use_blockchain"
    assert not nft_hex or use_indexers, "Must specify use_indexers=True if trying to get metadata for an nft"

    # First, try paytaca indexer (faster)
    if use_indexers:
        md = _try_to_dl_from_paytaca_indexer(token_id_hex, timeout=timeout, skip_icon=skip_icon,
                                             nft_hex=nft_hex)
        if md is not None:
            util.print_error(f"Success in downloading token metadata from {networks.net.PAYTACA_HOST} for:"
                             f" {token_id_hex} ({md.name})")
            return md

    # If indexer fails, try the blockchain (slower)
    if use_blockchain and not nft_hex:
        util.print_error(f"Falling-back to slower blockchain method to retrieve BCMR data for: {token_id_hex} ...")
        md = _try_to_dl_from_blockchain(wallet, token_id_hex, timeout=timeout, skip_icon=skip_icon)
        if md is not None:
            util.print_error(f"Success in downloading token metadata from blockchain for: {token_id_hex} ({md.name})")
            return md

    util.print_error(f"Failed to retrieve metadata for: {token_id_hex}")
