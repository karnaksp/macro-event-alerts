"""Official monthly results need a prior live baseline, never a fabricated forecast."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import yaml

from macro_event_alerts.app import _ingest
from macro_event_alerts.domain import sha256
from macro_event_alerts.storage import Store
from macro_event_alerts.validation import get_contracts

ROOT = Path(__file__).resolve().parents[1]


def settings():
    config = yaml.safe_load((ROOT / 'config.replay.yaml').read_text())
    config['providers'] = {'cbr': {'adapter':'cbr_web','enabled':True,'experimental':True}}
    rule = copy.deepcopy(config['rules'][0])
    rule.update(provider_ids=['cbr'], series_ids=['RU.CPI.HEADLINE.YOY'], event_types=['macro.release','macro.revised'])
    config['rules'] = [rule]
    return config


def envelope(period, at, *, backfill=False):
    item = json.loads((ROOT / 'examples/observations/macro.json').read_text())
    item.update(observation_id=str(uuid4()), observed_at=at, is_backfill=backfill)
    item['source'].update(provider_id='cbr', source_url='https://www.cbr.ru/hd_base/infl/', source_updated_at=None)
    item['subject'].update(country='RU', currency='RUB', series_id='RU.CPI.HEADLINE.YOY',
                           release_key='cbr:infl:'+period, reference_period=period)
    item['values'].update(actual='7.1',forecast=None,forecast_origin='unknown',scheduled_at=None,published_at=None)
    raw={'synthetic':True,'period':period,'values':item['values']}
    item['raw_sha256']=sha256(raw)
    return {'raw':raw,'observation':item}


def ingest(store, item):
    with store.transaction():
        _ingest(store, settings(), get_contracts(), item,
                now=datetime.fromisoformat(item['observation']['observed_at'].replace('Z','+00:00')))


def test_first_month_is_quiet_then_new_month_becomes_one_real_release(tmp_path):
    with Store(tmp_path / 'state.db') as store:
        ingest(store,envelope('2026-08','2026-10-01T10:00:00Z'))
        assert store.stats()['events'] == 0
        ingest(store,envelope('2026-09','2026-10-14T10:00:00Z'))
        assert store.stats()['events'] == 1
        assert store.stats()['outbox'] == 1
        event=json.loads(store.conn.execute('SELECT body_json FROM events').fetchone()[0])
        assert event['event_type']=='macro.release'
        assert event['subject']['reference_period']=='2026-09'
        assert event['payload']['forecast'] is None
        ingest(store,envelope('2026-09','2026-10-14T10:01:00Z'))
        assert store.stats()['events']==1
        ingest(store,envelope('2026-07','2026-10-14T10:02:00Z'))
        assert store.stats()['events']==1


def test_history_cannot_seed_live_monthly_alerts(tmp_path):
    with Store(tmp_path / 'state.db') as store:
        ingest(store,envelope('2026-08','2026-10-01T10:00:00Z',backfill=True))
        ingest(store,envelope('2026-09','2026-10-14T10:00:00Z'))
        assert store.stats()['events']==0
        assert store.stats()['outbox']==0
