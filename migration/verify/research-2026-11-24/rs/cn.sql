with s as (select case_id, fulltext t from cle_v2.case_text tablesample system(1) repeatable(7) where source='RECHTSPRAAK' and fulltext is not null),
m as (select s.case_id, upper(x[1])||'-'||ltrim(x[2],'0')||'/'||x[3] cn from s, regexp_matches(s.t, '\m([CTF]) ?[-‑–] ?([0-9]{1,4})/([0-9]{2})\M','g') x group by 1,2),
r as (select m.case_id, m.cn,
  (select count(*) from cle_v2.cjeu_document d where d.case_number=m.cn) ncand,
  exists (select 1 from cle_v2.case_citation cc join cle_v2.cjeu_document d on d.case_id=cc.target_case_id where cc.source_case_id=m.case_id and d.case_number=m.cn) has_edge
 from m)
select (select count(*) from s) ntexts, count(distinct case_id) cases_with_cn, count(*) mentions,
 count(*) filter (where ncand>0) resolvable, count(*) filter (where has_edge) with_edge,
 count(distinct case_id) filter (where ncand>0) cases_resolvable,
 count(distinct case_id) filter (where ncand>0 and not has_edge) cases_with_missing,
 count(*) filter (where left(cn,1)='C') c, count(*) filter (where left(cn,1)='T') t, count(*) filter (where left(cn,1)='F') f
from r
