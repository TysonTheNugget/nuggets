from flask import Flask, jsonify
import os
import json
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from tenacity import retry, stop_after_attempt, wait_exponential
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import time
import zlib
import re

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
BITCOIN_ADDRESS = os.getenv("BITCOIN_ADDRESS")
BLOCKCHAIR_API_KEY = os.getenv("BLOCKCHAIR_API_KEY")
# Use /latest for reading
JSONBIN_BASE_URL = "https://api.jsonbin.io/v3/b/68e4bbccae596e708f08e631"
JSONBIN_URL_LATEST = f"{JSONBIN_BASE_URL}/latest"
JSONBIN_URL_WRITE = JSONBIN_BASE_URL  # PUT here to update bin
JSONBIN_MASTER_KEY = os.getenv("JSONBIN_MASTER_KEY", "$2a$10$tWgX8avz4dzMiP.ulPMuu.wdShbGcrGy9M1Z4FUBVNSHTBpjfg/mq")
SINGLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'Singles')
os.makedirs(SINGLES_DIR, exist_ok=True)
MEMPOOL = "https://mempool.space/api"
BLOCKCHAIR = "https://api.blockchair.com/bitcoin"
SCAN_SINCE_UNIX = int(os.getenv("SCAN_SINCE_UNIX", "1759795200"))  # Oct 7, 2025 00:00:00 UTC
PNG_TEXT_KEY_HINT = os.getenv("PNG_TEXT_KEY_HINT", "Serial")
CONTENT_HOSTS = [
    "https://static.unisat.io/content",
    "https://ordinals.com/content",
    "https://api.hiro.so/ordinals/v1/inscriptions"
]
PNG_SIG = b'\x89PNG\r\n\x1a\n'

# HTTP session with retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.4, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

# ---------------------------
# JSONBin helpers (robust)
# ---------------------------
def _log_preview(thing, limit=200):
    try:
        s = thing if isinstance(thing, str) else json.dumps(thing)  # may raise for bytes/etc.
        return s[:limit]
    except Exception:
        return f"<unprintable:{type(thing)}>"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_jsonbin():
    try:
        r = session.get(
            JSONBIN_URL_LATEST,
            headers={"X-Master-Key": JSONBIN_MASTER_KEY},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        logger.debug(f"[JSONBin] Raw type={type(data)} preview={_log_preview(data)}")

        # JSONBin v3 typically returns {"record": ...}
        record = None
        if isinstance(data, dict) and "record" in data:
            record = data["record"]
        else:
            record = data

        # Normalize: record may be dict or list
        if isinstance(record, dict):
            # Your schema stored as {"mints":[...]} — keep that path
            mints = record.get("mints", [])
            if isinstance(mints, list):
                return mints
            # If someone stored the list directly under the bin:
            if "mints" not in record:
                # permissive: if dict isn't your schema, treat as empty
                logger.warning("[JSONBin] Dict without 'mints' key; returning empty list")
                return []
            logger.warning("[JSONBin] 'mints' exists but is not a list; returning empty list")
            return []
        elif isinstance(record, list):
            # If the bin itself is a list, use it directly (assume it's the mints list)
            return record
        else:
            logger.warning(f"[JSONBin] Unsupported top-level type: {type(record)}; returning empty list")
            return []
    except Exception as e:
        logger.error(f"[JSONBin] Error reading bin: {e}")
        return []

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def update_jsonbin(mints):
    try:
        payload = {"mints": mints}
        r = session.put(
            JSONBIN_URL_WRITE,
            headers={"X-Master-Key": JSONBIN_MASTER_KEY, "Content-Type": "application/json"},
            json=payload,
            timeout=15
        )
        r.raise_for_status()
        logger.info("[JSONBin] Updated bin with %d mints", len(mints))
        return True
    except Exception as e:
        logger.error(f"[JSONBin] Error updating bin: {e}")
        return False

# ---------------------------
# Bitcoin transaction helpers
# ---------------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_mempool_txs():
    try:
        r = session.get(f"{MEMPOOL}/address/{BITCOIN_ADDRESS}/txs/mempool", timeout=15)
        r.raise_for_status()
        data = r.json()
        logger.debug(f"[Mempool] mempool txs type={type(data)} len={len(data) if isinstance(data, list) else 'N/A'}")
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"[Mempool] Error fetching mempool txs: {e}")
        return []

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_tx_detail_mempool(txid: str):
    """Hydrate a txid into a full tx object with vout/status via mempool.space."""
    try:
        r = session.get(f"{MEMPOOL}/tx/{txid}", timeout=20)
        r.raise_for_status()
        j = r.json()
        # Normalize: mempool /tx/{txid} returns fields 'txid','status','vin','vout', etc.
        return j
    except Exception as e:
        logger.error(f"[Mempool] Error fetching tx detail for {txid}: {e}")
        return None

