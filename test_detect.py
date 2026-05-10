import sys
import os
sys.path.append(os.path.abspath('.'))
from app.auditor.signal_detector import detect_signals, detect_signals_semantic
print(detect_signals_semantic("I want to by $20000 APPL put options"))
print(detect_signals_semantic("i want to buy $5000 APPL put options"))
