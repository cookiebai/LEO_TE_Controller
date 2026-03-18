#pragma once
#include <string>
#include <vector>
#include <hiredis/hiredis.h>
#include "topo_graph.h"

using namespace std;

class RedisClient {
private:
    redisContext* context;
    string host;
    int port;

public:
    RedisClient(const string& host, int port);
    ~RedisClient();
    bool connect();
    void fetchTopology(TopoGraph& graph);
    void pushSrPolicy(const string& queue_name, const string& src, const string& dst, const vector<string>& sid_list);
};