def _blockchair_fetch_txids_dashboards(address, limit=100, max_pages=10, pause=0.25):
    txids = []
    offset = 0
    for _ in range(max_pages):
        try:
            url = f"{BLOCKCHAIR}/dashboards/address/{address}"
            params = {"limit": limit, "offset": offset}
            if BLOCKCHAIR_API_KEY:
                params["key"] = BLOCKCHAIR_API_KEY
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(1.0)
                continue
            r.raise_for_status()
            j = r.json()
            # structure: {"data": { "<address>": { "transactions": [ ...txids... ], ... } } }
            data_for_addr = j.get("data", {}).get(address, {})
            page_txids = data_for_addr.get("transactions", [])
            if not page_txids:
                break
            txids.extend(page_txids)
            if len(page_txids) < limit:
                break
            offset += limit
            time.sleep(pause)
        except Exception as e:
            logger.warning(f"[Blockchair] dashboards/address fallback to outputs. Reason: {e}")
            return []
    return txids

def _blockchair_fetch_txids_outputs(address, limit=100, max_pages=10, pause=0.25):
    txids = []
    offset = 0
    for _ in range(max_pages):
        try:
            url = f"{BLOCKCHAIR}/outputs"
            params = {"q": f"recipient({address})", "limit": limit, "offset": offset}
            if BLOCKCHAIR_API_KEY:
                params["key"] = BLOCKCHAIR_API_KEY
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(1.0)
                continue
            r.raise_for_status()
            j = r.json()
            rows = j.get("data", [])
            if not rows:
                break
            page_txids = [row.get("transaction_hash") for row in rows if row.get("transaction_hash")]
            if not page_txids:
                break
            txids.extend(page_txids)
            if len(rows) < limit:
                break
            offset += limit
            time.sleep(pause)
        except Exception as e:
            logger.error(f"[Blockchair] outputs recipient() error: {e}")
            break
    # de-dup preserve order
    seen = set(); uniq = []
    for t in txids:
        if t not in seen:
            uniq.append(t); seen.add(t)
    return uniq

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_chain_txs(pages=100, limit=100):
    """
    Returns a list of full tx objects (with vout/status) for the address,
    limited by SCAN_SINCE_UNIX on the caller.
    """
    # Try dashboards first
    txids = _blockchair_fetch_txids_dashboards(BITCOIN_ADDRESS, limit=limit, max_pages=pages)
    if not txids:
        # Fallback to outputs recipient()
        txids = _blockchair_fetch_txids_outputs(BITCOIN_ADDRESS, limit=limit, max_pages=pages)

    if not txids:
        logger.info("[Blockchair] No txids found for address.")
        return []

    # Hydrate via mempool.space
    txs = []
    for txid in txids:
        tx = fetch_tx_detail_mempool(txid)
        if tx:
            txs.append(tx)
    logger.info(f"[Scan] Hydrated {len(txs)} transactions from {len(txids)} txids")
    return txs

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def get_outspends(txid):
    try:
        r = session.get(f"{MEMPOOL}/tx/{txid}/outspends", timeout=15)
        r.raise_for_status()
        data = r.json()
        logger.debug(f"[Mempool] Outspends for {txid}: {data}")
        return data
    except Exception as e:
        logger.error(f"[Mempool] Error fetching outspends for {txid}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_binary_once(url, headers):
    try:
        r = session.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"[FetchBinary] Error fetching {url}: {e}")
        raise

