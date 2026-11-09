"""Application behavior shared by Telegram, CLI and live verification."""
import logging
from storage import Journal
from pipelines import indexing_pipeline, search_pipeline, summarization_pipeline, as_document, render_rows, validate_index

class TeamBot:
    def __init__(self, config, journal=None):
        self.config = config
        validate_index(config)
        self.journal = journal or Journal(config.database)
        self.generator = summarization_pipeline(config)

    def flush(self, scope):
        total = 0
        while rows := self.journal.pending(scope):
            indexing_pipeline(self.config, scope).run({'embed': {'documents': [as_document(r) for r in rows]}})
            self.journal.mark_indexed(scope, [r['message_id'] for r in rows])
            total += len(rows)
        return total

    def record(self, scope, mid, uid, author, created_at, text):
        row = self.journal.add(scope, mid, uid, author, created_at, text)
        try:
            self.flush(scope)
        except Exception as exc:
            # The durable outbox is retried on the next message or explicit /sync.
            logging.getLogger(__name__).warning('index_pending=%s', type(exc).__name__)
        return row

    def generate(self, task, context):
        # Never silently truncate a session and call the result a complete summary.
        if len(context) > 120000:
            raise ValueError('Слишком большой контекст: более 120000 символов. Нужна обработка частями; журнал сохранён.')
        result = self.generator.run({'prompt': {'task': task, 'context': context}})
        answer = result['llm']['replies'][0].text
        if not answer or not answer.strip():
            raise ValueError('Модель вернула пустой ответ.')
        return answer

    def ask(self, scope, question, exclude=None):
        # Retry pending writes, but still answer from the journal if the index is unavailable.
        warning = ''
        try:
            self.flush(scope)
            docs = search_pipeline(self.config, scope).run({'embed': {'text': question}})['retrieve']['documents']
        except Exception as exc:
            logging.getLogger(__name__).warning('retrieval_unavailable=%s', type(exc).__name__)
            docs = []
            warning = 'Поиск по архиву недоступен; использованы только последние сообщения.\n\n'
        recent = self.journal.recent(scope, exclude=exclude)
        by_id = {r['message_id']: r for r in recent}
        for d in docs:
            mid = int(d.meta['message_id'])
            if mid == exclude or mid in by_id:
                continue
            by_id[mid] = dict(d.meta, text=d.content)
        rows = sorted(by_id.values(), key=lambda r: r['message_id'])
        context = render_rows(rows)
        if len(context) > 120000:
            raise ValueError('Слишком много контекста. Уточните вопрос; журнал сохранён.')
        answer = self.generate('Ответь на вопрос: ' + question +
            '\nИспользуй последние сообщения и релевантные фрагменты архива. Это выборка, а не вся история чата.', context)
        return {'answer': warning + answer, 'retrieved': len(docs), 'context_ids': [r['message_id'] for r in rows]}

    def start(self, scope):
        return self.journal.begin(scope)

    def stop(self, scope):
        session = self.journal.close_session(scope)
        if not session:
            return {'answer': 'Нет сессии обсуждения. Сначала /start_listening.', 'message_count': 0}
        rows = self.journal.session_rows(scope, session['id'])
        if session['summary'] is not None:
            return {'answer': session['summary'], 'message_count': len(rows), 'cached': True}
        if not rows:
            answer = 'Сессия завершена. Сообщений в ней нет.'
        else:
            answer = self.generate('Подведи итог всей сессии в хронологическом порядке. Кратко перечисли: '
                'позиции участников; принятые решения; действия, ответственных и сроки; открытые вопросы. '
                'Если решения нет, так и напиши. При споре отдели мнение-рекомендацию от фактов. '
                'Не включай события за пределами этой сессии.', render_rows(rows))
        self.journal.save_summary(session['id'], answer)
        return {'answer': answer, 'message_count': len(rows), 'cached': False}
