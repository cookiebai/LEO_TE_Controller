// Static backup collector. Keeps task 1's queues JSON contract and uses tc JSON
// integer counters; works in the original nodes without Python or new packages.
package main

import (
    "context"
    "encoding/json"
    "fmt"
    "os"
    "os/exec"
    "regexp"
    "strings"
    "syscall"
    "time"
)

type object = map[string]interface{}

func commandJSON(args ...string) interface{} {
    ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
    defer cancel()
    data, err := exec.CommandContext(ctx, args[0], args[1:]...).Output()
    if err != nil { panic(fmt.Sprintf("%v: %v", args, err)) }
    var value interface{}
    if err := json.Unmarshal(data, &value); err != nil { panic(err) }
    return value
}
func number(value interface{}) (float64, bool) {
    n, ok := value.(float64)
    return n, ok
}
func option(value interface{}, key string) (float64, bool) {
    if obj, ok := value.(object); ok { value = obj[key] }
    return number(value)
}
func main() {
    defer func() { if err:=recover(); err!=nil {fmt.Fprintln(os.Stderr, err); os.Exit(1)} }()
    if len(os.Args)>1 && os.Args[1]!="--once" { panic("usage: leo-te-queue-probe --once") }
    path:=os.Getenv("QPROBE_CFG");if path=="" {path="/cfg.sh"}
    raw, err := os.ReadFile(path); if err!=nil {panic(err)}
    var cfg object
    if err=json.Unmarshal(raw,&cfg); err!=nil {panic(err)}
    node, ok := cfg["name"].(string); if !ok {panic("cfg.name missing")}
    ports, ok := cfg["ports"].(object); if !ok {panic("cfg.ports missing")}
    allQ := commandJSON("tc","-j","-s","qdisc","show").([]interface{})
    allL := commandJSON("ip","-j","link","show").([]interface{})
    byDev := map[string][]object{}
    carrier := map[string]string{}
    for _, item := range allQ {
        q:=item.(object); dev,_:=q["dev"].(string)
        byDev[dev]=append(byDev[dev],q)
    }
    for _, item := range allL {
        link:=item.(object); name,_:=link["ifname"].(string); carrier[name]="down"
        flags,_:=link["flags"].([]interface{})
        for _, flag:=range flags {if flag=="LOWER_UP" {carrier[name]="up"}}
    }
    names:=regexp.MustCompile(`L[0-9]{4}`)
    links:=[]object{}; measured:=0
    for _, rawPort:=range ports {
        port:=rawPort.(object); iface,_:=port["eth"].(string)
        ids,_:=port["linkid"].([]interface{})
        if len(ids)==0 || iface=="eth0" || iface=="lo" {continue}
        peers:=map[string]bool{}
        for _, id:=range ids {for _, name:=range names.FindAllString(fmt.Sprint(id),-1) {if name!=node {peers[name]=true}}}
        if len(peers)!=1 {panic("cannot identify peer for "+node+":"+iface)}
        peer:="";for name:=range peers {peer=name}
        row:=object{"src":node,"dst":peer,"interface":iface,"source":"leo-te-backup-tc-json",
            "carrier":carrier[iface],"measured":false,"queue_bytes":nil,"capacity_mbps":nil,"delay_ms":nil}
        roots:=[]object{}; rates:=[]float64{}; delay:=0.0; hasDelay:=false
        for _, q:=range byDev[iface] {
            if root,_:=q["root"].(bool);root {roots=append(roots,q)}
            opts,_:=q["options"].(object);if opts==nil {continue}
            rate,exists:=opts["rate64"];if !exists {rate=opts["rate"]}
            if n,ok:=option(rate,"rate");ok && n>0 {rates=append(rates,n*8/1e6)}
            if q["kind"]=="netem" {if n,ok:=option(opts["delay"],"delay");ok {delay+=n*1000;hasDelay=true}}
        }
        if len(roots)!=1 {row["reason"]="multiple-or-missing-root-qdiscs"} else {
            root:=roots[0];kind,_:=root["kind"].(string)
            backlog,backlogOK:=number(root["backlog"])
            if strings.Contains(" noqueue clsact ingress mq "," "+kind+" ") || !backlogOK {
                row["reason"]="not-measurable-kind-or-no-backlog"
            } else {
                row["measured"]=true;row["queue_bytes"]=backlog;measured++
                row["qlen_packets"]=root["qlen"];row["drops_total"]=root["drops"]
                row["overlimits_total"]=root["overlimits"]
                row["qdisc"]=fmt.Sprintf("%s %v Sent %vb/%vp",kind,root["handle"],root["bytes"],root["packets"])
            }
        }
        if len(rates)>0 {min:=rates[0];for _,n:=range rates {if n<min {min=n}};row["capacity_mbps"]=min}
        if hasDelay {row["delay_ms"]=delay}
        links=append(links,row)
    }
    if len(links)==0 {panic("no business ports in /cfg.sh")}
    stamp:=time.Now();host,_:=os.Hostname();var stat syscall.Stat_t
    if err=syscall.Stat("/proc/self/ns/net",&stat);err!=nil {panic(err)}
    out:=object{"node":node,"namespace":"container:"+host,"netns_inode":fmt.Sprint(stat.Ino),
        "timestamp":float64(stamp.UnixNano())/1e9,"sampled_at":stamp.Format(time.RFC3339Nano),
        "links":links,"links_total":len(links),"links_measured":measured,
        "units":object{"queue_bytes":"bytes","qlen_packets":"packets","drops_total":"cumulative packets",
                        "overlimits_total":"cumulative events","capacity_mbps":"Mbps","delay_ms":"ms"}}
    if err=json.NewEncoder(os.Stdout).Encode(out);err!=nil {panic(err)}
}
