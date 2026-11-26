import json,re,collections,random
pairs=json.load(open('allpairs.json')); lead={(a,b):c for a,b,c in json.load(open('lead.json'))}
res={}
for l in open('allres_nums.txt').read().splitlines()[1:]:
    a,b=[x.strip() for x in l.split('|')]; res[a]=[x.split('=') for x in b.split()]
eres={}
for l in open('allres_eclis.txt').read().splitlines()[1:]:
    a,b,c=[x.strip() for x in l.split('|')]; eres[a]=(b,c)
def pick(n):
    c=res.get(n)
    if not c: return None,None
    for pat in (r'6\d{4}(CJ|TJ|FJ)',r'6\d{4}(CO|TO|FO)',r'6\d{4}(CC|CV|CP)'):
        m=[x for x in c if re.match(pat,x[0])]
        if m: return m[0][1], m[0][0]
    return None,None
edges=set(); stats=collections.Counter(); per_src=collections.Counter(); tgt_kind=collections.Counter()
for src,items in pairs.items():
    for k,v,_ in items:
        if k=='num':
            stats['num_pairs']+=1
            t,cx=pick(v)
            if t: stats['num_resolved']+=1; edges.add((src,t,'num'))
            elif (src,v) in lead and pick(lead[(src,v)])[0]: stats['num_via_lead']+=1; edges.add((src,pick(lead[(src,v)])[0],'lead'))
            else: stats['num_unresolved']+=1; stats['unres_T' if v[0]=='T' else 'unres_C']+=1
        else:
            stats['ecli_pairs']+=1
            if v in eres: edges.add((src,eres[v][0],'ecli')); stats['ecli_resolved']+=1
de=set((s,t) for s,t,_ in edges)
print(stats); print('distinct resolved edges',len(de),'sources',len({s for s,_ in de}),'targets',len({t for _,t in de}))
for s,t in de: per_src[s]+=1
v=sorted(per_src.values()); print('outdeg median',v[len(v)//2],'p90',v[int(len(v)*.9)],'max',v[-1])
json.dump(sorted(de),open('edges.json','w'))
