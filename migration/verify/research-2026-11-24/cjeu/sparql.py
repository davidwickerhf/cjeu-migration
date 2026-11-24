import json, urllib.request, urllib.parse, collections
rows=json.load(open('uuid_rows.json'))
uris=sorted({x['u'] for x in rows})
out={}
for k in range(0,len(uris),100):
    vals=" ".join(f"<{u}>" for u in uris[k:k+100])
    q=f"""PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?w ?celex ?ecli ?type WHERE {{ VALUES ?w {{ {vals} }}
 OPTIONAL {{ ?w cdm:resource_legal_id_celex ?celex }} OPTIONAL {{ ?w cdm:case-law_ecli ?ecli }} }}"""
    import subprocess
    p=subprocess.run(['curl','-s','--max-time','180','--data-urlencode','query@-','-H','Accept: application/sparql-results+json','https://publications.europa.eu/webapi/rdf/sparql'],input=q.encode(),capture_output=True)
    try: res=json.loads(p.stdout)
    except Exception: print('fail',k,p.stdout[:200]); continue
    for b in res['results']['bindings']:
        d=out.setdefault(b['w']['value'],{'celex':set(),'ecli':set(),'type':set()})
        for f in ('celex','ecli','type'):
            if f in b: d[f].add(b[f]['value'])
json.dump({k:{f:sorted(v) for f,v in d.items()} for k,d in out.items()}, open('uuid_map.json','w'))
print('uris', len(uris), 'returned', len(out), 'with celex', sum(1 for d in out.values() if d['celex']), 'with ecli', sum(1 for d in out.values() if d['ecli']))
print(collections.Counter(t.split('#')[1] for d in out.values() for t in d['type']).most_common(12))
