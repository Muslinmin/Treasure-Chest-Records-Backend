"""Prefix clustering over the merchant-key corpus — §10.3.

Positional stripping (normalise.py) cannot know where a merchant name ends
when the bank appends a per-transaction reference — recurrence can.
``grab* a-98tuiknwx8qtav`` appears with a different 16-character suffix every
time; the cut point is observed, not assumed.

The guard that matters: cut only when a group of keys shares a common prefix
*and* the differing remainders all look like opaque per-transaction
references (a single token, no internal whitespace, containing a digit) —
not when the remainders are ordinary words of varying length. That is what
keeps ``google*google one`` / ``google*chatgpt`` / ``google*tinder dating``
from collapsing: the prefix ``google*`` matches, but ``chatgpt`` has no digit
and ``tinder dating`` contains a space, so neither looks like a reference.

Not MinHash/LSH. LSH exists to avoid O(n^2) candidate retrieval at scale;
~230 distinct keys is ~26k pairwise comparisons, microseconds in Python. Jaccard
over character n-grams would also score the two Grab examples far apart — the
random tail is longer than the merchant name and dominates the signature.
Revisit only past ~50k distinct keys.

Pure function, no I/O — same contract as normalise.py and identity.py.
Clustering is corpus-wide, so it cannot run per file inside process_file(); the
mapping it returns is a rebuildable cache, not authority.
"""

MIN_PREFIX_LEN = 4


def _looks_like_reference(remainder: str) -> bool:
    """A single whitespace-free token containing a digit — a per-transaction
    reference, not a real word. Real words either lack digits (``chatgpt``)
    or, when the remainder is multi-word (``tinder dating``), are excluded by
    the whitespace check outright.
    """
    remainder = remainder.strip()
    if not remainder or " " in remainder:
        return False
    return any(char.isdigit() for char in remainder)


def _common_prefix(a: str, b: str) -> str:
    length = min(len(a), len(b))
    for i in range(length):
        if a[i] != b[i]:
            return a[:i]
    return a[:length]


class _UnionFind:
    def __init__(self, items: list[str]):
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for item in self._parent:
            result.setdefault(self.find(item), []).append(item)
        return result


def cluster_keys(keys: list[str]) -> dict[str, str]:
    """Map every distinct merchant key to its clustered (canonical) key.

    Keys with no cluster match — including singletons and any key whose only
    prefix-sharing partners have word-shaped (not reference-shaped) remainders
    — map to themselves.
    """
    distinct = list(dict.fromkeys(keys))
    uf = _UnionFind(distinct)

    for i in range(len(distinct)):
        for j in range(i + 1, len(distinct)):
            a, b = distinct[i], distinct[j]
            prefix = _common_prefix(a, b)
            if len(prefix) < MIN_PREFIX_LEN:
                continue
            if _looks_like_reference(a[len(prefix):]) and _looks_like_reference(b[len(prefix):]):
                uf.union(a, b)

    mapping: dict[str, str] = {}
    for members in uf.groups().values():
        if len(members) == 1:
            mapping[members[0]] = members[0]
            continue
        canonical = members[0]
        for member in members[1:]:
            canonical = _common_prefix(canonical, member)
        canonical = canonical.rstrip()
        for member in members:
            mapping[member] = canonical or member

    return mapping
