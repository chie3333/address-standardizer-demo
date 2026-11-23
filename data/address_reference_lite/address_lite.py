"""작은 기준목록 DuckDB + 원본 Parquet 직접 조회. 원본 전체를 DB에 복사하지 않습니다."""
from pathlib import Path
import hashlib,json,re,time
import duckdb
import pandas as pd

REGION=['법정동시도명','법정동시군구명','법정동읍면동명','법정동리명']
CODE_WIDTHS={'법정동코드':10,'도로명코드':12,'행정구역분류코드':8}
def literal(value):return "'"+str(value).replace("'","''")+"'"
def code_expr(col,width):
    return f'''CASE WHEN try_cast("{col}" AS DOUBLE)=try_cast("{col}" AS BIGINT)
      AND length(cast(try_cast("{col}" AS BIGINT) AS VARCHAR))={width}
      THEN cast(try_cast("{col}" AS BIGINT) AS VARCHAR) END'''
def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk:=f.read(8*1024*1024):h.update(chunk)
    return h.hexdigest()

def build_catalog(source_dir,catalog_path,years=(2023,2024,2025)):
    """연도별 파르케에서 범주·코드 관계만 추출하여 작은 단일 DB를 만듭니다."""
    source_dir=Path(source_dir).resolve();target=Path(catalog_path).resolve()
    if target.exists():raise FileExistsError(f'기존 파일을 덮어쓰지 않습니다: {target}')
    paths={y:source_dir/f'RAW_DATA_{y}.parquet' for y in years}
    missing=[str(p) for p in paths.values() if not p.is_file()]
    if missing:raise FileNotFoundError(', '.join(missing))
    target.parent.mkdir(parents=True,exist_ok=True)
    c=duckdb.connect(str(target));c.execute('SET threads=4');c.execute("SET memory_limit='2GB'")
    def store(table,query):
        exists=c.execute('SELECT count(*) FROM information_schema.tables WHERE table_name=? AND table_schema=\'main\'',[table]).fetchall()[0][0]
        if exists:c.execute(f'INSERT INTO {table} BY NAME '+query)
        else:c.execute(f'CREATE TABLE {table} AS '+query)
    started=time.perf_counter()
    try:
        c.execute('BEGIN TRANSACTION')
        c.execute('CREATE TABLE sources(기준연도 INTEGER PRIMARY KEY, file_name VARCHAR, file_size_bytes BIGINT, sha256 VARCHAR, source_rows BIGINT, source_periods VARCHAR, serial_is_unique BOOLEAN)')
        c.execute('CREATE TABLE metadata(key VARCHAR PRIMARY KEY,value_json VARCHAR)')
        for year,path in paths.items():
            c.execute(f'CREATE OR REPLACE TEMP VIEW source_raw AS SELECT * FROM read_parquet({literal(path)})')
            stats=c.sql('SELECT count(*),count(DISTINCT 일련번호),count(*) FILTER(WHERE 일련번호 IS NULL) FROM source_raw').fetchall()[0]
            periods=c.sql('SELECT DISTINCT try_cast(기준연월 AS BIGINT) FROM source_raw ORDER BY 1').fetchall()
            if any(p[0] is None or p[0]//100!=year for p in periods):raise ValueError(f'{year}: 파일명과 원본 기준연월이 다릅니다.')
            if stats[0]!=stats[1] or stats[2]:raise ValueError('원본 일련번호가 유일하지 않아 연결키를 만들 수 없습니다.')
            c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?)',[year,path.name,path.stat().st_size,file_hash(path),stats[0],json.dumps([p[0] for p in periods]),True])
            fields=['*']+[f'{code_expr(col,w)} AS "{col}_key"' for col,w in CODE_WIDTHS.items()]
            c.execute('CREATE OR REPLACE TEMP VIEW source_keys AS SELECT '+', '.join(fields)+' FROM source_raw')
            store('legal_regions',f'''SELECT DISTINCT {year}::INTEGER AS 기준연도, 법정동코드_key AS legal_code,
                substr(법정동코드_key,1,2) AS sido_id, substr(법정동코드_key,1,5) AS sigungu_id,
                substr(법정동코드_key,1,8) AS emd_id, {', '.join(REGION)} FROM source_keys''')
            store('region_counts',f'''SELECT {year}::INTEGER AS 기준연도,{', '.join(REGION)},count(*)::BIGINT AS source_row_count FROM source_keys GROUP BY ALL''')
            store('legal_road_links',f'''SELECT DISTINCT {year}::INTEGER AS 기준연도,법정동코드_key AS legal_code,도로명코드_key AS road_code FROM source_keys''')
            store('legal_admin_links',f'''SELECT DISTINCT {year}::INTEGER AS 기준연도,법정동코드_key AS legal_code,행정구역분류코드_key AS admin_code FROM source_keys''')
            store('administrative_units',f'''SELECT DISTINCT {year}::INTEGER AS 기준연도,행정구역분류코드_key AS admin_code,行정동명,관할행정기관명 FROM source_keys'''.replace('行정동명','행정동명'))
            store('road_catalog',f'''SELECT {year}::INTEGER AS 기준연도,도로명코드_key AS road_id,
                substr(도로명코드_key,1,2) AS road_sido_segment,substr(도로명코드_key,3,3) AS road_sigungu_segment,
                substr(도로명코드_key,1,5) AS sigungu_id,substr(도로명코드_key,6,1) AS road_level_segment,
                substr(도로명코드_key,7,6) AS road_name_segment,
                list(DISTINCT 도로명 ORDER BY 도로명) AS road_names FROM source_keys WHERE 도로명코드_key IS NOT NULL GROUP BY ALL''')
            store('sido',f'''SELECT 기준연도,sido_id,sido_id AS legal_sido_segment,list(DISTINCT 법정동시도명 ORDER BY 法정동시도명) AS sido_names FROM legal_regions WHERE 기준연도={year} GROUP BY ALL'''.replace('法정동시도명','법정동시도명'))
            store('sigungu',f'''SELECT 기준연도,sigungu_id,sido_id,substr(sigungu_id,3,3) AS legal_sigungu_segment,
                list(DISTINCT 법정동시군구명 ORDER BY 법정동시군구명) FILTER(WHERE 법정동시군구명 IS NOT NULL) AS sigungu_names FROM legal_regions WHERE 기준연도={year} GROUP BY ALL''')
            store('emd',f'''SELECT 기준연도,emd_id,sigungu_id,substr(emd_id,6,3) AS legal_emd_segment,
                list(DISTINCT 법정동읍면동명 ORDER BY 법정동읍면동명) AS emd_names FROM legal_regions WHERE 기준연도={year} GROUP BY ALL''')
            store('ri',f'''SELECT 기준연도,legal_code AS ri_id,emd_id,substr(legal_code,9,2) AS legal_ri_segment,
                list(DISTINCT 법정동리명 ORDER BY 법정동리명) AS ri_names FROM legal_regions WHERE 기준연도={year} AND nullif(trim(법정동리명),'') IS NOT NULL GROUP BY ALL''')
        c.execute('''CREATE VIEW road_table AS SELECT r.*,g.sigungu_names,s.sido_names FROM road_catalog r
            LEFT JOIN sigungu g ON r.기준연도=g.기준연도 AND r.sigungu_id=g.sigungu_id
            LEFT JOIN sido s ON g.기준연도=s.기준연도 AND g.sido_id=s.sido_id''')
        c.execute('''CREATE TABLE admin_sido AS SELECT a.기준연도,substr(a.admin_code,1,2) AS admin_sido_id,
            list(DISTINCT l.법정동시도명 ORDER BY l.법정동시도명) AS observed_sido_names
            FROM legal_admin_links a JOIN legal_regions l ON a.기준연도=l.기준연도 AND a.legal_code=l.legal_code
            WHERE a.admin_code IS NOT NULL GROUP BY ALL''')
        c.execute('''CREATE TABLE admin_sigungu AS SELECT a.기준연도,substr(a.admin_code,1,5) AS admin_sigungu_id,
            substr(a.admin_code,1,2) AS admin_sido_id,substr(a.admin_code,3,3) AS admin_sigungu_segment,
            list(DISTINCT l.법정동시군구명 ORDER BY l.법정동시군구명) FILTER(WHERE l.법정동시군구명 IS NOT NULL) AS observed_sigungu_names
            FROM legal_admin_links a JOIN legal_regions l ON a.기준연도=l.기준연도 AND a.legal_code=l.legal_code
            WHERE a.admin_code IS NOT NULL GROUP BY ALL''')
        c.execute('''CREATE TABLE admin_emd AS SELECT 기준연도,admin_code AS admin_emd_id,substr(admin_code,1,5) AS admin_sigungu_id,
            substr(admin_code,6,3) AS admin_emd_segment,
            list(DISTINCT 행정동명 ORDER BY 행정동명) FILTER(WHERE 행정동명 IS NOT NULL) AS observed_emd_names,
            list(DISTINCT 관할행정기관명 ORDER BY 관할행정기관명) FILTER(WHERE 관할행정기관명 IS NOT NULL) AS observed_agency_names
            FROM administrative_units WHERE admin_code IS NOT NULL GROUP BY ALL''')
        c.execute('''CREATE VIEW region_admin_links AS SELECT l.기준연도,l.sido_id,l.sigungu_id,l.emd_id,l.legal_code,a.admin_code
            FROM legal_regions l JOIN legal_admin_links a ON l.기준연도=a.기준연도 AND l.legal_code=a.legal_code''')
        c.execute('''CREATE TABLE region_hierarchy(기준연도 INTEGER,node_id VARCHAR,parent_id VARCHAR,node_type VARCHAR,name VARCHAR,observed_legal_code_prefix VARCHAR,PRIMARY KEY(기준연도,node_id))''')
        c.execute('''CREATE TABLE sigungu_hierarchy_links(기준연도 INTEGER,sigungu_id VARCHAR,original_sigungu_name VARCHAR,city_name VARCHAR,district_name VARCHAR,parent_node_id VARCHAR,leaf_node_id VARCHAR)''')
        nodes={};links=[]
        def node(year,parent,kind,name,prefix):
            key=json.dumps([year,parent,kind,name,prefix],ensure_ascii=False)
            nid=hashlib.sha256(key.encode()).hexdigest()[:24]
            nodes[(year,nid)]=(year,nid,parent,kind,name,prefix);return nid
        rows=c.sql('SELECT g.기준연도,s.sido_id,s.sido_names,g.sigungu_id,g.sigungu_names FROM sigungu g JOIN sido s ON g.기준연도=s.기준연도 AND g.sido_id=s.sido_id ORDER BY 1,2,4').fetchall()
        for year,sid,snames,gid,gnames in rows:
            for sn in snames or [None]:
                root=node(year,None,'시도',sn,sid)
                for gn in gnames or [None]:
                    match=re.fullmatch(r'(.+시)\s+([^ ]+구)',gn or '')
                    if match:
                        parent=node(year,root,'시',match[1],None);leaf=node(year,parent,'구',match[2],gid)
                        links.append((year,gid,gn,match[1],match[2],parent,leaf))
                    else:
                        kind=next((k for k in ['시','군','구'] if (gn or '').endswith(k)),'지역코드범위')
                        leaf=node(year,root,kind,gn,gid)
                        links.append((year,gid,gn,gn if kind=='시' else None,gn if kind=='구' else None,root,leaf))
        c.executemany('INSERT INTO region_hierarchy VALUES (?,?,?,?,?,?)',list(nodes.values()))
        c.executemany('INSERT INTO sigungu_hierarchy_links VALUES (?,?,?,?,?,?,?)',links)
        for table,key in [('sido','sido_id'),('sigungu','sigungu_id'),('emd','emd_id'),('ri','ri_id'),('road_catalog','road_id'),('admin_sido','admin_sido_id'),('admin_sigungu','admin_sigungu_id'),('admin_emd','admin_emd_id')]:
            c.execute(f'CREATE UNIQUE INDEX idx_{table}_key ON {table}(기준연도,{key})')
        for key,value in {'storage':'catalog_only; full addresses stay in original Parquet','duckdb_version':duckdb.__version__,'source_columns':20,
                'code_layouts_provided':{'legal':[2,3,3,2],'administrative':[2,3,3],'road':[2,3,1,6]},
                'notes':['기준연도는 기준자료 시점이며 입력 작성연도 아님','원본 이름/코드 관측 목록이며 공식 명칭 마스터 검증 전','중간 시 노드의 공식코드 추정 안함','동층호/지하구분 등 없는 정보 생성 안함']}.items():
            c.execute('INSERT INTO metadata VALUES (?,?)',[key,json.dumps(value,ensure_ascii=False)])
        c.execute('DROP VIEW source_keys');c.execute('DROP VIEW source_raw')
        c.execute('COMMIT');c.execute('CHECKPOINT')
        return {'catalog':str(target),'years':list(years),'build_seconds':round(time.perf_counter()-started,3)}
    except Exception:
        try:c.execute('ROLLBACK')
        except Exception:pass
        raise
    finally:c.close()

