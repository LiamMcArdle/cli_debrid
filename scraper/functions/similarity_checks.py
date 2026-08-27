import html
import logging
import re
from typing import Dict, Any, List, Tuple
from difflib import SequenceMatcher
from fuzzywuzzy import fuzz
from PTT import parse_title
import unicodedata
from scraper.functions import *
from scraper.functions.ptt_parser import parse_with_ptt
from functools import lru_cache

# Pre-compiled regex patterns for better performance
_SHIELD_PATTERN = re.compile(r'S\.H\.I\.E\.L\.D\.?|S\s+H\s+I\s+E\s+L\s+D', re.IGNORECASE)
_SWAT_PATTERN = re.compile(r'S\.W\.A\.T\.?|S\s+W\s+A\s+T', re.IGNORECASE)
_PUNCTUATION_PATTERN = re.compile(r"[':()\[\]{}]")
_SPACE_PATTERN = re.compile(r'[\s_]+')
_MULTI_PERIOD_PATTERN = re.compile(r'\.+')
_BARE_039S_PATTERN = re.compile(r'\b039\s+s\b')
_BARE_039_PATTERN = re.compile(r'\s+039\s+')

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def improved_title_similarity(query_title: str, result: Dict[str, Any], is_anime: bool = False, content_type: str = None) -> float:
    # Normalize titles
    query_title = normalize_title(query_title).replace('&', 'and').replace('-','.')
    
    parsed_info = result.get('parsed_info', {})
    result_title = result.get('title', '')
    result_original_title = result.get('original_title', result_title)
    
    logging.debug(f"Title similarity calculation:")
    logging.debug(f"  - Query title: '{query_title}'")
    logging.debug(f"  - Original result title: '{result_original_title}'")
    
    # Use PTT to parse the result title if not already parsed
    if not parsed_info.get('title'):
        logging.debug(f"Parsing title with PTT: '{result_title}'")
        ptt_result = parse_with_ptt(result_title)
        logging.debug(f"PTT parsed result: {ptt_result}")
        ptt_title = ptt_result.get('title', '')
    else:
        ptt_title = parsed_info.get('title', result_title)
        logging.debug(f"Using already parsed title: '{ptt_title}'")
    
    ptt_title = normalize_title(ptt_title).replace('&', 'and').replace('-','.')
    
    logging.debug(f"  - Normalized query: '{query_title}'")
    logging.debug(f"  - Normalized PTT title: '{ptt_title}'")

    if is_anime:
        # For anime, use match_any_title function
        official_titles = [query_title]
        
        # Add alternative titles
        alternative_titles = parsed_info.get('alternative_title', [])
        if isinstance(alternative_titles, str):
            alternative_titles = [alternative_titles]
        official_titles.extend(alternative_titles)
        
        # Normalize alternative titles
        official_titles = [normalize_title(title).replace('&', 'and') for title in official_titles]
        
        similarity = match_any_title(ptt_title, official_titles)
        
        logging.debug(f"Anime title similarity: {similarity}")

    else:
        # For non-anime, use the existing logic with improved word matching
        token_sort_similarity = fuzz.token_sort_ratio(query_title, ptt_title) / 100
        
        # Split into words and remove 's' from the end of words for comparison
        query_words = set(word.rstrip('s') for word in query_title.split())
        ptt_words = set(word.rstrip('s') for word in ptt_title.split())
        
        # Check if all base words (without 's') are present
        all_words_present = query_words.issubset(ptt_words) or ptt_words.issubset(query_words)

        # If token sort similarity is very high (>0.95), don't penalize as heavily
        if token_sort_similarity > 0.95:
            similarity = token_sort_similarity
        else:
            similarity = token_sort_similarity * (0.75 if all_words_present else 0.5)

        logging.debug(f"Token sort ratio: {token_sort_similarity}")
        logging.debug(f"All base words present: {all_words_present}")

    logging.debug(f"Final similarity score: {similarity}")

    return similarity  # Already a float between 0 and 1

