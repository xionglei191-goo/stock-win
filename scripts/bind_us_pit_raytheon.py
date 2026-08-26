
import sys, json, shutil; sys.path.insert(0,'.')
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import pandas as pd
from research_platform.us_pit.models import LicenseClass, SourceDependency, SourceRole
from research_platform.us_pit.sources_official import _require_sec_user_agent
from research_platform.us_pit.hashing import sha256_file, sha256_json
from research_platform.us_pit.store import USPITStore

ua=_require_sec_user_agent(None)
store=USPITStore('data/us_pit')
review_dir=Path('data/us_pit/review_inputs/oxalpha_final_v5').resolve()
output=Path('data/us_pit/review_inputs/oxalpha_final_v6').resolve()
if output.exists(): raise SystemExit('exists')

target_sid='us_isin_us7551115071'
chosen_cik='0001047122'
observed=datetime.now(timezone.utc).isoformat()
dep_list=[]
evidence={}
for cik,label in [(chosen_cik,'chosen'), ('0000082267','rejected-candidate')]:
    url=f'https://data.sec.gov/submissions/CIK{cik}.json'
    payload=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':ua}),timeout=60).read()
    ref=store.put_bytes(payload,media_type='application/json')
    dep_list.append(SourceDependency(
        source_id='sec_submissions',source_version='data-sec-submissions-v1',
        role=SourceRole.SIGNAL_INPUT,license_class=LicenseClass.OFFICIAL_PUBLIC,
        object_sha256=ref.sha256,observed_at=observed,published_at=observed,
        as_of_date=observed[:10],url=url,dataset='sec_company_search',
        metadata={'artifact_kind':'raw_data_sec_submissions','query_cik':cik,
                  'response_sha256':ref.sha256,'live_query_result':True}))
    d=json.loads(payload)
    evidence[label]={'cik':cik,'official_name':d.get('name'),
        'former':[f.get('name') for f in (d.get('formerNames') or [])],
        'filings_last': max(d.get('filings',{}).get('recent',{}).get('filingDate',[]) or ['']),
        'sha256':ref.sha256}

frame=pd.read_parquet(review_dir/'identity_review.parquet')
new_ids=[]
new_notes=[]
admitted=0
note_text=(f"Issuer CIK {chosen_cik} bound: conformed RAYTHEON CO (former HE HOLDINGS "
           f"INC renamed 1997 Hughes merger), SIC defense electronics, filings through "
           f"2020-04-13 matching the RTN-listed issuer of ISIN US7551115071; alternative "
           f"candidate 82267 rejected (filings ended 2013). sha256 "
           f"{evidence['chosen']['sha256'][:16]}.")
for _, row in frame.iterrows():
    current = str(row.get('issuer_id') or '').strip()
    sid = str(row.get('suggested_security_id') or '').strip()
    if current or sid != target_sid:
        new_ids.append(current)
        new_notes.append(str(row.get('review_note') or ''))
        continue
    admitted += 1
    new_ids.append(f'us_issuer_cik_{chosen_cik}')
    new_notes.append(note_text)
frame['issuer_id'] = new_ids
frame['review_note'] = new_notes
staging=output.parent/f'.{output.name}.staging-{uuid4().hex}'
staging.mkdir(parents=True)
for pth in sorted(review_dir.iterdir()):
    if pth.name=='identity_review.parquet': continue
    if pth.is_file(): shutil.copyfile(pth, staging/pth.name)
frame.to_parquet(staging/'identity_review.parquet',index=False)
manifest={'format_version':'us-pit-raytheon-discriminated-binding-v1',
 'base_dir':str(review_dir),'evidence':evidence,'admitted_rows':admitted,
 'batch_ids':[d.batch_id for d in [store.write_source_batch(dep_list)]],
 'bound_identity_review_sha256':sha256_file(staging/'identity_review.parquet')}
manifest['binding_id']=sha256_json(manifest)
(staging/'raytheon_binding_manifest.json').write_text(json.dumps(manifest,indent=1),encoding='utf-8')
staging.replace(output)
print(json.dumps({'status':'BOUND','binding_id':manifest['binding_id'],'admitted':admitted},indent=1))
