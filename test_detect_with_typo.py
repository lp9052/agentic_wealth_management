import sys
sys.path.append('.')
from app.auditor.typo_filter import correct_typos
from app.auditor.signal_detector import detect_signals_semantic

prompt = "I want to by $20000 APPL put options"
corrected = correct_typos(prompt)
print("Corrected:", corrected)
print("Signals:", detect_signals_semantic(corrected))
