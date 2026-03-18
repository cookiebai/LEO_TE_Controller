#include "topo_graph.h"

void TopoGraph::addEdge(const string& src, const string& dst, double delay, double util, bool is_active) {
    adjList[src].push_back({dst, delay, util, is_active});
}

void TopoGraph::clear() {
    adjList.clear();
}

int TopoGraph::getNodeCount() const {
    return adjList.size();
}