#include "redis_client.h"
#include <iostream>

RedisClient::RedisClient(const string& host, int port) : host(host), port(port), context(nullptr) {}

RedisClient::~RedisClient() {
    if (context) redisFree(context);
}

bool RedisClient::connect() {
    context = redisConnect(host.c_str(), port);
    if (context == nullptr || context->err) {
        if (context) cerr << "Redis 错误: " << context->errstr << endl;
        return false;
    }
    return true;
}

void RedisClient::fetchTopology(TopoGraph& graph) {
    graph.clear();
    redisReply *keys_reply = (redisReply *)redisCommand(context, "KEYS topo:link:*");
    if (keys_reply->type == REDIS_REPLY_ARRAY) {
        for (size_t i = 0; i < keys_reply->elements; i++) {
            string key = keys_reply->element[i]->str;
            redisReply *hget_reply = (redisReply *)redisCommand(context, "HMGET %s src dst delay utilization status", key.c_str());
            
            if (hget_reply->type == REDIS_REPLY_ARRAY && hget_reply->elements == 5 && hget_reply->element[0]->str != NULL) {
                string src = hget_reply->element[0]->str;
                string dst = hget_reply->element[1]->str;
                double delay = atof(hget_reply->element[2]->str);
                double util = atof(hget_reply->element[3]->str);
                bool is_active = (string(hget_reply->element[4]->str) == "UP");
                
                graph.addEdge(src, dst, delay, util, is_active);
            }
            freeReplyObject(hget_reply);
        }
    }
    freeReplyObject(keys_reply);
}

void RedisClient::pushSrPolicy(const string& queue_name, const string& src, const string& dst, const vector<string>& sid_list) {
    string sid_json = "[";
    for (size_t j = 0; j < sid_list.size(); ++j) {
        sid_json += "\"" + sid_list[j] + "\"";
        if (j < sid_list.size() - 1) sid_json += ",";
    }
    sid_json += "]";

    string policy_cmd = "{\"src\":\"" + src + "\", \"dst\":\"" + dst + "\", \"sids\":" + sid_json + "}";
    redisReply *reply = (redisReply *)redisCommand(context, "RPUSH %s %s", queue_name.c_str(), policy_cmd.c_str());
    if (reply) freeReplyObject(reply);
}