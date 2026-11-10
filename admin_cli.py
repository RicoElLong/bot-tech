"""Create/reset the owner or make a consistent, non-overwriting SQLite backup."""

import argparse
from contextlib import closing
from getpass import getpass
import os
from pathlib import Path
import re
import sqlite3

from dotenv import load_dotenv

from admin_store import AdminStore
from admin_web import PASSWORDS
from storage import Store


def backup(source, destination):
    if not source.is_file():
        raise ValueError('Файл базы не найден.')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as original:
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Проверка резервной копии не пройдена.')
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def main():
    root = Path(__file__).resolve().parent
    load_dotenv(root / '.env.local')
    load_dotenv(root / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path(os.getenv('DATABASE_PATH', 'data/bot.sqlite3')))
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('create-owner', 'reset-owner'):
        actions.add_parser(name).add_argument('username')
    actions.add_parser('backup').add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'backup':
            backup(args.database, args.destination)
            print('Резервная копия создана и проверена.')
            return
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', args.username):
            raise ValueError('Логин должен содержать 3–64 латинские буквы, цифры, точки, дефисы или подчёркивания.')
        password = getpass('Пароль владельца: ')
        if not 12 <= len(password) <= 256 or password != getpass('Повторите пароль: '):
            raise ValueError('Пароли должны совпадать и содержать от 12 до 256 символов.')
        store = Store(args.database)
        with closing(store.db):
            AdminStore(store).set_owner(args.username, PASSWORDS.hash(password),
                                        reset=args.action == 'reset-owner')
        print('Учётная запись сохранена. Предыдущие сеансы завершены.')
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
