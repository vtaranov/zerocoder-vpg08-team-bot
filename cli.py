"""Run the same service without Telegram; synthetic examples only."""
import argparse
import json
from datetime import datetime, timezone
from config import load_config
from service import TeamBot

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--scope', default='cli:demo')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('start')
    sub.add_parser('stop')
    sub.add_parser('status')
    sub.add_parser('sync')
    r = sub.add_parser('record')
    r.add_argument('message_id', type=int)
    r.add_argument('author')
    r.add_argument('text')
    a = sub.add_parser('ask')
    a.add_argument('question')
    args = p.parse_args()
    s = TeamBot(load_config())
    if args.cmd == 'start':
        result = s.start(args.scope)
    elif args.cmd == 'stop':
        result = s.stop(args.scope)
    elif args.cmd == 'status':
        result = s.journal.status(args.scope)
    elif args.cmd == 'sync':
        result = {'indexed': s.flush(args.scope)}
    elif args.cmd == 'record':
        result = s.record(args.scope, args.message_id, args.author, args.author, datetime.now(timezone.utc).isoformat(), args.text)
    else:
        result = s.ask(args.scope, args.question)
    print(json.dumps(result, ensure_ascii=False, indent=2))