def preprocess_title(title):
    # Remove only non-resolution quality terms
    terms_to_remove = ['web-dl', 'webrip', 'bluray', 'dvdrip']
    for term in terms_to_remove:
        title = re.sub(r'\b' + re.escape(term) + r'\b', '', title, flags=re.IGNORECASE)
    # Remove any resulting double periods
    title = re.sub(r'\.{2,}', '.', title)
    # Remove any resulting double spaces
    title = re.sub(r'\s+', ' ', title)
    return title.strip()

@lru_cache(maxsize=1024)
def normalize_title(title: str) -> str:
    """
    Normalize the title by replacing spaces with periods, removing certain punctuation,
    standardizing the format, and removing non-English letters while keeping accented English letters and '&'.
    Uses caching to avoid re-processing the same title multiple times.
    """
    # Decode HTML entities (handles &#039; → ', &amp; → &, etc.)
    title = html.unescape(title)
    # Also handle bare numeric remnants from partially-stripped entities (e.g. " 039 s" from &#039;s)
    title = _BARE_039S_PATTERN.sub("'s", title)
    title = _BARE_039_PATTERN.sub("'", title)
    # Legacy explicit replacements as fallback
    if '&' in title:
        title = title.replace('&039;', "'").replace('&039s', "'s").replace('&#39;', "'")

    # Convert superscript and subscript unicode digits to normal digits to improve matching (e.g., '²' -> '2')
    supersub_digit_map = str.maketrans({
        '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4',
        '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
        '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4',
        '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9'
    })
    title = title.translate(supersub_digit_map)

    # Characters that stand in for a plain 'x' in stylised titles. NFKD does not
    # decompose these, and the ASCII filter below drops anything non-Latin, so
    # 'HUNTER×HUNTER' normalised to 'hunterhunter' and scored 42 against
    # 'Hunter x Hunter 1999 - 30' -- well under the 0.8 floor -- while every
    # release on every indexer spells it with an ASCII x. Measured 2026-08-26.
    title = title.translate(str.maketrans({
        '×': 'x',   # U+00D7 multiplication sign
        'ｘ': 'x',   # U+FF58 fullwidth latin small x
        'Ｘ': 'X',   # U+FF38 fullwidth latin capital X
        '✕': 'x', '✖': 'x', '⨯': 'x',
    }))
    
    # Handle percentage signs early if present
    if '%' in title:
        title = title.replace('1%', '1.percent').replace('1.%', '1.percent')
    
    # Replace slashes with spaces before normalization
    title = title.replace('/', ' ')
    
    # Normalize Unicode characters
    normalized = unicodedata.normalize('NFKD', title)
    
    # Handle common acronyms with pre-compiled patterns
    normalized = _SHIELD_PATTERN.sub('SHIELD', normalized)
    normalized = _SWAT_PATTERN.sub('SWAT', normalized)
    
    # Remove punctuation and convert spaces to periods in one pass
    normalized = _PUNCTUATION_PATTERN.sub('', normalized)
    normalized = _SPACE_PATTERN.sub('.', normalized)
    # Ensure single periods between words
    normalized = _MULTI_PERIOD_PATTERN.sub('.', normalized)
    # Add periods around standalone letters (like 'TS') that should be matched
    normalized = re.sub(r'(?<=[^.\w])(\w)(?=[^.\w])', r'.\1.', normalized)
    normalized = _MULTI_PERIOD_PATTERN.sub('.', normalized)  # Clean up any double periods from the previous step
    
    # Insert a period between letter-digit and digit-letter boundaries (e.g., 'accountant2' -> 'accountant.2')
    normalized = re.sub(r'(?<=\d)(?=[a-zA-Z])|(?<=[a-zA-Z])(?=\d)', '.', normalized)
    normalized = _MULTI_PERIOD_PATTERN.sub('.', normalized)  # Clean up if new double periods were created
    
    # Efficient character filtering using a single pass
    chars = []
    for c in normalized:
        if (c.isalnum() or 
            c in '.-&' or 
            (unicodedata.category(c).startswith('L') and ord(c) < 0x300)):
            # Only append ASCII chars and '&'
            if ord(c) < 128 or c == '&':
                chars.append(c)
    
    # Join characters and clean up
    normalized = ''.join(chars).strip('.').lower()
    
    return normalized