def fetch_binary_with_fallback(inscription_id):
    errors = []
    for base in CONTENT_HOSTS:
        url = f"{base}/{inscription_id}/content" if "hiro.so" in base else f"{base}/{inscription_id}"
        headers = {"Accept": "image/png,application/octet-stream;q=0.9,*/*;q=0.8"}
        try:
            buf = fetch_binary_once(url, headers)
            if buf.startswith(PNG_SIG):
                return buf
            errors.append(f"non-PNG from {base}")
        except Exception as e:
            errors.append(f"{base}: {e}")
    raise Exception(f"All content hosts failed: {' | '.join(errors)}")

def parse_png_text(buf):
    if not buf.startswith(PNG_SIG):
        return {"ok": False, "text": None}
    text = {}
    off = 8
    while off + 8 <= len(buf):
        if off + 8 > len(buf): break
        len_chunk = int.from_bytes(buf[off:off+4], "big")
        off += 4
        type_chunk = buf[off:off+4].decode("latin1")
        off += 4
        if off + len_chunk > len(buf): break
        data = buf[off:off+len_chunk]
        off += len_chunk
        off += 4  # Skip CRC

        if type_chunk == "tEXt":
            zero = data.find(b'\x00')
            if zero >= 0:
                k = data[:zero].decode("latin1")
                v = data[zero+1:].decode("latin1")
                text[k] = v
        elif type_chunk == "zTXt":
            zero = data.find(b'\x00')
            if zero >= 0:
                k = data[:zero].decode("latin1")
                comp_method = data[zero+1]
                comp_data = data[zero+2:]
                if comp_method == 0:
                    try:
                        v = zlib.decompress(comp_data).decode("utf-8")
                        text[k] = v
                    except Exception as e:
                        text[k] = f"<zTXt decompress error: {e}>"
        elif type_chunk == "iTXt":
            parts = data.split(b'\x00', 5)
            if len(parts) >= 5:
                k, comp_flag, comp_method, _, _, payload = parts[:5]
                try:
                    v = zlib.decompress(payload).decode("utf-8") if comp_flag == b'\x01' else payload.decode("utf-8")
                    text[k.decode("latin1")] = v
                except Exception as e:
                    text[k.decode("latin1")] = f"<iTXt error: {e}>"

        if type_chunk == "IEND":
            break
    return {"ok": bool(text), "text": text}

def get_case_insensitive(map_obj, key):
    keys = map_obj.keys() if map_obj else []
    k = next((k for k in keys if k.lower() == key.lower()), None)
    return map_obj.get(k) if k else None

def maybe_serial_from_json_values(text_map):
    for v in text_map.values():
        if not isinstance(v, str):
            continue
        v = v.strip()
        if not v:
            continue
        try:
            obj = json.loads(v)
            s = obj.get("serial") or obj.get("Serial") or (obj.get("name") if re.match(r'^[A-Za-z0-9]{10,24}$', str(obj.get("name", ""))) else None)
            if s:
                return str(s)
        except:
            continue
    return None

def find_alnum_token(s):
    if not isinstance(s, str):
        return None
    m = re.search(r'\b[A-Za-z0-9]{10,24}\b', s)
    return m.group(0) if m else None

def find_png_inscription_id(txid, max_index=5):
    for i in range(max_index + 1):
        id = f"{txid}i{i}"
        try:
            buf = fetch_binary_with_fallback(id)
            if buf.startswith(PNG_SIG):
                return id
        except:
            continue
    return None

