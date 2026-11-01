"""Remove one callback upon an authenticated request to the operator."""
import argparse
from pathlib import Path
import sys
from uuid import UUID
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web_api import Settings
from web_leads import LeadStore

parser = argparse.ArgumentParser(description='Удаление заявки сайта и связанных данных')
parser.add_argument('--delete-id', required=True, type=UUID, help='UUID из карточки заявки')
args = parser.parse_args()
store = LeadStore(Settings.from_env().database)
print('Удалено' if store.delete(str(args.delete_id)) else 'Заявка не найдена')
