import json,re,sys
sys.path.insert(0,"/Users/davidwickerhf/cjeu-sync-20260930")
from runner_q import query
S=sys.argv[1]
js=open(f"{S}/data.js").read(); d=json.loads(js[js.index("{"):js.rstrip().rstrip(";").rindex("}")+1])
docs={x["id"]:x for x in d["docs"]}
links=[l for k,v in d.items() if k!="docs" and isinstance(v,list) for l in v if isinstance(l,dict) and "group" in l]
def q(sql,p=None):
    r=query(sql,p or []); 
    if not r.get("ok"): raise SystemExit(json.dumps(r)[:500])
    return r["rows"]
ids=sorted(docs)
rows=q("SELECT id, ecli, celex_id, array_to_string(sources,',') src, date_decision FROM cle_v2.cases WHERE ecli = ANY(%s)",[ids])
case={r["ecli"]:r for r in rows}
out=[]
for l in links:
    a,b=l.get("from"),l.get("to")
    ca,cb=case.get(a),case.get(b)
    rec={"group":l["group"],"from":a,"to":b,"from_in_db":bool(ca),"to_in_db":bool(cb)}
    if ca and cb:
        e=q("SELECT source_case_id=%s AS fwd, relation_type, source_dataset, is_cross_jurisdiction FROM cle_v2.case_citation WHERE (source_case_id=%s AND target_case_id=%s) OR (source_case_id=%s AND target_case_id=%s)",[ca["id"],ca["id"],cb["id"],cb["id"],ca["id"]])
        rec["edges"]=[f'{"->" if x["fwd"] else "<-"} {x["relation_type"]}/{x["source_dataset"]}' for x in e]
        # raw unresolved mentions
        raw=q("SELECT relation_type, source_dataset, target_ecli_raw, target_celex_raw FROM cle_v2.case_citation WHERE source_case_id IN (%s,%s) AND target_case_id IS NULL AND (target_ecli_raw IN (%s,%s) OR target_celex_raw IN (%s,%s))",[ca["id"],cb["id"],a,b,cb["celex_id"] or "-",ca["celex_id"] or "-"])
        rec["unresolved_raw"]=[f'{x["relation_type"]}/{x["source_dataset"]}' for x in raw]
        # text: does source text mention target ids? check both directions
        for side,(src,tgt) in {"a_text":(ca,b),"b_text":(cb,a)}.items():
            t=docs[tgt]; pats=[t["ecli"]]
            if t.get("ecli","").startswith("ECLI:"): pats.append(t["ecli"][5:])
            if t.get("caseno") and t["lane"]=="eu": pats+= [t["caseno"]]
            if t.get("appno"): pats+= [x.strip() for x in re.split(r"[;,]",t["appno"]) if x.strip()]
            if t.get("celex"): pats.append(t["celex"])
            for al in t.get("aliases",[]): pats.append(al)
            tx=q("SELECT source, length(fulltext) n FROM cle_v2.case_text WHERE case_id=%s",[src["id"]])
            hits={}
            for p in pats:
                if not p: continue
                h=q("SELECT count(*) c FROM cle_v2.case_text WHERE case_id=%s AND position(%s in regexp_replace(fulltext, '[‐‑‒–— ]', '-', 'g'))>0",[src["id"],p.replace("‑","-")])
                hits[p]=h[0]["c"]
            rec[side]={"texts":[f'{x["source"]}:{x["n"]}' for x in tx],"mentions":{k:v for k,v in hits.items() if v}}
    out.append(rec)
json.dump({"docs":docs,"links":out},open(f"{S}/trace.json","w"),indent=1,ensure_ascii=False)
for r in out:
    print(f'{r["group"][:28]:28} {r["from"]:40} -> {r["to"]:36} db={int(r["from_in_db"])}{int(r["to_in_db"])} edges={r.get("edges")} raw={r.get("unresolved_raw")}')
    print(f'    from-text {r.get("a_text")}\n    to-text   {r.get("b_text")}')
