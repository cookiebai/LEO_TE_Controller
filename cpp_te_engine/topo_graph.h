#pragma once
#include <string>
#include <vector>
#include <unordered_map>

using namespace std;

struct Edge {
    string dst;
    double delay;
    double utilization;
    bool is_active;
};

class TopoGraph {
public:
    unordered_map<string, vector<Edge>> adjList;

    void addEdge(const string& src, const string& dst, double delay, double util, bool is_active = true);
    void clear();
    int getNodeCount() const;
};