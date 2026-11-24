import sys, json
sys.path.insert(0, "/Users/davidwickerhf/cjeu-sync-20260930")
from runner_q import query
rows=[]; last=0
while True:
    r=query(f"""select d.id, d.celex_id, c.ecli, d.national_judgement_xml x from cle_v2.cjeu_document d join cle_v2.cases c on c.id=d.case_id
      where d.national_judgement_xml ~* '(raad van state|hoge raad|centrale raad|college van beroep|gerechtshof|rechtbank|tariefcommissie|kantongerecht|arrondissementsrechtbank|afdeling bestuursrechtspraak|voorzieningenrechter)'
      and d.id > {last} order by d.id limit 500""")
    assert r.get("ok"), r
    rows+=r["rows"]
    if len(r["rows"])<500: break
    last=r["rows"][-1]["id"]
json.dump(rows, open("nj_nl.json","w"))
print(len(rows))
