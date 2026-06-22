import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "scripts"))
from utils.parser import _detect_topic
test_str = "đăng bài béo lên google map"
print("Result:", repr(_detect_topic(test_str, test_str)))
