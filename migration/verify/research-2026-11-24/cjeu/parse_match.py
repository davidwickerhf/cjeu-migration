import sys, json, re, html, collections
sys.path.insert(0, "/Users/davidwickerhf/cjeu-sync-20260930")
from runner_q import query
rows=json.load(open('nj_nl.json'))
NL=re.compile(r'(raad van state|hoge raad|centrale raad|college van beroep|gerechtshof|rechtbank|tariefcommissie|kantongerecht|afdeling bestuursrechtspraak|voorzieningenrechter|president)', re.I)
BE=re.compile(r'(brussel|antwerpen|gent|li[eè]ge|cassatie|grondwettelijk|arbeidshof|arbeidsrechtbank|ondernemingsrechtbank|hof van beroep|belg|leuven|brugge|mechelen|hasselt|kortrijk|vredegerecht|rechtbank van eerste aanleg|politierechtbank|raad voor vreemdelingenbetwistingen)', re.I)
entries=[]
for r in rows:
    affaire=None
    for p in re.findall(r'<p>(.*?)</p>', r['x'], re.S):
        p=html.unescape(p).strip()
        m=re.match(r'\*\* AFFAIRE (.*?) \*\*', p)
        if m: affaire=m.group(1); continue
        m=re.match(r'\*([AIP]\d)\*\s*(.*)', p)
        if not m: continue
        tag, rest = m.groups()
        if not NL.search(rest) or BE.search(rest): continue
        dm=re.search(r'(\d{2})/(\d{2})/(\d{4})', rest)
        if not dm: continue
        date=f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        par=rest[dm.end():]
        pm=re.search(r'\((.*)\)', par)
        toks=[]
        if pm:
            for t in re.split(r';|,| et | en | and ', pm.group(1)):
                t=re.sub(r'\s','',t.lower())
                if len(t)>=4 and re.search(r'\d',t): toks.append(t)
        court=rest[:dm.start()]
        entries.append(dict(celex=r['celex_id'], ecli=r['ecli'], tag=tag, date=date, court=court.strip(' ,'), toks=toks, affaire=affaire))
print('entries', len(entries), collections.Counter(e['tag'][0] for e in entries), 'with toks', sum(1 for e in entries if e['toks']))
json.dump(entries, open('entries.json','w'))
# server-side match: date + token in zaaknummer
pairs=[(i,e['date'],t) for i,e in enumerate(entries) for t in e['toks']]
res=[]
B=300
for k in range(0,len(pairs),B):
    chunk=pairs[k:k+B]
    vals=",".join(f"({i},date '{d}','{t.replace(chr(39),chr(39)*2)}')" for i,d,t in chunk)
    sql=f"""select v.i, c.ecli, r.instance, r.zaaknummer from (values {vals}) v(i,d,t)
      join cle_v2.rs_document r on r.date_decision=v.d
      join cle_v2.cases c on c.id=r.case_id
      where position(v.t in regexp_replace(lower(coalesce(r.zaaknummer,'')),'\\s','','g'))>0"""
    q=query(sql); assert q.get('ok'), str(q)[:500]
    res+=q['rows']
    if q.get('truncated'): print('TRUNC', k)
json.dump(res, open('matches.json','w'))
by=collections.defaultdict(set)
for m in res: by[m['i']].add((m['ecli'],m['instance'],m['zaaknummer']))
print('matched entries', len(by), 'unique', sum(1 for v in by.values() if len(v)==1))
