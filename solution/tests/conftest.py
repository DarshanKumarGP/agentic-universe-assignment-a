import json
import sqlite3
from pathlib import Path

import pytest

from ledger.crypto import Vault
from ledger.db import connect
from ledger.schema_def import load_catalogue, load_schemas

MOTOR = {"workflow_id": "motor_renewal", "fields": [
    {"name": "customer_name", "type": "string", "required": True, "pii": True,
     "aliases": ["name", "customer name", "insured name"]},
    {"name": "phone", "type": "phone", "required": True, "pii": True,
     "aliases": ["mobile", "mobile no", "phone number", "contact"]},
    {"name": "policy_number", "type": "string", "required": True, "reference": True,
     "aliases": ["policy no", "policy"]},
    {"name": "policy_expiry", "type": "date", "required": False, "aliases": ["expiry date", "expiry"]},
    {"name": "premium_amount", "type": "decimal", "required": False, "aliases": ["premium"]},
    {"name": "preferred_language", "type": "enum", "required": False,
     "values": ["en", "hi", "ta", "kn", "te", "mr"], "default": "en", "aliases": ["language"]}]}

DISPOSITIONS = """workflow_id,code,label,is_terminal,is_success
motor_renewal,renewed,Policy renewed,true,true
motor_renewal,payment_link_sent,Payment link sent,false,false
motor_renewal,callback_requested,Callback requested,false,false
motor_renewal,not_interested,Not interested,true,false
motor_renewal,dnd,Do not disturb,true,false
"""


@pytest.fixture
def schemas_dir(tmp_path) -> Path:
    d = tmp_path / "schemas"
    d.mkdir()
    (d / "motor_renewal.json").write_text(json.dumps(MOTOR))
    (d / "dispositions.csv").write_text(DISPOSITIONS)
    return d


@pytest.fixture
def schemas(schemas_dir):
    return load_schemas(schemas_dir)


@pytest.fixture
def catalogue(schemas_dir):
    return load_catalogue(schemas_dir)


@pytest.fixture
def vault():
    return Vault(b"test-secret")


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "ledger.db")


@pytest.fixture
def conn(db_path, vault) -> sqlite3.Connection:
    return connect(db_path, vault)
