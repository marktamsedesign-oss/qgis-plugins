"""Shortest path along a road layer between two points.

The road graph is built once for an area around the leg and cached, so
further clicks inside that area only run Dijkstra (well under a second).
Clicks join the nearest point on a road (not just the nearest vertex);
if that road is an isolated fragment, a nearby connected road is used.
The road layer is only read, never changed.
"""
from qgis.analysis import (QgsGraphAnalyzer, QgsGraphBuilder,
                           QgsNetworkDistanceStrategy, QgsVectorLayerDirector)
from qgis.core import (Qgis, QgsDistanceArea, QgsFeatureRequest, QgsPointXY,
                       QgsProject, QgsRectangle, QgsSpatialIndex)

try:  # QGIS >= 3.36 and QGIS 4
    NO_GEOMETRY = Qgis.FeatureRequestFlag.NoGeometry
except AttributeError:
    NO_GEOMETRY = QgsFeatureRequest.NoGeometry

MAX_ROADS = 50000       # bigger graphs take more than ~5 s to build
MIN_MARGIN_DEG = 0.02   # about 2 km around a leg in a lat/lon layer
MIN_MARGIN_M = 2000
SNAP_CANDIDATES = 3
SNAP_SLACK_M = 150  # consider roads up to this much farther than the nearest


class RouteError(Exception):
    pass


def _dedupe(points):
    out = []
    for point in points:
        if not out or point != out[-1]:
            out.append(point)
    return out


class RoadRouter:
    def __init__(self):
        self.clear()

    def clear(self):
        self._layer_id = None
        self._area = None
        self._graph = None
        self._index = None
        self._geometries = {}
        self._distance = None

    def route(self, layer, a, b):
        """Points from a to b along the roads of layer (all in layer CRS)."""
        self._ensure_graph(layer, a, b)
        near_a, near_b = self._candidates(a), self._candidates(b)
        # the roads nearest the clicks first; farther ones only if those do not connect
        pairs = sorted(((i, j) for i in range(len(near_a)) for j in range(len(near_b))),
                       key=lambda ij: (ij[0] + ij[1], ij[0]))
        trees = {}
        for i, j in pairs:
            path = self._join(near_a[i], near_b[j], trees)
            if path:
                return _dedupe([a] + path + [b])
        raise RouteError("no road connection between these points")

    def _join(self, at_a, at_b, trees):
        """Shortest road path between two points on road segments, or None."""
        measure = self._distance.measureLine
        _, pa, segment_a, ends_a = at_a
        _, pb, segment_b, ends_b = at_b
        if segment_a == segment_b:
            return [pa, pb]
        best = None
        for start_point in ends_a:
            start = self._graph.findVertex(start_point)
            if start < 0:
                continue
            if start not in trees:
                trees[start] = QgsGraphAnalyzer.dijkstra(self._graph, start, 0)
            tree, cost = trees[start]
            for end_point in ends_b:
                end = self._graph.findVertex(end_point)
                if end < 0 or (end != start and tree[end] == -1):
                    continue
                total = measure(pa, start_point) + cost[end] + measure(end_point, pb)
                if best is None or total < best[0]:
                    best = (total, start, end, tree)
        if best is None:
            return None
        _, start, end, tree = best
        return [pa] + self._walk(tree, start, end) + [pb]

    def _ensure_graph(self, layer, a, b):
        need = QgsRectangle(a, b)
        margin = max(max(need.width(), need.height()) * 0.6,
                     MIN_MARGIN_DEG if layer.crs().isGeographic() else MIN_MARGIN_M)
        need.grow(margin)
        if self._layer_id == layer.id() and self._area is not None and self._area.contains(need):
            return
        area = QgsRectangle(need)
        area.grow(margin * 0.5)  # room for the next few clicks

        count_request = (QgsFeatureRequest().setFilterRect(area).setNoAttributes()
                         .setFlags(NO_GEOMETRY).setLimit(MAX_ROADS + 1))
        if sum(1 for _ in layer.getFeatures(count_request)) > MAX_ROADS:
            raise RouteError("this leg is too long to route in one step (more than "
                             f"{MAX_ROADS:,} roads around it); click a point in between")
        roads = layer.materialize(QgsFeatureRequest().setFilterRect(area).setNoAttributes())
        if roads.featureCount() == 0:
            raise RouteError("no roads near these points")

        director = QgsVectorLayerDirector(roads, -1, "", "", "",
                                          QgsVectorLayerDirector.Direction.DirectionBoth)
        director.addStrategy(QgsNetworkDistanceStrategy())
        builder = QgsGraphBuilder(roads.crs(), True, 0.0, "WGS84")
        director.makeGraph(builder, [])
        self._graph = builder.graph()
        self._geometries = {f.id(): f.geometry() for f in roads.getFeatures()}
        self._index = QgsSpatialIndex(roads.getFeatures())
        self._distance = QgsDistanceArea()
        self._distance.setSourceCrs(roads.crs(), QgsProject.instance().transformContext())
        self._distance.setEllipsoid("WGS84")
        self._layer_id, self._area = layer.id(), area

    def _candidates(self, point):
        """Nearby road segments to join at: the nearest, plus up to two more
        within SNAP_SLACK_M of it, so an isolated scrap of road next to the
        click does not block the route. Items: (gap metres, point on road,
        segment key, segment end points)."""
        found = []
        for fid in self._index.nearestNeighbor(point, 8):
            geometry = self._geometries[fid]
            _, closest, after, _ = geometry.closestSegmentWithContext(point)
            if after > 0:
                closest = QgsPointXY(closest)
                ends = (QgsPointXY(geometry.vertexAt(after - 1)), QgsPointXY(geometry.vertexAt(after)))
                found.append((self._distance.measureLine(point, closest), closest, (fid, after), ends))
        if not found:
            raise RouteError("no road near the clicked point")
        found.sort(key=lambda item: item[0])
        return [item for item in found if item[0] <= found[0][0] + SNAP_SLACK_M][:SNAP_CANDIDATES]

    def _walk(self, tree, start, end):
        graph = self._graph
        points = [graph.vertex(end).point()]
        while end != start:
            end = graph.edge(tree[end]).fromVertex()
            points.append(graph.vertex(end).point())
        points.reverse()
        return points
