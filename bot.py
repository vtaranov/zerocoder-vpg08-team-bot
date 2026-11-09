"""Telegram transport: passive recording, mentions, replies and sessions."""
import hashlib
import logging
import re
from datetime import datetime, timezone
import telebot
from config import load_config
from service import TeamBot

HELP = """VPG08: помощник команды с общей памятью.
Сохраняю текстовые сообщения людей в разрешённых чатах, отвечаю при @упоминании,
ответе на моё сообщение или /ask. В личном чате можно написать «Что думаешь?».
/start_listening - начать сессию обсуждения
/stop_listening - закончить сессию и получить итог
/status - состояние; /sync - повторить запись в Pinecone
/id - ID чата; /help - справка
В группах команды управления доступны владельцам из ALLOWED_USER_IDS.
Тексты и авторство сохраняются в локальной SQLite и передаются в OpenAI/Pinecone.
Обрабатываю только новые текстовые сообщения, без файлов и правок старых сообщений."""

def scope_for(message):
    return f'telegram:{message.chat.id}:{getattr(message, "message_thread_id", None) or 0}'

def allowed(config, message):
    if not message.from_user or message.from_user.is_bot or getattr(message, 'sender_chat', None):
        return False
    if message.chat.type == 'private':
        return message.from_user.id in config.admins
    return message.chat.type in {'group','supergroup'} and message.chat.id in config.chats

def addressed(message, username, bot_id):
    text = message.text or ''
    mention = bool(re.search(r'(?<![\w@])@' + re.escape(username) + r'(?![\w])', text, re.I))
    reply = message.reply_to_message
    reply_to_bot = bool(reply and reply.from_user and reply.from_user.id == bot_id)
    return mention or reply_to_bot or (message.chat.type == 'private' and text.strip().lower().rstrip('?! .') == 'что думаешь')

def command_for(text, username):
    token = text.split(maxsplit=1)[0] if text.split() else ''
    if not token.startswith('/'):
        return None
    command, _, target = token.partition('@')
    if target and target.lower() != username.lower():
        return 'other_bot'
    return command.lower()

def deliver(bot, message, text):
    # No parse_mode: model text cannot create malformed Markdown or hidden links.
    for start in range(0, len(text), 3500):
        kwargs = {'message_thread_id': message.message_thread_id} if getattr(message, 'message_thread_id', None) else {}
        bot.send_message(message.chat.id, text[start:start+3500], **kwargs)

def build_bot(config, service, bot=None, identity=None):
    bot = bot or telebot.TeleBot(config.token, threaded=False)
    identity = identity or bot.get_me()
    @bot.message_handler(content_types=['text'])
    def handle(message):
        text = message.text or ''
        command = command_for(text, identity.username)
        # Bootstrap ID command returns metadata to the configured owner only. No text is indexed.
        if command == '/id' and message.from_user and message.from_user.id in config.admins:
            deliver(bot, message, f'chat_id={message.chat.id}; topic_id={getattr(message, "message_thread_id", None) or 0}')
            return
        if not allowed(config, message):
            return
        scope = scope_for(message)
        is_admin = message.from_user.id in config.admins
        try:
            if command in {'/start','/help'}:
                deliver(bot, message, HELP)
            elif command in {'/start_listening','/stop_listening','/sync'}:
                if not is_admin:
                    deliver(bot, message, 'Команда доступна только владельцу бота.')
                    return
                if command == '/start_listening':
                    if message.chat.type != 'private' and not getattr(identity, 'can_read_all_group_messages', False):
                        member = bot.get_chat_member(message.chat.id, identity.id)
                        if member.status not in {'administrator', 'creator'}:
                            deliver(bot, message, 'Telegram не передаёт мне обычные сообщения группы. Для полной записи сессии настройте доступ к сообщениям в учебной группе и повторите команду.')
                            return
                    _, new = service.start(scope)
                    deliver(bot, message, 'Сессия начата. Записываю обсуждение до /stop_listening.' if new else 'Сессия уже идёт; накопленные сообщения сохранены.')
                elif command == '/stop_listening':
                    deliver(bot, message, service.stop(scope)['answer'])
                else:
                    n = service.flush(scope)
                    deliver(bot, message, f'Синхронизировано сообщений: {n}.')
            elif command == '/status':
                s = service.journal.status(scope)
                deliver(bot, message, f"Сообщений: {s['messages']}; ожидают индексации: {s['pending']}; сессия: {'идёт' if s['listening'] else 'не активна'}.")
            elif command == '/ask':
                q = text.partition(' ')[2].strip()
                deliver(bot, message, service.ask(scope, q)['answer'] if q else 'Напишите: /ask ваш вопрос')
            elif command:
                if command != 'other_bot':
                    deliver(bot, message, 'Неизвестная команда. /help')
            else:
                u = message.from_user
                author = ' '.join(v for v in [u.first_name, u.last_name] if v) or f'Участник {u.id}'
                when = datetime.fromtimestamp(message.date, timezone.utc).isoformat()
                service.record(scope, message.message_id, u.id, author, when, text)
                if addressed(message, identity.username, identity.id):
                    question = re.sub('@' + re.escape(identity.username), '', text, flags=re.I).strip() or 'Что думаешь об обсуждении?'
                    deliver(bot, message, service.ask(scope, question, exclude=message.message_id)['answer'])
        except Exception as exc:
            logging.getLogger(__name__).error('request_error=%s', type(exc).__name__)
            msg = str(exc) if isinstance(exc, ValueError) else 'Запрос не завершён. Журнал сохранён; повторите команду. /status покажет очередь индексации.'
            deliver(bot, message, msg)
    return bot

def main():
    import fcntl
    from pathlib import Path
    logging.basicConfig(level=logging.WARNING)
    for name in ['httpx','httpcore','urllib3','telebot','pinecone']:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    config = load_config(require_bot=True)
    lockpath = Path('/tmp') / ('zerocoder-telegram-' + hashlib.sha256(config.token.encode()).hexdigest()[:16] + '.lock')
    with lockpath.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Другой учебный бот с тем же токеном уже запущен.')
        bot = telebot.TeleBot(config.token, threaded=False)
        if bot.get_webhook_info().url:
            raise SystemExit('Активен webhook; polling не запущен.')
        me = bot.get_me()
        service = TeamBot(config)
        build_bot(config, service, bot, me)
        print(f'VPG08 ready: @{me.username}; permitted groups: {len(config.chats)}', flush=True)
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20, allowed_updates=['message'])
        finally:
            bot.stop_polling()

if __name__ == '__main__':
    main()
