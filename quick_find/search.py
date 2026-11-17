"""Find features by attribute value across project layers.

No GUI code here, so it can be tested headless.

Searching is done in two passes so it stays quick:

1. ID and name fields (Site_ID, Name, ...) and numeric fields. Their values
   are read once per layer and kept in memory (SearchCache), so after the
   first search of a layer this pass is near-instant. The memory is dropped
   when the layer's data, filter or fields change.
2. Every other text field (descriptions, remarks, JSON), read from the layer
   each time. It runs only when pass 1 finds nothing, and by itself only when
   it is cheap; otherwise the caller can offer it (deep=True).

Layers with more than LARGE_LAYER features (e.g. a whole-country OSM roads
layer) are skipped by the caller unless the user asks for them; if they are
searched, they are read directly and never kept in memory.
"""
import re
from dataclasses import dataclass
from functools import partial

from qgis.PyQt.QtCore import QMetaType
from qgis.core import Qgis, QgsExpression, QgsFeatureRequest

try:
    from qgis.core import NULL
except ImportError:  # null attributes are plain None
    NULL = None
try:  # QGIS >= 3.36 and QGIS 4
    NO_GEOMETRY = Qgis.FeatureRequestFlag.NoGeometry
except AttributeError:
    NO_GEOMETRY = QgsFeatureRequest.NoGeometry

EXACT, WORD, PARTIAL = 0, 1, 2
PER_LAYER_LIMIT = 1000
MAX_MATCHES = 50
LARGE_LAYER = 200_000         # features; bigger layers are skipped unless asked for
AUTO_DEEP_CELLS = 2_000_000   # pass 2 runs by itself only below features x fields
SEP = "\x1f"                  # joins one feature's cached values

_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_KEY_TOKENS = {"id", "ids", "fid", "no", "num", "code", "key", "ref"}
_KEY_WORDS = ("name", "site", "label", "title", "code")


@dataclass
class Match:
    layer_id: str
    layer_name: str
    fid: int
    field: str
    value: str
    rank: int
    order: int


def _is_text(field):
    # QGIS 4 reports QMetaType types; QGIS 3 reports QVariant types with the same number
    return field.type() == QMetaType.Type.QString


def _is_null(value):
    return value is None or value == NULL


def is_key_field(name):
    """True for ID / name style fields: Site_ID, Name, SiteName, PLA ID."""
    low = name.lower()
    if "json" in low:
        return False
    tokens = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower())
    return any(t in _KEY_TOKENS for t in tokens) or any(w in low for w in _KEY_WORDS)


def is_large(layer):
    return layer.featureCount() > LARGE_LAYER


def rank_text(query, value):
    """EXACT, WORD or PARTIAL (case-insensitive), or None if no match.

    WORD means the query is not touching another letter or digit, so
    MIN164 is a WORD match in MIN_R3R3_ANL6_MIN164 but only PARTIAL in
    MIN1640.
    """
    q = query.strip().casefold()
    v = str(value).strip().casefold()
    if not q or q not in v:
        return None
    if v == q:
        return EXACT
    if re.search(r"(?<![^\W_])" + re.escape(q) + r"(?![^\W_])", v):
        return WORD
    return PARTIAL


def _rank_value(query, number, value, numeric):
    if numeric:
        try:
            return EXACT if number is not None and float(value) == float(number) else None
        except (TypeError, ValueError):
            return None
    return rank_text(query, value)


def _keep(found, layer, fid, best, order):
    key = (layer.id(), fid)
    if key not in found or best[0] < found[key].rank:
        found[key] = Match(layer.id(), layer.name(), fid, best[1], best[2], best[0], order)


class SearchCache:
    """ID/name and numeric values of each layer, read once, kept until the layer changes."""

    def __init__(self):
        self._layers = {}    # layer id -> (feature count, [(field name, is numeric)], [(fid, joined values)])
        self._watched = set()

    def clear(self, layer_id=None, *_):
        if layer_id is None:
            self._layers.clear()
        else:
            self._layers.pop(layer_id, None)

    def has(self, layer):
        entry = self._layers.get(layer.id())
        return entry is not None and entry[0] == layer.featureCount()

    def rows(self, layer):
        if not self.has(layer):  # never read, changed, or features added behind QGIS's back
            self._layers[layer.id()] = self._read(layer)
            if layer.id() not in self._watched:
                self._watched.add(layer.id())
                drop = partial(self.clear, layer.id())
                # unsaved edits and rollbacks -> layerModified; saving -> editingStopped;
                # reload or filter -> dataChanged; direct writes -> repaintRequested
                for signal in (layer.layerModified, layer.editingStopped, layer.dataChanged,
                               layer.subsetStringChanged, layer.updatedFields,
                               layer.repaintRequested):
                    signal.connect(drop)
        _, meta, rows = self._layers[layer.id()]
        return meta, rows

    @staticmethod
    def _read(layer):
        fields = layer.fields()
        indexes = [i for i, f in enumerate(fields)
                   if f.isNumeric() or (_is_text(f) and is_key_field(f.name()))]
        meta = [(fields.at(i).name(), fields.at(i).isNumeric()) for i in indexes]
        rows = []
        if indexes:
            request = QgsFeatureRequest().setFlags(NO_GEOMETRY).setSubsetOfAttributes(indexes)
            for feature in layer.getFeatures(request):
                values = ["" if _is_null(v) else str(v).replace(SEP, " ")
                          for v in (feature.attribute(i) for i in indexes)]
                joined = SEP.join(values)
                if joined.strip(SEP):
                    rows.append((feature.id(), joined))
        return layer.featureCount(), meta, rows


