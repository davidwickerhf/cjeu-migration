with s as (select case_id, fulltext t from cle_v2.case_text tablesample system(1) repeatable(7) where source='RECHTSPRAAK' and fulltext is not null),
m0 as (select s.case_id, x[2] a, x[1] pre from s, regexp_matches(s.t, '((?:nr|nrs|no|nos|appl|klachtnummers?|application)\.?:? ?)\m([0-9]{1,6}/[0-9]{2})\M(?![0-9/])','gi') x),
m as (select case_id, a, bool_or(true) from m0 group by 1,2),
ehrm as (select case_id from s where t ~ 'EHRM|ECHR|EVRM|Europees Hof voor de Rechten') ,
c as (select m.case_id, m.a, (m.case_id in (select case_id from ehrm)) ctx,
  (select count(distinct ed.case_id) from cle_v2.echr_document_appno p join cle_v2.echr_document ed on ed.item_id=p.item_id where p.appno=m.a and p.source='appno') ncases,
  (select count(distinct ed.case_id) from cle_v2.echr_document_appno p join cle_v2.echr_document ed on ed.item_id=p.item_id where p.appno=m.a and p.source='appno' and ed.doctype in ('HEJUD','HFJUD')) njud,
  exists (select 1 from cle_v2.case_citation cc join cle_v2.echr_document ed on ed.case_id=cc.target_case_id join cle_v2.echr_document_appno p on p.item_id=ed.item_id and p.source='appno' where cc.source_case_id=m.case_id and p.appno=m.a) has_edge
 from m)
select count(*) mentions, count(*) filter (where ctx) in_ehrm_ctx, count(*) filter (where ncases>0) resolvable, count(*) filter (where ctx and ncases>0) res_ctx,
 count(*) filter (where has_edge) edge, count(*) filter (where ctx and ncases>0 and not has_edge) missing,
 count(*) filter (where ctx and ncases>1 and not has_edge) missing_multi, count(*) filter (where ctx and njud=1 and not has_edge) missing_one_jud, count(*) filter (where ctx and njud>1 and not has_edge) missing_multi_jud, count(*) filter (where ctx and njud=0 and ncases>0 and not has_edge) missing_dec_only,
 count(distinct case_id) filter (where ctx and ncases>0 and not has_edge) cases_missing
from c