class LiteAddressDB:
    def __init__(self,catalog_path,source_dir=None,verify_hash=False):
        self.path=Path(catalog_path).resolve();self.con=duckdb.connect(str(self.path),read_only=True)
        folder=Path(source_dir).resolve() if source_dir is not None else self.path.parent/'data'
        self.sources={}
        for year,name,size,sha,_,_,_ in self.con.sql('SELECT * FROM sources ORDER BY 기준연도').fetchall():
            p=folder/name
            if not p.is_file():self.close();raise FileNotFoundError(f'원본 파르케가 필요합니다: {p}')
            if p.stat().st_size!=size:self.close();raise ValueError(f'원본 크기가 달라 카탈로그를 재생성해야 합니다: {p}')
            if verify_hash and file_hash(p)!=sha:self.close();raise ValueError(f'원본 해시가 다릅니다: {p}')
            self.sources[year]=p
    def close(self):self.con.close()
    def find_regions(self,sido=None,sigungu=None,emd=None,ri=None,year=None,city=None,district=None):
        terms=[];params=[]
        for col,val in zip(REGION,[sido,sigungu,emd,ri]):
            if val is not None:terms.append('l."'+col+'"=?');params.append(val)
        if year is not None:terms.append('l.기준연도=?');params.append(int(year))
        if city is not None or district is not None:
            q='EXISTS(SELECT 1 FROM sigungu_hierarchy_links h WHERE h.기준연도=l.기준연도 AND h.sigungu_id=l.sigungu_id'
            for col,val in [('city_name',city),('district_name',district)]:
                if val is not None:q+=' AND h.'+col+'=?';params.append(val)
            terms.append(q+')')
        return self.con.execute('SELECT l.* FROM legal_regions l'+(' WHERE '+' AND '.join(terms) if terms else '')+' ORDER BY ALL',params).df()
    def find_roads(self,name,sido=None,sigungu=None,year=None):
        terms=['list_contains(road_names,?)'];params=[name]
        for col,val in [('sido_names',sido),('sigungu_names',sigungu)]:
            if val is not None:terms.append(f'list_contains({col},?)');params.append(val)
        if year is not None:terms.append('기준연도=?');params.append(int(year))
        return self.con.execute('SELECT * FROM road_table WHERE '+' AND '.join(terms)+' ORDER BY ALL',params).df()
    def _search(self,year,code_col,code,main_col,main,sub_col,sub=None,land_type=None,limit=200):
        year=int(year)
        if year not in self.sources:raise ValueError('등록되지 않은 기준연도입니다.')
        if not isinstance(limit,int) or limit<1:raise ValueError('limit는 양의 정수여야 합니다.')
        code=str(code)
        if not code.isdigit() or len(code)!=CODE_WIDTHS[code_col]:raise ValueError('코드 길이 또는 형식이 잘못되었습니다.')
        if isinstance(main,bool) or not str(main).isdigit():raise ValueError('본번은 0 이상의 정수입니다.')
        terms=[f'"{code_col}"=?',f'regexp_full_match(trim("{main_col}"), \'[0-9]+\')',f'try_cast("{main_col}" AS INTEGER)=?'];params=[int(code),int(main)]
        if sub is not None:
            if isinstance(sub,bool) or not str(sub).isdigit():raise ValueError('부번은 0 이상의 정수입니다.')
            terms.extend([f'regexp_full_match(trim("{sub_col}"), \'[0-9]+\')',f'try_cast("{sub_col}" AS INTEGER)=?']);params.append(int(sub))
        if land_type is not None:terms.append('대지구분코드=?');params.append(land_type)
        where=' AND '.join(terms)
        source=f'read_parquet({literal(self.sources[year])})'
        total=self.con.execute(f'SELECT count(*) FROM {source} WHERE {where}',params).fetchall()[0][0]
        codes=', '.join(f'{code_expr(col,w)} AS "{col}"' for col,w in CODE_WIDTHS.items())
        combinations=self.con.execute(f'SELECT DISTINCT {year}::INTEGER AS 기준연도,{codes} FROM {source} WHERE {where} ORDER BY ALL',params).df()
        candidate=self.con.execute(f'SELECT {year}::INTEGER AS 기준연도,* FROM {source} WHERE {where} ORDER BY 일련번호 LIMIT ?',params+[limit]).df()
        return {'reference_year':year,'source_file':self.sources[year].name,'candidate_count':total,'truncated':total>limit,
                'code_combinations':combinations,'candidates':candidate,
                'search_conditions':{'code_column':code_col,'code':code,'main_column':main_col,'main':int(main),'sub':sub,'land_type':land_type},
                'record_key':['기준연도','일련번호'],'status':'후보있음' if total else '후보없음'}
    def road(self,year,road_code,main,sub=None,limit=200):
        return self._search(year,'도로명코드',road_code,'건물본번',main,'건물부번',sub,limit=limit)
    def lot(self,year,legal_code,main,sub=None,land_type=None,limit=200):
        return self._search(year,'법정동코드',legal_code,'지번본번',main,'지번부번',sub,land_type,limit)
    def lot_by_region(self,*,main,sub=None,land_type=None,limit=200,include_admin=False,**region):
        matches=self.find_regions(**region)[['기준연도','legal_code']].drop_duplicates();results=[]
        for year,code in matches.itertuples(index=False,name=None):
            if code is not None:results.append(self.lot(year,code,main,sub,land_type,limit))
        return self._combine(results,include_admin)
    def road_by_name(self,*,name,main,sub=None,sido=None,sigungu=None,limit=200,include_admin=False):
        results=[self.road(year,code,main,sub,limit) for year,code in self.find_roads(name,sido,sigungu)[['기준연도','road_id']].itertuples(index=False,name=None)]
        return self._combine(results,include_admin)
    def _combine(self,results,include_admin):
        frames=[r['code_combinations'] for r in results if not r['code_combinations'].empty]
        df=pd.concat(frames,ignore_index=True).drop_duplicates() if frames else pd.DataFrame(columns=['기준연도',*CODE_WIDTHS])
        cols=['법정동코드','도로명코드']+(['행정구역분류코드'] if include_admin else [])
        unique=df[cols].drop_duplicates()
        status='기준자료내후보없음' if df.empty else '복수코드조합후보' if len(unique)>1 else '단일코드조합후보'
        # 이 상태는 후보 정보일 뿐 주소의 유일성/정확성에 대한 자동 확정이 아닙니다.
        return {'input_address_year':None,'available_reference_years':sorted(self.sources),
            'missing_reference_years':sorted({2023,2024,2025}-set(self.sources)),
            'all_expected_years_checked':{2023,2024,2025}.issubset(self.sources),
            'comparison_code_fields':cols,'status':status,'code_combinations':df,'per_search':results}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('source_dir');parser.add_argument('catalog_path')
    args=parser.parse_args();print(json.dumps(build_catalog(args.source_dir,args.catalog_path),ensure_ascii=False,indent=2))
