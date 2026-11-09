import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from telebot.types import Message
from config import Config
from storage import Journal
from service import TeamBot
from bot import build_bot, allowed, command_for, scope_for, addressed

class LocalTeam(TeamBot):
    def __init__(self, journal):
        self.journal = journal
        self.contexts = []
        self.fail_generation = False
    def flush(self, scope):
        raise ConnectionError('synthetic outage')
    def generate(self, task, context):
        if self.fail_generation:
            raise ConnectionError('synthetic outage')
        self.contexts.append(json.loads(context))
        return 'Проверочное резюме'

def message(mid=1, uid=10, text='текст', chat=-100, kind='supergroup', bot=False, **extra):
    return Message.de_json(dict(message_id=mid, date=1794222000,
        chat=dict(id=chat,type=kind), text=text,
        **{'from':dict(id=uid,is_bot=bot,first_name='Анна')}, **extra))

class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'test.sqlite3'
        self.j = Journal(self.path)
        self.s = LocalTeam(self.j)
        self.c = Config(api_key='test',pinecone_key='test',index='test',admins=(10,),chats=(-100,))
    def tearDown(self):
        self.tmp.cleanup()
    def add(self, mid, text='текст', scope='a', author='Анна'):
        return self.s.record(scope,mid,'1',author,'2026-11-09T12:00:00+00:00',text)
    def test_restart_preserves_session_and_outbox(self):
        sid,_=self.s.start('a')
        self.add(1)
        reopened=Journal(self.path)
        self.assertEqual(reopened.status('a'),dict(messages=1,pending=1,listening=True))
        self.assertEqual(reopened.pending('a')[0]['session_id'],sid)
    def test_delivery_retry_deduplicates_only_message_id(self):
        self.add(1,'Срок - пятница')
        self.add(1,'не должен заменить оригинал')
        self.add(2,'Срок - пятница')
        self.assertEqual(self.j.status('a')['messages'],2)
        self.assertEqual(self.j.pending('a')[0]['text'],'Срок - пятница')
    def test_repeated_start_does_not_lose_messages(self):
        sid,new=self.s.start('a');self.assertTrue(new)
        self.add(1)
        self.assertEqual(self.s.start('a'),(sid,False))
        self.assertEqual(len(self.j.session_rows('a',sid)),1)
    def test_complete_session_chronology_and_boundaries(self):
        self.add(1,'До сессии')
        self.s.start('a')
        self.add(3,'Поздняя поправка')
        self.add(2,'Раннее предложение',author='Борис')
        out=self.s.stop('a')
        self.assertEqual(out['message_count'],2)
        self.assertEqual([r['message_id'] for r in self.s.contexts[-1]],[2,3])
        self.assertEqual([r['author'] for r in self.s.contexts[-1]],['Борис','Анна'])
        self.add(4,'После сессии')
        self.assertTrue(self.s.stop('a')['cached'])
        self.assertEqual(len(self.s.contexts),1)
    def test_failed_summary_can_be_retried_after_restart(self):
        self.s.start('a');self.add(1)
        self.s.fail_generation=True
        with self.assertRaises(ConnectionError):self.s.stop('a')
        other=LocalTeam(Journal(self.path))
        self.assertEqual(other.stop('a')['message_count'],1)
        self.assertFalse(other.journal.status('a')['listening'])
    def test_chat_isolation_and_empty_session(self):
        self.s.start('a');self.add(1,scope='b')
        self.assertEqual(self.s.stop('a')['message_count'],0)
        self.assertEqual(self.j.recent('a'),[])
    def test_access_and_topic_isolation(self):
        self.assertTrue(allowed(self.c,message(uid=20)))
        self.assertFalse(allowed(self.c,message(chat=-200)))
        self.assertFalse(allowed(self.c,message(bot=True)))
        self.assertFalse(allowed(self.c,message(uid=20,chat=20,kind='private')))
        self.assertNotEqual(scope_for(message()),scope_for(message(message_thread_id=5)))
    def test_mentions_replies_and_targeted_commands(self):
        self.assertTrue(addressed(message(text='@Demo_bot что думаешь?'),'Demo_bot',77))
        self.assertFalse(addressed(message(text='@Demo_bot_other'),'Demo_bot',77))
        self.assertEqual(command_for('/start_listening@Demo_bot','demo_bot'),'/start_listening')
        self.assertEqual(command_for('/start@Another_bot','Demo_bot'),'other_bot')
    def test_transport_passive_capture_and_owner_commands(self):
        transport=Mock()
        # Use real telebot handler registration and synchronous dispatch, fake only network delivery.
        import telebot
        bot=telebot.TeleBot('123:test',threaded=False)
        bot.send_message=transport.send_message
        build_bot(self.c,self.s,bot,SimpleNamespace(username='Demo_bot',id=77,can_read_all_group_messages=True))
        bot.process_new_messages([message(uid=20)])
        self.assertEqual(self.j.status('telegram:-100:0')['messages'],1)
        transport.send_message.assert_not_called()
        bot.process_new_messages([message(mid=2,uid=20,text='/start_listening@Demo_bot')])
        self.assertFalse(self.j.status('telegram:-100:0')['listening'])
        bot.process_new_messages([message(mid=3,text='/start_listening@Demo_bot')])
        self.assertTrue(self.j.status('telegram:-100:0')['listening'])
        bot.process_new_messages([message(mid=4,text='ответ бота',bot=True)])
        self.assertEqual(self.j.status('telegram:-100:0')['messages'],1)
    def test_index_retry_marks_only_acknowledged_records(self):
        from unittest.mock import patch
        self.j.add('a',1,'1','Анна','2026-11-09','Один')
        real=object.__new__(TeamBot);real.config=self.c;real.journal=self.j
        with patch('service.indexing_pipeline') as factory:
            factory.return_value.run.side_effect=ConnectionError('outage')
            with self.assertRaises(ConnectionError):real.flush('a')
            self.assertEqual(self.j.status('a')['pending'],1)
            factory.return_value.run.side_effect=None
            factory.return_value.run.return_value={'write':{'documents_written':1}}
            self.assertEqual(real.flush('a'),1)
            self.assertEqual(self.j.status('a')['pending'],0)
    def test_privacy_mode_refuses_incomplete_session(self):
        import telebot
        bot=telebot.TeleBot('123:test',threaded=False)
        bot.send_message=Mock()
        bot.get_chat_member=Mock(return_value=SimpleNamespace(status='member'))
        build_bot(self.c,self.s,bot,SimpleNamespace(username='Demo_bot',id=77,can_read_all_group_messages=False))
        bot.process_new_messages([message(text='/start_listening')])
        self.assertFalse(self.j.status('telegram:-100:0')['listening'])
        self.assertIn('Telegram не передаёт',bot.send_message.call_args.args[1])
    def test_id_bootstrap_does_not_record_unapproved_chat(self):
        import telebot
        bot=telebot.TeleBot('123:test',threaded=False);bot.send_message=Mock()
        build_bot(self.c,self.s,bot,SimpleNamespace(username='Demo_bot',id=77,can_read_all_group_messages=True))
        bot.process_new_messages([message(chat=-200,text='/id')])
        bot.send_message.assert_called_once()
        self.assertEqual(self.j.status('telegram:-200:0')['messages'],0)

if __name__=='__main__':unittest.main()
