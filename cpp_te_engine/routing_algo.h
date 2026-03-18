#pragma once
#include "topo_graph.h"
#include <vector>
#include <string>

using namespace std;

class RoutingEngine {
private:
    double congestion_threshold;
    int max_sid_depth;
    string sid_prefix;

public:
    RoutingEngine(double threshold, int max_depth, const string& prefix);
    
    // 返回计算好的 IPv6 SID 列表
    vector<string> computeCSPF(const TopoGraph& graph, const string& start, const string& end);
    
    // 检查全网并返回拥塞源节点 (简单示例)
    vector<pair<string, string>> detectCongestion(const TopoGraph& graph);
};