# Floor for the two gates that run OUTSIDE filter_results: the Upgrade Hub
# branch of the upgrading queue, which builds its candidate from a stored magnet
# and never calls filter_results at all, and MediaMatcher, which confirms a file
# on a season/episode coordinate without ever comparing titles.
#
# Calibrated 2026-08-26 against the wrong-show releases that actually reached the
# library. The worst of those scored 0.53 ('Rooster' against 'Dr. Stone' and its
# aliases), while every verified-legitimate alternate title scored 1.00 --
# 'Kage no Jitsuryokusha ni Naritakute' for 'The Eminence in Shadow', 'Sousou no
# Frieren' for 'Frieren', romaji and scene variants generally -- because the
# alias list carries the release's own name. The gap is wide; 0.60 sits in it.
#
# Deliberately well below the 0.80 / 0.60 thresholds filter_results applies.
# These gates answer "is this even the same show", not "is this release good
# enough". Raising this is the wrong repair for a wrong-show acceptance: a show
# whose romaji name is missing from its alias record scores ~0.30 against its
# English title and would become permanently, silently uncollectable.
MIN_TITLE_MATCH = 0.60


def title_is_asserted(candidate_title: str) -> bool:
    """Whether a candidate name says anything about WHICH show it belongs to.

    Fewer than four letters is a bare number or a tag ('03', 'v2', 'ep'), which
    asserts nothing -- files inside season packs are routinely named that way.
    ``title_verdict`` fails open on these, so any caller that RANKS by the score
    it returns must exclude them first, or an unnamed file scores a perfect 1.0
    and outranks the file that actually names the item.
    """
    return len(re.sub(r'[^a-z]', '', normalize_title(candidate_title or ''))) >= 4


def title_verdict(candidate_title: str, official_titles: List[str],
                  threshold: float = MIN_TITLE_MATCH) -> Tuple[bool, float, str]:
    """Does `candidate_title` name one of `official_titles`?

    Returns (matches, best_score, reason).

    Fails OPEN -- (True, 1.0, reason) -- whenever the candidate carries no usable
    title, or there is nothing comparable to judge it against. A missing name is
    not evidence of a wrong one, and a false rejection here is permanent: the
    item simply never fills, and the only symptom is a log line repeating
    forever.

    Deliberately does NOT reimplement the scoring in filter_results. That block
    blends parsed and full titles, applies length and extension penalties and
    consults the API alias pool, and remains the place where release-quality
    judgements are made. This is a much blunter question asked in two places
    that cannot reach filter_results at all.

    KNOWN MISS, measured and accepted: a candidate that is a superset of an
    official title is not decidable here. 'Monogatari Series Second Season' (the
    same show) and 'Saiunkoku Monogatari' (a different one) both score 0.85
    against 'Monogatari'; 'Bleach Sennen Kessen-hen' scores 0.40 against a
    'Bleach' alias list that lacks the arc name. No threshold on any fuzz metric
    separates those classes -- tightening enough to reject Saiunkoku also
    rejects Bleach, and a false rejection is permanent while a false acceptance
    is caught downstream. The mean of token_set and token_sort is used precisely
    because it fails open on supersets. Wrong-show releases that merely share a
    trailing noun are filter_results' job: its extension penalty scores the
    tokens a candidate adds, which is the information this function lacks.
    """
    normalized_candidate = normalize_title(candidate_title or '')
    if not title_is_asserted(candidate_title):
        return True, 1.0, 'no title asserted'

    names = [n for n in (normalize_title(t) for t in official_titles if t) if n]
    if not names:
        # Wholly non-Latin titles normalize to the empty string. Judging against
        # an empty set would reject every release for such a show.
        return True, 1.0, 'no comparable official title'

    best = max((fuzz.token_set_ratio(normalized_candidate, n)
                + fuzz.token_sort_ratio(normalized_candidate, n)) / 200.0
               for n in names)
    if best >= threshold:
        return True, best, 'matches an official title (%.2f)' % best
    return False, best, 'names a different show (best %.2f < %.2f)' % (best, threshold)
