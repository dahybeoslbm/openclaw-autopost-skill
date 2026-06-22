import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "scripts"))
from utils.parser import parse_request
test_str = "đăng bài béo lên google map"
print("Result:", parse_request(test_str))
