import json, subprocess, time, sys
rows=json.load(open('uuid_rows.json'))
uris=sorted({x['u'] for x in rows})
N=int(sys.argv[1]); out={}; fails=0
for k in range(0,len(uris),N):
    vals=" ".join(f"<{u}>" for u in uris[k:k+N])
    q=f"PREFIX cdm: <http://publications.europa.eu/ontology/cdm#> SELECT ?w ?celex WHERE {{ VALUES ?w {{ {vals} }} ?w cdm:dossier_identifier ?celex }}"
    p=subprocess.run(['curl','-s','--max-time','120','--data-urlencode','query='+q,'-H','Accept: application/sparql-results+json','https://publications.europa.eu/webapi/rdf/sparql'],capture_output=True)
    try: res=json.loads(p.stdout)
    except Exception: fails+=1; time.sleep(2); continue
    for b in res['results']['bindings']: out.setdefault(b['w']['value'],[]).append(b['celex']['value'])
    time.sleep(0.3)
json.dump(out, open('uuid_dossier.json','w'))
print('uris',len(uris),'mapped',len(out),'fails',fails)
