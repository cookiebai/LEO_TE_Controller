#include <iostream>
#include <unistd.h>
#include <yaml-cpp/yaml.h>
#include "redis_client.h"
#include "topo_graph.h"
#include "routing_algo.h"

using namespace std;

int main() {
    cout << "读取配置文件..." << endl;
    YAML::Node config;
    try {
        config = YAML::LoadFile("../config/controller.yaml");
    } catch (const YAML::BadFile& e) {
        cerr << "无法读取 controller.yaml, 请检查路径!" << endl;
        return 1;
    }

    // 初始化参数
    string redis_host = config["redis"]["host"].as<string>();
    int redis_port = config["redis"]["port"].as<int>();
    string queue_name = config["redis"]["te_queue_name"].as<string>();
    
    int polling_ms = config["te_engine"]["polling_interval_ms"].as<int>();
    double threshold = config["te_engine"]["congestion_threshold_percent"].as<double>();
    int max_sid = config["te_engine"]["max_sid_depth"].as<int>();
    string sid_prefix = config["network"]["srv6_locator_prefix"].as<string>();

    RedisClient redis(redis_host, redis_port);
    if (!redis.connect()) return 1;

    RoutingEngine router(threshold, max_sid, sid_prefix);
    TopoGraph graph;

    cout << "课题四: 集中式 TE 核心引擎初始化完成。开始监听拓扑..." << endl;

    while (true) {
        // 1. 获取最新拓扑
        redis.fetchTopology(graph);
        
        if (graph.getNodeCount() > 0) {
            // 2. 检测拥塞
            auto congested_links = router.detectCongestion(graph);
            
            if (!congested_links.empty()) {
                // 假设我们对受影响的节点进行重路由 (实际工程中，这里需要查找受影响的业务流)
                // 这里以第一条拥塞链路的源节点作为需要引流的起点，假设终点为 sat_core_1
                string src = congested_links[0].first;
                string dst = "sat_core_1"; // 示例终点
                
                cout << "[事件] 发现拥塞，触发重路由: " << src << " -> " << dst << endl;
                
                // 3. 计算新路径
                vector<string> new_sid_list = router.computeCSPF(graph, src, dst);
                
                if (!new_sid_list.empty()) {
                    // 4. 下发策略
                    redis.pushSrPolicy(queue_name, src, dst, new_sid_list);
                    cout << "[执行] 已推送 SRv6 策略至下发队列" << endl;
                } else {
                    cout << "[警告] 无法找到满足约束的备用路径" << endl;
                }
            }
        }
        
        usleep(polling_ms * 1000); // 毫秒转微秒
    }

    return 0;
}