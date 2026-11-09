"""Real API verification with fictional discussion; writes sanitized public evidence."""
import json
import logging
import sys
import tempfile
import time
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from config import ROOT, load_config
from service import TeamBot
from pipelines import search_pipeline, namespace

logging.basicConfig(level=logging.WARNING)
for n in ['httpx','httpcore','urllib3','pinecone']:
    logging.getLogger(n).setLevel(logging.CRITICAL)

@contextmanager
def synthetic_namespace(config, scope, result):
    try:
        yield
    finally:
        from pinecone import Pinecone
        index=Pinecone(api_key=config.pinecone_key).Index(config.index)
        ns=namespace(config,scope)
        index.delete(delete_all=True,namespace=ns)
        deadline=time.monotonic()+40
        while index.describe_index_stats().namespaces.get(ns):
            if time.monotonic()>deadline:
                raise TimeoutError('Synthetic namespace deletion not confirmed')
            time.sleep(1)
        result['cleanup']='Deletion of the isolated synthetic run namespace confirmed.'
        (ROOT/'evidence').mkdir(exist_ok=True)
        (ROOT/'evidence/live-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))


def main():
    run=uuid4().hex[:10]
    scope='live:fictional:'+run
    c=load_config()
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'model':c.model,
        'embedding_model':c.embedding_model,'transport':'CLI/service, real OpenAI and Pinecone',
        'data':'Fictional authors and project, no Telegram IDs or real chat content','checks':{}}
    with tempfile.TemporaryDirectory() as tmp, synthetic_namespace(c,scope,result):
        c=replace(c,database=Path(tmp)/'team.sqlite3')
        s=TeamBot(c)
        s.record(scope,1,'anna','Анна','2026-11-09T09:00:00+00:00',
            'Вне сессии: старый проект Астра имел бюджет 90000 рублей. Это не бюджет нового проекта.')
        sid,created=s.start(scope)
        rows=[
            (2,'anna','Анна','Предлагаю провести демонстрацию проекта КВАРЦ-731 в четверг в 11:00. Бюджет 18000 рублей.'),
            (3,'boris','Борис','В четверг я занят. Предлагаю пятницу в 15:30. Я подготовлю стенд.'),
            (4,'anna','Анна','Согласна: окончательная демонстрация КВАРЦ-731 в пятницу в 15:30, четверг отменяем. Бюджет уменьшаем до 12500 рублей. Я подготовлю презентацию к пятнице до 12:00.'),
            (5,'boris','Борис','Подтверждаю пятницу 15:30 и бюджет 12500 рублей. Стенд подготовлю к пятнице до 14:00. Адрес площадки пока не определён.')]
        for mid,uid,name,text in rows:
            s.record(scope,mid,uid,name,f'2026-11-09T09:0{mid}:00+00:00',text)
        # Same update again must not add a vector/message; similar text by a different speaker must stay.
        s.record(scope,5,'boris','Борис','2026-11-09T09:05:00+00:00',rows[-1][3])
        s.flush(scope)
        result['checks']['journal']=s.journal.status(scope)
        assert result['checks']['journal']==dict(messages=5,pending=0,listening=True)
        # Pinecone is eventually consistent. Poll until all expected messages are searchable.
        deadline=time.monotonic()+40
        while True:
            docs=search_pipeline(c,scope).run({'embed':{'text':'Проект КВАРЦ-731 демонстрация бюджет'}})['retrieve']['documents']
            if {int(d.meta['message_id']) for d in docs}=={1,2,3,4,5}:break
            if time.monotonic()>deadline:raise TimeoutError('Index search visibility not confirmed')
            time.sleep(1)
        result['checks']['pinecone']={'count':len(docs),'message_ids':sorted(int(d.meta['message_id']) for d in docs),
            'authors':sorted({d.meta['author'] for d in docs})}
        # Rebuild the service to prove that session state is persistent.
        s=TeamBot(c)
        answer=s.ask(scope,'Что думаешь? Когда в итоге демонстрация КВАРЦ-731, какой бюджет и кто что готовит?')
        result['checks']['context_answer']=answer
        print('Context answer:', answer['answer'],flush=True)
        assert '12500' in answer['answer'].translate(str.maketrans('', '', ' \u00a0\u202f\n\t'))
        summary=s.stop(scope)
        result['checks']['session_summary']=summary
        assert summary['message_count']==4
        assert '15:30' in summary['answer']
        assert '12500' in summary['answer'].translate(str.maketrans('', '', ' \u00a0\u202f\n\t'))
        result['checks']['repeated_stop']=s.stop(scope)
        assert result['checks']['repeated_stop']['cached']
        result['checks']['unknown_fact']=s.ask(scope,'Какой точный адрес площадки демонстрации?')
        result['checks']['other_chat']=s.ask(scope+':other','Когда демонстрация КВАРЦ-731 и какой бюджет?')
        assert result['checks']['other_chat']['retrieved']==0
        assert result['checks']['other_chat']['context_ids']==[]
        # Direct semantic retrieval, without a recent local journal, proves persisted archive search.
        remote=TeamBot(replace(c,database=Path(tmp)/'fresh.sqlite3'))
        result['checks']['archive_only']=remote.ask(scope,'Каков окончательный бюджет проекта КВАРЦ-731?')
        assert result['checks']['archive_only']['retrieved']>0
        assert '12500' in result['checks']['archive_only']['answer'].translate(str.maketrans('', '', ' \u00a0\u202f\n\t'))
    (ROOT/'evidence').mkdir(exist_ok=True)
    (ROOT/'evidence/live-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
