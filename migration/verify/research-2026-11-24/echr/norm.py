import re,json,glob,collections
H='[‐‑–\xad-]'
pairs=collections.defaultdict(set)  # (case_id) -> set of (kind,value,leadflag)
rows=0
lead={}
for f in glob.glob('full_*.txt'):
    for line in open(f).read().splitlines()[1:]:
        if line.startswith('TRUNC'): continue
        p=[x.strip() for x in line.split('|')]
        if len(p)<3: continue
        cid,lang,ref=p[0],p[1],'|'.join(p[2:]); rows+=1
        nums=[f"{m[1]}-{m[2]}/{m[3]}" for m in re.finditer(r'([CT]) ?'+H+r' ?(\d{1,3})/(\d{2})',ref)]
        for i,n in enumerate(nums):
            pairs[cid].add(('num',n,'')); (i>0 and lead.setdefault((cid,n),nums[0]))
        for m in re.finditer(r'EU:[CT]:\d{4}:\d+',ref): pairs[cid].add(('ecli','ECLI:'+m[0],''))
nums=sorted({v for s in pairs.values() for k,v,_ in s if k=='num'}); ec=sorted({v for s in pairs.values() for k,v,_ in s if k=='ecli'})
print('rows',rows,'sources',len(pairs),'num pairs',sum(1 for s in pairs.values() for k,v,_ in s if k=='num'),'ecli pairs',sum(1 for s in pairs.values() for k,v,_ in s if k=='ecli'),'distinct nums',len(nums),'distinct eclis',len(ec))
json.dump({k:sorted(map(list,v)) for k,v in pairs.items()},open('allpairs.json','w'))
open('allnums.txt','w').write('\n'.join(nums)); open('alleclis.txt','w').write('\n'.join(ec))

json.dump([[a,b,c] for (a,b),c in lead.items()],open('lead.json','w'))