# ---------------------------
# Scan transactions
# ---------------------------
def scan_transactions(initial=False):
    mints = get_jsonbin() if not initial else []
    seen_set = {m.get("txid") for m in mints if isinstance(m, dict)}
    txs = []

    if initial:
        logger.info("[Scan] Performing initial full scan...")
        chain_txs = fetch_chain_txs(pages=100, limit=100)
        # Filter by SCAN_SINCE_UNIX (if status/block_time present)
        txs = [t for t in chain_txs if isinstance(t, dict) and t.get("status", {}).get("block_time", 0) >= SCAN_SINCE_UNIX]
    else:
        logger.info("[Scan] Scanning mempool for updates...")
        txs = fetch_mempool_txs()

    new_mints = 0
    for tx in txs:
        txid = tx.get("txid")
        if not txid or txid in seen_set:
            continue

        try:
            outspends = get_outspends(txid)
            if not outspends:
                continue

            vouts = tx.get("vout", [])
            candidates = []
            for idx, spent in enumerate(outspends[:len(vouts)]):
                if spent.get("spent"):
                    reveal_txid = spent.get("txid")
                    if reveal_txid:
                        candidates.append(reveal_txid)

            uniq_candidates = list(set(candidates))
            inscription_id = None
            for reveal_txid in uniq_candidates[:1]:
                inscription_id = find_png_inscription_id(reveal_txid)
                if inscription_id:
                    break

            if not inscription_id:
                continue

            buf = fetch_binary_with_fallback(inscription_id)
            parsed = parse_png_text(buf)
            text_map = parsed.get("text", {}) or {}
            serial = (get_case_insensitive(text_map, PNG_TEXT_KEY_HINT) or
                      maybe_serial_from_json_values(text_map) or
                      find_alnum_token(' '.join([str(v) for v in text_map.values() if isinstance(v, str)])))

            if not serial:
                continue

            buyer_addr = next((vout.get("scriptpubkey_address") for vout in vouts if vout.get("scriptpubkey_address")), "")
            timestamp = tx.get("status", {}).get("block_time", int(time.time()))

            image_file = None
            if os.path.exists(SINGLES_DIR):
                try:
                    for fname in os.listdir(SINGLES_DIR):
                        if fname.lower().endswith(".png") and serial in fname:
                            image_file = fname
                            break
                except Exception as e:
                    logger.warning(f"[Scan] Error scanning SINGLES_DIR: {e}")

            mint_item = {
                "txid": txid,
                "serial": serial,
                "buyerAddr": buyer_addr,
                "confirmedAt": timestamp,
                "imageFile": image_file
            }
            mints.append(mint_item)
            seen_set.add(txid)
            new_mints += 1
            logger.debug(f"[Scan] Added mint: {serial} for tx {txid}")

        except Exception as e:
            logger.error(f"[Scan] Error processing tx {txid}: {e}")

    logger.info(f"[Scan] Processed {len(txs)} txs, found {new_mints} new mints")
    mints.sort(key=lambda x: x.get("confirmedAt", 0), reverse=True)
    if mints:
        update_jsonbin(mints)
    return mints

# ---------------------------
# Scheduler to update JSONBin every 30 seconds
# ---------------------------
def update_mints_job():
    try:
        logger.info("[Scheduler] Running mints update job...")
        existing_mints = get_jsonbin()
        if not existing_mints:
            logger.info("[Scheduler] JSONBin empty, performing initial scan...")
            scan_transactions(initial=True)
        else:
            logger.info("[Scheduler] Performing mempool scan...")
            scan_transactions(initial=False)
    except Exception as e:
        logger.error(f"[Scheduler] Error in update job: {e}")

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(update_mints_job, 'interval', seconds=30, max_instances=1, coalesce=True, misfire_grace_time=10)
scheduler.start()
logger.info("[Scheduler] Started: updating JSONBin every 30s")

# ---------------------------
# Routes
# ---------------------------
@app.route('/mints')
def mints():
    try:
        mints = get_jsonbin()
        return jsonify({
            "ok": True,
            "mints": mints,
            "total": len(mints)
        })
    except Exception as e:
        logger.error(f"[Mints] Error serving mints: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/debug')
def debug():
    try:
        mints = get_jsonbin()
        return jsonify({
            "total_mints": len(mints),
            "sample_mint": mints[0] if mints else None,
            "jsonbin_status": "updated" if mints else "empty"
        })
    except Exception as e:
        logger.error(f"[Debug] Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    if not BITCOIN_ADDRESS or not BLOCKCHAIR_API_KEY:
        logger.error("Missing BITCOIN_ADDRESS or BLOCKCHAIR_API_KEY environment variables")
        exit(1)
    app.run(debug=True, host='0.0.0.0', port=5000)
