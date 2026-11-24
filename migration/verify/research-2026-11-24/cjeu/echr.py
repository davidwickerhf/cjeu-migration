import sys, json, collections
sys.path.insert(0, "/Users/davidwickerhf/cjeu-sync-20260930")
from runner_q import query
r=query("""with s as materialized (select t.case_id id, t.document_celex celex_id, t.fulltext from cle_v2.case_text t tablesample system (6) repeatable (42) where t.source='CELLAR_ITEM' and t.language='en' and t.fulltext is not null)
select s.id, s.celex_id, (s.fulltext ~ 'ECtHR|European Court of Human Rights|Eur\\. Court H') mention,
  (select string_agg(coalesce(m[1],'')||m[2], ' ') from regexp_matches(s.fulltext, 'CE:ECHR:(\\d{4}:)?(\\d{4}JUD\\d{9})','g') m) refs
from s where s.fulltext ~ 'ECtHR|European Court of Human Rights|Eur\\. Court H|CE:ECHR'""")
assert r.get('ok'), str(r)[:400]
rows=r['rows']; print('trunc', r.get('truncated'))
n=query("select count(*) n from cle_v2.case_text t tablesample system (6) repeatable (42) where t.source='CELLAR_ITEM' and t.language='en' and t.fulltext is not null")['rows'][0]['n']
print('sample texts', n, 'mention/CE:ECHR texts', len(rows))
refs=[(x['id'],x['celex_id'],tok) for x in rows if x['refs'] for tok in set(x['refs'].split())]
print('texts with CE:ECHR id', sum(1 for x in rows if x['refs']), 'distinct (text,ref)', len(refs), 'no-year', sum(1 for _,_,t in refs if t[4]!=':'))
print('mention-only texts (no CE:ECHR id)', sum(1 for x in rows if not x['refs'] and x['mention']))
pass
full=sorted({ 'ECLI:CE:ECHR:'+t for _,_,t in refs if t[4]==':'})
noyr=sorted({t for _,_,t in refs if t[4]!=':'})
vals=",".join(f"('{e}')" for e in full)
q=query(f"select v.e, c.id from (values {vals}) v(e) left join cle_v2.cases c on c.ecli=v.e")
hit={x['e'] for x in q['rows'] if x['id']}
print('distinct ECLIs with year', len(full), 'resolve', len(hit))
print('unresolved examples', [e for e in full if e not in hit][:10])
# no-year: match by suffix via item? try date part + appno
json.dump({'refs':refs,'full':full,'noyr':noyr,'hit':sorted(hit)}, open('echr.json','w'))
