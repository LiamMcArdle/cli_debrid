import pickle
import os
import re
import logging
import tempfile
import threading
from contextlib import contextmanager
from utilities.settings import get_setting
from utilities.file_lock import FileLock


@contextmanager
def _file_guard(target_path):
    """Hold an exclusive lock for the duration of a read-modify-write on target_path.

    The lock lives in a sidecar .lock file rather than in the pickle itself: opening
    the pickle 'wb' truncates it before any lock on it could be acquired, which is
    the window that loses data.
    """
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    lock_file = open(target_path + '.lock', 'a+')
    try:
        with FileLock(lock_file):
            yield
    finally:
        try:
            lock_file.close()
        except Exception:
            pass


def _atomic_dump(target_path, data):
    """Write data to target_path via a temp file and an atomic rename.

    Concurrent readers see either the previous complete file or the new complete
    file, never a truncated one.
    """
    directory = os.path.dirname(target_path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.not_wanted-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            pickle.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; match the 0644 a plain open() would have produced.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, target_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_pickle(target_path):
    """Return (set, trustworthy). Never raises.

    trustworthy is False when the file exists but could not be understood. Callers
    must not persist the empty set in that case: writing it back turns a transient
    read failure into permanent loss of the whole list.
    """
    try:
        with open(target_path, 'rb') as f:
            data = pickle.load(f)
    except FileNotFoundError:
        return set(), True
    except Exception as e:
        logging.error(
            f"not_wanted: could not read {target_path}: {e!r} - "
            f"treating as unreadable, NOT as empty"
        )
        return set(), False

    if not isinstance(data, set):
        logging.error(
            f"not_wanted: {target_path} contained {type(data).__name__}, expected set - "
            f"treating as unreadable, NOT as empty"
        )
        return set(), False

    return data, True


def _load_store(target_path):
    data, _ = _read_pickle(target_path)
    return data


# Normalised-set cache, keyed on the file's identity rather than a TTL.
#
# is_magnet_not_wanted unpickled ~3MB and rebuilt a 47k-element set through
# get_base_filename's regexes on every single call — ~37ms, called twice per
# candidate result and ~50 results per scrape. Over a backlog run that is hours
# of pure overhead, and it grows as the list does.
#
# Keying on (mtime_ns, size) means a write through _save_store or _add_to_store
# invalidates it for free, including writes from another process.
_NORMALISED_CACHE = {}
_NORMALISED_CACHE_LOCK = threading.Lock()


def _load_normalised_store(target_path):
    """Return the store as a set of base filenames, cached until the file changes."""
    try:
        st = os.stat(target_path)
        stamp = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        stamp = None
    except OSError:
        # Cannot identify the file, so cannot safely cache against it.
        return {get_base_filename(v) for v in _load_store(target_path) if v is not None}

    with _NORMALISED_CACHE_LOCK:
        entry = _NORMALISED_CACHE.get(target_path)
        if entry is not None and entry[0] == stamp:
            return entry[1]

    normalised = {get_base_filename(v) for v in _load_store(target_path) if v is not None}

    with _NORMALISED_CACHE_LOCK:
        _NORMALISED_CACHE[target_path] = (stamp, normalised)
    return normalised


def _save_store(target_path, values):
    if not isinstance(values, set):
        raise TypeError(f"expected a set for {target_path}, got {type(values).__name__}")
    with _file_guard(target_path):
        _atomic_dump(target_path, values)


def _add_to_store(target_path, value):
    """Locked read-modify-write. Returns True if the value was newly added."""
    with _file_guard(target_path):
        values, trustworthy = _read_pickle(target_path)
        if not trustworthy:
            logging.error(
                f"not_wanted: refusing to overwrite unreadable {target_path} with a "
                f"single-entry set; leaving it untouched"
            )
            return False
        if value in values:
            return False
        values.add(value)
        _atomic_dump(target_path, values)
        return True


def normalize_title(t: str) -> str:
    """Normalize a torrent title for not-wanted comparison.
    Lowercases, collapses dots to spaces, strips leading [group] tags and
    trailing container extensions so titles match regardless of formatting.
    """
    t = (t or '').lower()
    # Strip leading bracket tags like [tvN], [YIFY], [GroupName] — these vary
    # between indexers and cause mismatches on the same underlying torrent
    t = re.sub(r'^\s*\[[^\]]{1,20}\]\s*', '', t)
    t = re.sub(r'\.+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # Strip trailing container extensions so stored titles match Zilean titles
    t = re.sub(r'\s+(mkv|avi|mp4|mov|wmv|flv|webm|m4v|ts|m2ts|bdmv)$', '', t)
    return t

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')

# Update the paths to use the environment variable
NOT_WANTED_MAGNETS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_magnets.pkl')
NOT_WANTED_URLS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_urls.pkl')
NOT_WANTED_NZB_SEGMENTS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_nzb_segments.pkl')
NOT_WANTED_NZB_GUIDS_FILE = os.path.join(DB_CONTENT_DIR, 'not_wanted_nzb_guids.pkl')


def extract_nzb_segment_id(nzb_xml: str) -> str:
    """Extract the first segment Message-ID from NZB XML — identical across all indexers."""
    try:
        import xml.etree.ElementTree as ET
        # Strip namespace for easier parsing
        xml_clean = re.sub(r'\sxmlns="[^"]+"', '', nzb_xml, count=1)
        root = ET.fromstring(xml_clean)
        for file_el in root.iter('file'):
            segs = file_el.find('segments')
            if segs is not None:
                for seg in segs.iter('segment'):
                    msg_id = seg.text
                    if msg_id:
                        return msg_id.strip().strip('<>').lower()
    except Exception:
        pass
    return ''


def load_not_wanted_nzb_segments():
    return _load_store(NOT_WANTED_NZB_SEGMENTS_FILE)


def save_not_wanted_nzb_segments(s):
    _save_store(NOT_WANTED_NZB_SEGMENTS_FILE, s)


def add_to_not_wanted_nzb_segment(segment_id: str):
    if not segment_id:
        return
    if _add_to_store(NOT_WANTED_NZB_SEGMENTS_FILE, segment_id.strip().strip('<>').lower()):
        logging.info(f'[NZB] Added broken NZB segment ID {segment_id!r} to not-wanted list')


def is_nzb_segment_not_wanted(nzb_xml: str) -> bool:
    if get_setting('Debug', 'disable_not_wanted_check', False):
        return False
    seg_id = extract_nzb_segment_id(nzb_xml)
    if not seg_id:
        return False
    s = load_not_wanted_nzb_segments()
    if seg_id in s:
        logging.info(f'[NZB] Filtering out NZB — segment ID {seg_id!r} is in not-wanted list')
        return True
    return False


def extract_nzb_guid(url_or_guid: str) -> str:
    """Extract the indexer GUID from an NZB URL or guid string.
    Handles formats:
      - https://api.nzbgeek.info/api?t=get&id=ed914f26...
      - https://nzbgeek.info/geekseek.php?guid=ed914f26...
      - https://api.althub.co.za/getnzb/ed914f26...nzb
      - Plain guid string: ed914f26add1db0a7cc6a19c6358e5b0
    Returns normalized lowercase guid or empty string.
    """
    if not url_or_guid:
        return ''
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url_or_guid)
        qs = parse_qs(parsed.query)
        # ?id=... or ?guid=...
        for key in ('id', 'guid'):
            if key in qs:
                return qs[key][0].strip().lower()
        # Path-based: /getnzb/GUID.nzb or /getnzb/GUID&...
        path = parsed.path.rstrip('/')
        last = path.split('/')[-1]
        # Strip .nzb extension
        last = re.sub(r'\.nzb$', '', last, flags=re.IGNORECASE)
        # Strip query string remnants (althub appends &i=... to path)
        last = last.split('&')[0].split('?')[0]
        if last and re.match(r'^[0-9a-f]{16,}$', last, re.IGNORECASE):
            return last.lower()
    except Exception:
        pass
    # If it looks like a plain guid already
    if re.match(r'^[0-9a-f]{16,}$', url_or_guid.strip(), re.IGNORECASE):
        return url_or_guid.strip().lower()
    return ''


def load_not_wanted_nzb_guids():
    return _load_store(NOT_WANTED_NZB_GUIDS_FILE)


def save_not_wanted_nzb_guids(s):
    _save_store(NOT_WANTED_NZB_GUIDS_FILE, s)


def add_to_not_wanted_nzb_guid(url_or_guid: str):
    guid = extract_nzb_guid(url_or_guid)
    if not guid:
        return
    if _add_to_store(NOT_WANTED_NZB_GUIDS_FILE, guid):
        logging.info(f'[NZB] Added broken NZB guid {guid!r} to not-wanted list')


def is_nzb_guid_not_wanted(url_or_guid: str) -> bool:
    if get_setting('Debug', 'disable_not_wanted_check', False):
        return False
    guid = extract_nzb_guid(url_or_guid)
    if not guid:
        return False
    s = load_not_wanted_nzb_guids()
    if guid in s:
        logging.info(f'[NZB] Filtering out NZB — guid {guid!r} is in not-wanted list')
        return True
    return False


def load_not_wanted_magnets():
    return _load_store(NOT_WANTED_MAGNETS_FILE)

def save_not_wanted_magnets(not_wanted_set):
    _save_store(NOT_WANTED_MAGNETS_FILE, not_wanted_set)

def add_to_not_wanted(hash_value, item_identifier=None, item=None):
    if hash_value is None:
        logging.debug("Received None value for hash in add_to_not_wanted — skipping")
        return
    _add_to_store(NOT_WANTED_MAGNETS_FILE, hash_value)

def get_base_filename(url):
    """Extract the base filename from a URL or magnet link."""
    if url is None:
        logging.debug("Received None value for URL/magnet in get_base_filename — skipping")
        return None

    if url.startswith('magnet:'):
        import re
        # Hex hash (40 chars, SHA1)
        btih_match = re.search(r'btih:([a-fA-F0-9]{40})(?:[&?]|$)', url, re.IGNORECASE)
        if btih_match:
            return btih_match.group(1).lower()
        # Base32 hash (32 chars, also valid btih encoding) — decode to hex for uniform comparison
        b32_match = re.search(r'btih:([A-Z2-7]{32})(?:[&?]|$)', url, re.IGNORECASE)
        if b32_match:
            try:
                import base64
                raw = base64.b32decode(b32_match.group(1).upper())
                return raw.hex().lower()
            except Exception:
                return b32_match.group(1).lower()
    
    # For URLs with file parameter
    if 'file=' in url:
        return url.split('file=')[-1].split('&')[0]
    
    # For direct URLs
    return url.split('/')[-1]

def is_magnet_not_wanted(magnet):
    if get_setting('Debug','disable_not_wanted_check', False):
        logging.debug(f"Not wanted check is disabled, allowing magnet: {magnet[:60] if magnet else 'None'}...")
        return False
        
    if magnet is None:
        logging.debug("Received None value for magnet in is_magnet_not_wanted — skipping check")
        return False
        
    # Extract hash from magnet link
    magnet_hash = get_base_filename(magnet)
    if magnet_hash is None:
        return False

    # Check if the hash exists in not_wanted
    is_not_wanted = magnet_hash in _load_normalised_store(NOT_WANTED_MAGNETS_FILE)
    if is_not_wanted:
        logging.info(f"Filtering out magnet {magnet[:60]}... as it is in not_wanted_magnets list")
    return is_not_wanted

def get_not_wanted_magnets():
    return load_not_wanted_magnets()

def get_not_wanted_urls():
    return load_not_wanted_urls()

def add_to_not_wanted_urls(url, item_identifier=None, item=None):
    if url is None:
        logging.debug("Received None value for url in add_to_not_wanted_urls — skipping")
        return
    _add_to_store(NOT_WANTED_URLS_FILE, url)

def is_url_not_wanted(url):
    if get_setting('Debug','disable_not_wanted_check', False):
        logging.debug(f"Not wanted check is disabled, allowing URL: {url}")
        return False
    # Get base filename of the URL
    url_filename = get_base_filename(url)

    # Check if the filename exists in not_wanted
    is_not_wanted = url_filename in _load_normalised_store(NOT_WANTED_URLS_FILE)
    if is_not_wanted:
        logging.info(f"Filtering out URL {url} as it is in not_wanted_urls list")
    return is_not_wanted

def load_not_wanted_urls():
    return _load_store(NOT_WANTED_URLS_FILE)

def save_not_wanted_urls(not_wanted_set):
    _save_store(NOT_WANTED_URLS_FILE, not_wanted_set)

def purge_not_wanted_magnets_file():
    # Purge the contents of the file by overwriting it with an empty set
    _save_store(NOT_WANTED_MAGNETS_FILE, set())
    logging.warning("The 'not_wanted_magnets.pkl' file has been purged.")
    print("The 'not_wanted_magnets.pkl' file has been purged.")

def validate_not_wanted_entries():
    """Validate the not wanted magnets and URLs files on boot."""
    logging.info("Validating not wanted entries...")
    
    # Validate magnets
    magnets = load_not_wanted_magnets()
    if magnets:
        logging.info(f"Found {len(magnets)} not wanted magnets")
        logging.info("First 5 magnet entries:")
        for i, magnet in enumerate(list(magnets)[:5]):
            if magnet is None:
                logging.error(f"Entry {i} is None - will be removed")
                continue
            logging.info(f"  {i+1}. {magnet[:60]}...")

    # Save cleaned magnets if any None values were removed
    if None in magnets:
        magnets.discard(None)
        save_not_wanted_magnets(magnets)
        logging.info("Cleaned up not wanted magnets list by removing None values")

if __name__ == '__main__':
    validate_not_wanted_entries()