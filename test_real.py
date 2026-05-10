import sys
import os
sys.path.append(os.path.abspath('.'))

from app.engine import graph
from app.database.client_db import get_client_state, get_client_data

client_id = "CLEAN_RETIREE"
raw_client_data = get_client_data(client_id)
state = {
    "client_id": client_id,
    "client_data": raw_client_data,
    "prompt": "I want to by $20000 APPL put options",
    "proposal": "",
    "proposal_json": {},
    "status": "PENDING",
    "revision_count": 0,
    "supervisor_enabled": True,
    "is_real_time": False,
    "fired_rules": [],
    "history_log": "",
    "additional_info": "",
    "info_injected": False
}

final_state = graph.invoke(state)
print("Final Status:", final_state['status'])
print("Audit Trace:\n", final_state['history_log'])
