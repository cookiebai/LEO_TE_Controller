#include "routing_algo.h"
#include <queue>
#include <limits>
#include <iostream>

RoutingEngine::RoutingEngine(double threshold, int max_depth, const string& prefix) 
    : congestion_threshold(threshold), max_sid_depth(max_depth), sid_prefix(prefix) {}

vector<pair<string, string>> RoutingEngine::detectCongestion(const TopoGraph& graph) {
    vector<pair<string, string>> congested_flows;
    for (const auto& node : graph.adjList) {
        for (const auto& edge : node.second) {
            if (edge.utilization >= congestion_threshold || !edge.is_active) {
                congested_flows.push_back({node.first, edge.dst});
            }
        }
    }
    return congested_flows;
}

vector<string> RoutingEngine::computeCSPF(const TopoGraph& graph, const string& start, const string& end) {
    unordered_map<string, double> distances;
    unordered_map<string, string> previous;
    auto cmp = [](pair<double, string> left, pair<double, string> right) { return left.first > right.first; };
    priority_queue<pair<double, string>, vector<pair<double, string>>, decltype(cmp)> pq(cmp);

    for (const auto& pair : graph.adjList) {
        distances[pair.first] = numeric_limits<double>::infinity();
        for (const auto& edge : pair.second) {
            distances[edge.dst] = numeric_limits<double>::infinity();
        }
    }
    distances[start] = 0.0;
    pq.push({0.0, start});

    while (!pq.empty()) {
        string current = pq.top().second;
        double currentDist = pq.top().first;
        pq.pop();

        if (current == end) break;
        if (currentDist > distances[current]) continue;

        for (const auto& edge : graph.adjList.at(current)) {
            // 核心约束：避开拥塞链路和断掉的链路
            if (edge.utilization >= congestion_threshold || !edge.is_active) continue;

            double newDist = currentDist + edge.delay;
            if (newDist < distances[edge.dst]) {
                distances[edge.dst] = newDist;
                previous[edge.dst] = current;
                pq.push({newDist, edge.dst});
            }
        }
    }

    vector<string> sid_list;
    string curr = end;
    if (previous.find(curr) == previous.end() && start != end) {
        // 找不到路，返回空
        return sid_list; 
    }

    vector<string> temp_path;
    while (previous.find(curr) != previous.end()) {
        temp_path.push_back(curr);
        curr = previous[curr];
    }
    
    // 转换为 SID (例如 sat99 转换为 fd00:sat99::1)
    for (int i = temp_path.size() - 1; i >= 0; --i) {
        sid_list.push_back(sid_prefix + temp_path[i] + "::1");
        // 如果超过硬件最大深度，立刻截断
        if (sid_list.size() >= max_sid_depth) {
            cout << "[警告] SID 深度达到极限 " << max_sid_depth << endl;
            break;
        }
    }
    return sid_list;
}