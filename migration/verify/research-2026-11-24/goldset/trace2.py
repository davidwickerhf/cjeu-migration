import json,sys
sys.path.insert(0,"/Users/davidwickerhf/cjeu-sync-20260930")
from runner_q import query
S=sys.argv[1]; t=json.load(open(f"{S}/trace.json")); docs=t["docs"]
def q(sql,p): 
    r=query(sql,p); assert r.get("ok"),r; return r["rows"]
def cid(e): 
    r=q("SELECT id FROM cle_v2.cases WHERE ecli=%s",[e]); return r[0]["id"] if r else None
# Patterns per target, by form
P={
 "ECLI:EU:C:2019:189":{"ecli":["EU:C:2019:189"],"caseno":["C-221/17"],"name":["Tjebbes"],"date":["12 maart 2019","12 March 2019","12 mars 2019"]},
 "ECLI:EU:C:2013:720":{"ecli":["EU:C:2013:720"],"caseno":["C-199/12","C-200/12","C-201/12","C 199/12"],"name":["X, Y en Z","X, Y and Z","X, Y et Z","X e.a."],"date":["7 november 2013","7 November 2013","7 novembre 2013"]},
}
for k,d in docs.items():
    if d["lane"]=="echr" or k.startswith("ECLI:CE"):
        P[k]={"ecli":[k[5:]],"appno":[a.strip() for a in d["appno"].split(";")],"name":[d["name"].split(" v. ")[0]]}
    if k.startswith("ECLI:NL"):
        P[k]={"ecli":[k[5:], k.split(":")[-1]],"caseno":[c.strip() for c in d["caseno"].replace(" en ",",").split(",")][:3]}
def scan(src_ecli, tgt):
    i=cid(src_ecli); out={}
    for form,pats in P[tgt].items():
        for p in pats:
            if not p: continue
            r=q("SELECT count(*) c FROM cle_v2.case_text WHERE case_id=%s AND position(%s in translate(fulltext, '‐‑‒–—  ', '-----  '))>0",[i,p])
            if r[0]["c"]: out.setdefault(form,[]).append(p)
    return out
rows=[]
for l in t["links"]:
    a,b=l["from"],l["to"]
    rows.append({"group":l["group"],"cjeu":a,"other":b,"edges":l.get("edges"),"other_mentions_cjeu":scan(b,a),"cjeu_mentions_other":scan(a,b)})
# also AG opinion of each CJEU ruling citing the ECHR docs
for cj,cc in [("ECLI:EU:C:2019:189","62017CC0221"),("ECLI:EU:C:2013:720","62012CC0199")]:
    r=q("SELECT id, ecli FROM cle_v2.cases WHERE celex_id=%s",[cc])
    print("AG opinion",cc,r)
    if r:
        for l in t["links"]:
            if l["from"]==cj and l["to"].startswith("ECLI:CE"):
                hits=[]
                for form,pats in P[l["to"]].items():
                    for p in pats:
                        c=q("SELECT count(*) c FROM cle_v2.case_text WHERE case_id=%s AND position(%s in translate(fulltext, '‐‑‒–—  ', '-----  '))>0",[r[0]["id"],p])[0]["c"]
                        if c: hits.append(f"{form}:{p}")
                print("   AG",cc,"->",l["to"],hits)
json.dump(rows,open(f"{S}/trace2.json","w"),indent=1,ensure_ascii=False)
for r in rows: print(f'{r["group"][:24]:24} {r["other"]:36} edges={r["edges"]}\n     other->cjeu {r["other_mentions_cjeu"]}\n     cjeu->other {r["cjeu_mentions_other"]}')