def _scan_cached(pairs, query, number, cache, found, on_read):
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    for order, layer in pairs:
        if on_read and not cache.has(layer):
            on_read(layer)
        meta, rows = cache.rows(layer)
        hits = 0
        for fid, joined in rows:
            if not pattern.search(joined):
                continue
            best = None
            for (name, numeric), value in zip(meta, joined.split(SEP)):
                if value:
                    rank = _rank_value(query, number, value, numeric)
                    if rank is not None and (best is None or rank < best[0]):
                        best = (rank, name, value)
            if best:
                _keep(found, layer, fid, best, order)
                hits += 1
                if hits >= PER_LAYER_LIMIT:
                    break


def _layer_request(layer, query, number, first_pass):
    parts, indexes = [], []
    pattern = QgsExpression.quotedString("%" + query + "%")
    for i, field in enumerate(layer.fields()):
        column = QgsExpression.quotedColumnRef(field.name())
        if _is_text(field) and is_key_field(field.name()) == first_pass:
            parts.append(f"{column} ILIKE {pattern}")
            indexes.append(i)
        elif first_pass and number is not None and field.isNumeric():
            parts.append(f"{column} = {number}")
            indexes.append(i)
    if not parts:
        return None, []
    request = QgsFeatureRequest().setFilterExpression(" OR ".join(parts))
    request.setFlags(NO_GEOMETRY)
    request.setSubsetOfAttributes(indexes)
    request.setLimit(PER_LAYER_LIMIT)
    return request, indexes


def _scan(pairs, query, number, first_pass, found):
    """Read matching features straight from the layers (no memory kept)."""
    for order, layer in pairs:
        request, indexes = _layer_request(layer, query, number, first_pass)
        if request is None:
            continue
        fields = layer.fields()
        for feature in layer.getFeatures(request):
            best = None
            for i in indexes:
                value = feature.attribute(i)
                if _is_null(value):
                    continue
                field = fields.at(i)
                rank = _rank_value(query, number, value, field.isNumeric())
                if rank is not None and (best is None or rank < best[0]):
                    best = (rank, field.name(), str(value))
            if best:
                _keep(found, layer, feature.id(), best, order)


def deep_cost(layers):
    """Rough size of pass 2: features x other text fields."""
    return sum(max(layer.featureCount(), 0) *
               sum(1 for f in layer.fields() if _is_text(f) and not is_key_field(f.name()))
               for layer in layers)


def search(layers, query, cache=None, deep=None, on_second_pass=None, on_read=None):
    """Return (matches sorted best first, deep_skipped).

    deep: None = run pass 2 only if pass 1 found nothing and it is cheap;
    True = always run it when pass 1 found nothing; False = never.
    deep_skipped is True when pass 2 was needed but not run because it is slow.
    Partial matches are dropped when any exact or whole-word match exists.
    on_second_pass() is called before pass 2; on_read(layer) before a layer
    is read into the cache for the first time.
    """
    query = query.strip()
    if not query:
        return [], False
    cache = cache if cache is not None else SearchCache()
    number = query if _NUMBER.match(query) else None
    pairs = list(enumerate(layers))
    found = {}
    _scan_cached([(o, l) for o, l in pairs if not is_large(l)], query, number, cache, found, on_read)
    _scan([(o, l) for o, l in pairs if is_large(l)], query, number, True, found)
    deep_skipped = False
    if not found and deep is not False:
        if deep or deep_cost(layers) <= AUTO_DEEP_CELLS:
            if on_second_pass:
                on_second_pass()
            _scan(pairs, query, number, False, found)
        else:
            deep_skipped = True
    matches = sorted(found.values(), key=lambda m: (m.rank, m.order, len(m.value)))
    if matches and matches[0].rank < PARTIAL:
        matches = [m for m in matches if m.rank < PARTIAL]
    return matches[:MAX_MATCHES], deep_skipped


def snippet(value, query, width=60):
    """One-line excerpt of value around the query, with HTML tags removed."""
    text = " ".join(re.sub(r"<[^>]+>", " ", str(value)).split())
    if len(text) <= width:
        return text
    pos = text.casefold().find(query.strip().casefold())
    start = max(0, pos - width // 3) if pos >= 0 else 0
    end = start + width
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
