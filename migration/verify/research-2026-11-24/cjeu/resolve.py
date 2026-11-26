import sys, json, re, collections
sys.path.insert(0, "/Users/davidwickerhf/cjeu-sync-20260930")
from runner_q import query
E=json.load(open('entries.json')); M=json.load(open('matches.json'))
def ctype(c):
    c=c.lower()
    for k,v in [('centrale raad','Centrale Raad van Beroep'),('college van beroep','College van Beroep voor het bedrijfsleven'),
                ('raad van state','Raad van State'),('afdeling bestuursrechtspraak','Raad van State'),('hoge raad','Hoge Raad'),
                ('tariefcommissie','Tariefcommissie'),('gerechtshof','Gerechtshof'),('hof ','Gerechtshof'),('rechtbank','Rechtbank'),('kantongerecht','Rechtbank'),('voorzieningenrechter','Rechtbank'),('president','Rechtbank')]:
        if k in c: return v
    return None
PLACES=["amsterdam","'s-gravenhage","den haag","rotterdam","utrecht","arnhem","leeuwarden","'s-hertogenbosch","den bosch","haarlem","zwolle","breda","maastricht","almelo","alkmaar","assen","dordrecht","groningen","middelburg","roermond","zutphen","noord-holland","noord-nederland","oost-brabant","zeeland-west-brabant","limburg","gelderland","overijssel","midden-nederland","oost-nederland","arnhem-leeuwarden"]
def compat(court, inst):
    t=ctype(court)
    if not t: return True
    il=inst.lower()
    if t=='Hoge Raad': return il=='hoge raad'
    if t in ('Gerechtshof','Rechtbank'):
        if t.lower() not in il and not (t=='Rechtbank' and 'kantongerecht' in il): return False
        cl=court.lower().replace('den haag',"'s-gravenhage").replace('den bosch',"'s-hertogenbosch")
        ps=[p for p in PLACES if p in cl]
        return not ps or any(p in il.replace('den haag',"'s-gravenhage") for p in ps)
    return t.lower() in il
by=collections.defaultdict(set)
for m in M:
    if compat(E[m['i']]['court'], m['instance']): by[m['i']].add(m['ecli'])
uniq={i:next(iter(v)) for i,v in by.items() if len(v)==1}
print('token+court matched', len(by), 'unique', len(uniq))
# fallback: date + court type, unique candidate
un=[i for i in range(len(E)) if i not in by and ctype(E[i]['court'])]
res=[]
for k in range(0,len(un),300):
    ch=un[k:k+300]
    vals=",".join(f"({i},date '{E[i]['date']}','{ctype(E[i]['court'])}')" for i in ch)
    q=query(f"""select v.i, c.ecli, r.instance, r.zaaknummer from (values {vals}) v(i,d,t) join cle_v2.rs_document r on r.date_decision=v.d
      join cle_v2.cases c on c.id=r.case_id where position(lower(v.t) in lower(r.instance))>0 and r.instance <> 'Parket bij de Hoge Raad'""")
    assert q.get('ok'), str(q)[:300]; res+=q['rows']
    if q.get('truncated'): print('TRUNC')
fb=collections.defaultdict(list)
for m in res:
    if compat(E[m['i']]['court'], m['instance']): fb[m['i']].append(m)
fbu={i:v[0] for i,v in fb.items() if len(v)==1}
print('unmatched with court type', len(un), 'fallback has cands', len(fb), 'unique', len(fbu))
json.dump({'uniq':uniq,'fbu':{i:v for i,v in fbu.items()}, 'amb':{i:list(v) for i,v in by.items() if len(v)>1}}, open('resolved.json','w'))
c=collections.Counter()
for i,e in enumerate(E):
    st='token' if i in uniq else 'fallback' if i in fbu else 'amb' if i in by else 'none'
    c[(e['tag'][0],st)]+=1
print(sorted(c.items()))
print('distinct CJEU cases w/ resolved A:', len({E[i]['ecli'] for i in uniq if E[i]['tag'][0]=='A'}), 'P:', len({E[i]['ecli'] for i in uniq if E[i]['tag'][0]=='P'}))
